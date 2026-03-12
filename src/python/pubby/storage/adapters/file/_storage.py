"""
File-based ActivityPub storage adapter.

Stores data as JSON files organized by type:

    data_dir/
    ├── followers/
    │   └── {sanitized_actor_id}.json
    ├── interactions/
    │   ├── {sanitized_target}/
    │   │   └── {type}-{sanitized_actor}.json
    │   └── _mentions/
    │       └── {sanitized_actor}.json  (reverse index)
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

# Schema version history:
# 1: Initial version (no version file = version 0)
# 2: Added _object_ids/ index for get_interaction_by_object_id()
SCHEMA_VERSION = 2


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

    def __init__(self, data_dir: str | Path, *, auto_migrate: bool = True):
        self.data_dir = Path(data_dir)
        self._locks: dict[str, threading.RLock] = {}
        self._global_lock = threading.RLock()

        if auto_migrate:
            self._run_migrations()

    # ---------- Schema versioning ----------

    @property
    def _version_path(self) -> Path:
        return self.data_dir / ".schema_version"

    def _get_schema_version(self) -> int:
        """Get the current schema version from the version file."""
        if not self._version_path.exists():
            return 0
        try:
            return int(self._version_path.read_text().strip())
        except (ValueError, OSError):
            return 0

    def _set_schema_version(self, version: int) -> None:
        """Write the schema version to the version file."""
        self._version_path.parent.mkdir(parents=True, exist_ok=True)
        self._version_path.write_text(str(version))

    def _run_migrations(self) -> None:
        """Run any pending migrations to bring schema up to date."""
        current = self._get_schema_version()
        if current >= SCHEMA_VERSION:
            return

        logger.info(
            "Migrating file storage schema from version %d to %d",
            current,
            SCHEMA_VERSION,
        )

        # Migration registry: version -> migration function
        migrations = {
            2: self._migrate_to_v2_object_id_index,
        }

        for version in range(current + 1, SCHEMA_VERSION + 1):
            if version in migrations:
                logger.info("Running migration to version %d", version)
                migrations[version]()

        self._set_schema_version(SCHEMA_VERSION)
        logger.info("Migration complete")

    def _migrate_to_v2_object_id_index(self) -> None:
        """Migrate to v2: backfill object_id index."""
        from ..._migrations import _get_all_file_interactions

        interactions = _get_all_file_interactions(self)
        indexed = 0

        for interaction in interactions:
            if interaction.object_id:
                self._update_object_id_index(interaction, add=True)
                indexed += 1

        logger.info("Indexed %d interactions by object_id", indexed)

    def _get_lock(self, path: str) -> threading.RLock:
        """Get or create an RLock for a given path."""
        with self._global_lock:
            if path not in self._locks:
                self._locks[path] = threading.RLock()
            return self._locks[path]

    def write_json(self, path: Path, data: Any) -> None:
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

    def read_json(self, path: Path) -> Any:
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

    def list_json_files(self, directory: Path) -> list[Path]:
        """List all .json files in a directory."""
        if not directory.exists():
            return []
        return sorted(directory.glob("*.json"))

    # ---------- Followers ----------

    def _follower_path(self, actor_id: str) -> Path:
        return self.data_dir / "followers" / f"{_sanitize(actor_id)}.json"

    def store_follower(self, follower: Follower):
        path = self._follower_path(follower.actor_id)
        self.write_json(path, follower.to_dict())

    def remove_follower(self, actor_id: str):
        path = self._follower_path(actor_id)
        self._delete_file(path)

    def get_followers(self) -> list[Follower]:
        followers_dir = self.data_dir / "followers"
        result = []
        for fpath in self.list_json_files(followers_dir):
            data = self.read_json(fpath)
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
        self.write_json(path, interaction.to_dict())

        # Update mention index for each mentioned actor
        if interaction.mentioned_actors:
            self._update_mention_index(interaction, add=True)

        # Update object_id index
        if interaction.object_id:
            self._update_object_id_index(interaction, add=True)

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
        data = self.read_json(path)
        if data is not None:
            interaction = Interaction.build(data)

            # Remove from mention index before marking deleted
            if interaction.mentioned_actors:
                self._update_mention_index(interaction, add=False)

            # Note: We keep the object_id index for deleted interactions
            # so they can still be looked up with status=DELETED

            data["status"] = InteractionStatus.DELETED.value
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.write_json(path, data)

    def delete_interaction_by_object_id(
        self,
        source_actor_id: str,
        object_id: str,
    ) -> bool:
        # Use the object_id index for efficient lookup
        index_path = self._object_id_index_path(object_id)
        if not index_path.exists():
            return False

        index_data = self.read_json(index_path)
        if not index_data or index_data.get("source_actor_id") != source_actor_id:
            return False

        itype = InteractionType(index_data["interaction_type"])
        interaction_path = self._interaction_path(
            index_data["source_actor_id"],
            index_data["target_resource"],
            itype,
        )

        data = self.read_json(interaction_path)
        if data is None or data.get("status") == InteractionStatus.DELETED.value:
            return False

        interaction = Interaction.build(data)
        if interaction.mentioned_actors:
            self._update_mention_index(interaction, add=False)

        # Note: We keep the object_id index for deleted interactions
        # so they can still be looked up with status=DELETED

        data["status"] = InteractionStatus.DELETED.value
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.write_json(interaction_path, data)
        return True

    def get_interaction_by_object_id(
        self,
        object_id: str,
        status: InteractionStatus = InteractionStatus.CONFIRMED,
    ) -> Interaction | None:
        # Use the object_id index for O(1) lookup
        index_path = self._object_id_index_path(object_id)
        if not index_path.exists():
            return None

        index_data = self.read_json(index_path)
        if not index_data:
            return None

        itype = InteractionType(index_data["interaction_type"])
        interaction_path = self._interaction_path(
            index_data["source_actor_id"],
            index_data["target_resource"],
            itype,
        )

        data = self.read_json(interaction_path)
        if data is None:
            return None

        interaction = Interaction.build(data)
        if interaction.status != status:
            return None

        return interaction

    def get_interactions(
        self,
        target_resource: str,
        interaction_type: InteractionType | None = None,
        status: InteractionStatus = InteractionStatus.CONFIRMED,
    ) -> list[Interaction]:
        interaction_dir = self._interaction_dir(target_resource)
        result = []
        for fpath in self.list_json_files(interaction_dir):
            data = self.read_json(fpath)
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

    # ---------- Mention index ----------

    def _mention_index_path(self, actor_url: str) -> Path:
        """Path to the mention index file for a given actor."""
        return (
            self.data_dir
            / "interactions"
            / "_mentions"
            / f"{_sanitize(actor_url)}.json"
        )

    def _update_mention_index(self, interaction: Interaction, add: bool) -> None:
        """Add or remove an interaction from the mention index."""
        for actor_url in interaction.mentioned_actors:
            path = self._mention_index_path(actor_url)
            lock = self._get_lock(str(path))
            with lock:
                index: list[dict] = []
                if path.exists():
                    index = json.loads(path.read_text(encoding="utf-8"))

                entry = {
                    "target_resource": interaction.target_resource,
                    "interaction_type": interaction.interaction_type.value,
                    "source_actor_id": interaction.source_actor_id,
                }

                if add:
                    # Avoid duplicates
                    if entry not in index:
                        index.append(entry)
                else:
                    # Remove if present
                    if entry in index:
                        index.remove(entry)

                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(index, indent=2), encoding="utf-8")

    # ---------- Object ID index ----------

    def _object_id_index_path(self, object_id: str) -> Path:
        """Path to the object_id index file."""
        return (
            self.data_dir
            / "interactions"
            / "_object_ids"
            / f"{_sanitize(object_id)}.json"
        )

    def _update_object_id_index(self, interaction: Interaction, add: bool) -> None:
        """Add or remove an interaction from the object_id index."""
        if not interaction.object_id:
            return

        path = self._object_id_index_path(interaction.object_id)
        lock = self._get_lock(str(path))
        with lock:
            if add:
                entry = {
                    "target_resource": interaction.target_resource,
                    "interaction_type": interaction.interaction_type.value,
                    "source_actor_id": interaction.source_actor_id,
                }
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(entry, indent=2), encoding="utf-8")
            else:
                # Remove the index file
                if path.exists():
                    path.unlink()

    def get_interactions_mentioning(
        self,
        actor_url: str,
        interaction_type: InteractionType | None = None,
        status: InteractionStatus = InteractionStatus.CONFIRMED,
    ) -> list[Interaction]:
        """Retrieve interactions that mention a given actor URL."""
        path = self._mention_index_path(actor_url)
        if not path.exists():
            return []

        index = self.read_json(path)
        if not index:
            return []

        result = []
        for entry in index:
            if (
                interaction_type is not None
                and entry.get("interaction_type") != interaction_type.value
            ):
                continue

            itype = InteractionType(entry["interaction_type"])
            interaction_path = self._interaction_path(
                entry["source_actor_id"],
                entry["target_resource"],
                itype,
            )
            data = self.read_json(interaction_path)
            if data is None:
                continue

            interaction = Interaction.build(data)
            if interaction.status != status:
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
        self.write_json(path, record)

    def get_activities(
        self,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        activities_dir = self.data_dir / "activities"
        files = self.list_json_files(activities_dir)
        # Sort by modification time, newest first
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        result = []
        for fpath in files[offset : offset + limit]:
            data = self.read_json(fpath)
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
        self.write_json(path, record)

    def get_cached_actor(
        self,
        actor_id: str,
        max_age_seconds: float = 86400.0,
    ) -> dict | None:
        path = self._actor_cache_path(actor_id)
        data = self.read_json(path)
        if data is None:
            return None

        fetched_at_str = data.get("fetched_at")
        if fetched_at_str:
            fetched_at = datetime.fromisoformat(fetched_at_str.replace("Z", "+00:00"))
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
            if age > max_age_seconds:
                return None

        return data.get("actor_data")

    # ---------- Quote authorizations ----------

    def _quote_auth_path(self, authorization_id: str) -> Path:
        return (
            self.data_dir
            / "quote_authorizations"
            / f"{_sanitize(authorization_id)}.json"
        )

    def store_quote_authorization(
        self,
        authorization_id: str,
        authorization_data: dict,
    ):
        path = self._quote_auth_path(authorization_id)
        self.write_json(path, authorization_data)

    def get_quote_authorization(self, authorization_id: str) -> dict | None:
        path = self._quote_auth_path(authorization_id)
        return self.read_json(path)
