"""
File-based ActivityPub storage adapter.

Stores data as JSON files organized by type:

    data_dir/
    ├── followers/
    │   └── {sanitized_actor_id}.json
    ├── interactions/
    │   └── {sanitized_target}/
    │       └── {type}-{sanitized_actor}.json
    ├── activities/
    │   └── {sanitized_activity_id}.json
    └── cache/
        └── actors/
            └── {sanitized_actor_id}.json

Thread-safe via an RLock per resource path.
"""

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...._model import (
    Follower,
    Interaction,
    InteractionStatus,
    InteractionType,
)
from ..._base import ActivityPubStorage

logger = logging.getLogger(__name__)


def _sanitize(value: str) -> str:
    """Create a filesystem-safe name from a URL or ID."""
    h = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    # Keep a readable prefix from the value
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in value)
    safe = safe.strip("-")[:80]
    return f"{safe}-{h}"


class FileActivityPubStorage(ActivityPubStorage):
    """
    File-based storage backend for ActivityPub data.

    All data is stored as JSON files, one per entity, organized in
    subdirectories. Thread-safe via per-path RLock.

    :param data_dir: Root directory for storing data.
    """

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self._locks: dict[str, threading.RLock] = {}
        self._global_lock = threading.RLock()

    def _get_lock(self, path: str) -> threading.RLock:
        """Get or create an RLock for a given path."""
        with self._global_lock:
            if path not in self._locks:
                self._locks[path] = threading.RLock()
            return self._locks[path]

    def _write_json(self, path: Path, data: Any) -> None:
        """Write data as JSON to a file, creating directories as needed."""
        lock = self._get_lock(str(path))
        with lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            try:
                tmp.write_text(
                    json.dumps(data, indent=2, default=str), encoding="utf-8"
                )
                tmp.replace(path)
            except Exception:
                if tmp.exists():
                    tmp.unlink()
                raise

    def _read_json(self, path: Path) -> Any:
        """Read JSON data from a file."""
        lock = self._get_lock(str(path))
        with lock:
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))

    def _delete_file(self, path: Path) -> bool:
        """Delete a file. Returns True if the file existed."""
        lock = self._get_lock(str(path))
        with lock:
            if path.exists():
                path.unlink()
                return True
            return False

    def _list_json_files(self, directory: Path) -> list[Path]:
        """List all .json files in a directory."""
        if not directory.exists():
            return []
        return sorted(directory.glob("*.json"))

    # ---------- Followers ----------

    def _follower_path(self, actor_id: str) -> Path:
        return self.data_dir / "followers" / f"{_sanitize(actor_id)}.json"

    def store_follower(self, follower: Follower):
        path = self._follower_path(follower.actor_id)
        self._write_json(path, follower.to_dict())

    def remove_follower(self, actor_id: str):
        path = self._follower_path(actor_id)
        self._delete_file(path)

    def get_followers(self) -> list[Follower]:
        followers_dir = self.data_dir / "followers"
        result = []
        for fpath in self._list_json_files(followers_dir):
            data = self._read_json(fpath)
            if data is not None:
                result.append(Follower.build(data))
        return result

    # ---------- Interactions ----------

    def _interaction_dir(self, target_resource: str) -> Path:
        return self.data_dir / "interactions" / _sanitize(target_resource)

    def _interaction_path(
        self,
        source_actor_id: str,
        target_resource: str,
        interaction_type: InteractionType,
    ) -> Path:
        dirname = self._interaction_dir(target_resource)
        filename = f"{interaction_type.value}-{_sanitize(source_actor_id)}.json"
        return dirname / filename

    def store_interaction(self, interaction: Interaction):
        path = self._interaction_path(
            interaction.source_actor_id,
            interaction.target_resource,
            interaction.interaction_type,
        )
        self._write_json(path, interaction.to_dict())

    def delete_interaction(
        self,
        source_actor_id: str,
        target_resource: str,
        interaction_type: InteractionType,
    ):
        path = self._interaction_path(
            source_actor_id, target_resource, interaction_type
        )
        # Mark as deleted rather than removing the file
        data = self._read_json(path)
        if data is not None:
            data["status"] = InteractionStatus.DELETED.value
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._write_json(path, data)

    def get_interactions(
        self,
        target_resource: str,
        interaction_type: InteractionType | None = None,
        status: InteractionStatus = InteractionStatus.CONFIRMED,
    ) -> list[Interaction]:
        interaction_dir = self._interaction_dir(target_resource)
        result = []
        for fpath in self._list_json_files(interaction_dir):
            data = self._read_json(fpath)
            if data is None:
                continue
            interaction = Interaction.build(data)
            if interaction.status != status:
                continue
            if (
                interaction_type is not None
                and interaction.interaction_type != interaction_type
            ):
                continue
            result.append(interaction)
        return result

    # ---------- Activities ----------

    def _activity_path(self, activity_id: str) -> Path:
        return self.data_dir / "activities" / f"{_sanitize(activity_id)}.json"

    def store_activity(self, activity_id: str, activity_data: dict):
        path = self._activity_path(activity_id)
        record = {
            "activity_id": activity_id,
            "activity_data": activity_data,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write_json(path, record)

    def get_activities(
        self,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        activities_dir = self.data_dir / "activities"
        files = self._list_json_files(activities_dir)
        # Sort by modification time, newest first
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        result = []
        for fpath in files[offset : offset + limit]:
            data = self._read_json(fpath)
            if data is not None and "activity_data" in data:
                result.append(data["activity_data"])
        return result

    # ---------- Actor cache ----------

    def _actor_cache_path(self, actor_id: str) -> Path:
        return self.data_dir / "cache" / "actors" / f"{_sanitize(actor_id)}.json"

    def cache_remote_actor(
        self,
        actor_id: str,
        actor_data: dict,
        fetched_at: datetime | None = None,
    ):
        path = self._actor_cache_path(actor_id)
        record = {
            "actor_id": actor_id,
            "actor_data": actor_data,
            "fetched_at": (fetched_at or datetime.now(timezone.utc)).isoformat(),
        }
        self._write_json(path, record)

    def get_cached_actor(
        self,
        actor_id: str,
        max_age_seconds: float = 86400.0,
    ) -> dict | None:
        path = self._actor_cache_path(actor_id)
        data = self._read_json(path)
        if data is None:
            return None

        fetched_at_str = data.get("fetched_at")
        if fetched_at_str:
            fetched_at = datetime.fromisoformat(fetched_at_str)
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
            if age > max_age_seconds:
                return None

        return data.get("actor_data")
