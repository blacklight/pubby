"""
Migration utilities for Pubby storage backends.

These functions help migrate existing data when the storage schema evolves.
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .adapters.file import FileActivityPubStorage
    from ._base import ActivityPubStorage

logger = logging.getLogger(__name__)


def extract_mentions_from_tags(obj_data: dict) -> list[str]:
    """Extract actor URLs from Mention tags in an ActivityPub object.

    This is the same logic as InboxProcessor._extract_mentioned_actors,
    duplicated here to avoid circular imports.
    """
    mentioned = []
    tags = obj_data.get("tag", [])
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, dict) and tag.get("type") == "Mention":
                href = tag.get("href")
                if isinstance(href, str) and href:
                    mentioned.append(href)
    return mentioned


def backfill_mentions(
    storage: "ActivityPubStorage",
    *,
    dry_run: bool = False,
) -> dict:
    """Backfill mentioned_actors for existing interactions.

    Scans all interactions, extracts mentions from the ``raw_object`` stored
    in metadata, and updates the ``mentioned_actors`` field.

    :param storage: The storage backend to migrate.
    :param dry_run: If True, only report what would be done without making changes.
    :return: A dict with migration statistics.
    """
    stats = {
        "scanned": 0,
        "updated": 0,
        "skipped_no_metadata": 0,
        "skipped_no_raw_object": 0,
        "skipped_already_has_mentions": 0,
        "errors": 0,
    }

    # Get all interactions - we need to iterate through all target resources
    # This is a limitation since we don't have a "get all interactions" method
    # For file storage, we can work around this
    from .adapters.file import FileActivityPubStorage

    if isinstance(storage, FileActivityPubStorage):
        interactions = _get_all_file_interactions(storage)
    else:
        logger.warning(
            "backfill_mentions currently only supports FileActivityPubStorage. "
            "For DB storage, run a direct SQL migration."
        )
        return stats

    for interaction in interactions:
        stats["scanned"] += 1

        # Skip if already has mentions
        if interaction.mentioned_actors:
            stats["skipped_already_has_mentions"] += 1
            continue

        # Check for raw_object in metadata
        metadata = interaction.metadata or {}
        if not metadata:
            stats["skipped_no_metadata"] += 1
            continue

        raw_object = metadata.get("raw_object")
        if not isinstance(raw_object, dict):
            stats["skipped_no_raw_object"] += 1
            continue

        # Extract mentions
        mentions = extract_mentions_from_tags(raw_object)
        if not mentions:
            continue

        # Update the interaction
        if not dry_run:
            try:
                interaction.mentioned_actors = mentions
                storage.store_interaction(interaction)
                stats["updated"] += 1
                logger.debug(
                    "Updated interaction %s with %d mentions",
                    interaction.object_id or interaction.activity_id,
                    len(mentions),
                )
            except Exception:
                logger.exception(
                    "Failed to update interaction %s",
                    interaction.object_id or interaction.activity_id,
                )
                stats["errors"] += 1
        else:
            stats["updated"] += 1
            logger.info(
                "[DRY RUN] Would update interaction %s with mentions: %s",
                interaction.object_id or interaction.activity_id,
                mentions,
            )

    return stats


def backfill_object_id_index(
    storage: "ActivityPubStorage",
    *,
    dry_run: bool = False,
) -> dict:
    """
    Backfill the object_id index for existing interactions.

    Scans all interactions and creates index entries for those with an
    ``object_id``. This enables O(1) lookups via ``get_interaction_by_object_id()``.

    :param storage: The storage backend to migrate.
    :param dry_run: If True, only report what would be done without making changes.
    :return: A dict with migration statistics.
    """
    stats = {
        "scanned": 0,
        "indexed": 0,
        "skipped_no_object_id": 0,
        "skipped_already_indexed": 0,
        "errors": 0,
    }

    from .adapters.file import FileActivityPubStorage

    if not isinstance(storage, FileActivityPubStorage):
        logger.warning(
            "backfill_object_id_index only supports FileActivityPubStorage. "
            "DB storage uses SQL indexes automatically."
        )
        return stats

    interactions = _get_all_file_interactions(storage)

    for interaction in interactions:
        stats["scanned"] += 1

        if not interaction.object_id:
            stats["skipped_no_object_id"] += 1
            continue

        # Check if already indexed
        index_path = storage._object_id_index_path(interaction.object_id)
        if index_path.exists():
            stats["skipped_already_indexed"] += 1
            continue

        if not dry_run:
            try:
                storage._update_object_id_index(interaction, add=True)
                stats["indexed"] += 1
                logger.debug(
                    "Indexed interaction %s",
                    interaction.object_id,
                )
            except Exception:
                logger.exception(
                    "Failed to index interaction %s",
                    interaction.object_id,
                )
                stats["errors"] += 1
        else:
            stats["indexed"] += 1
            logger.info(
                "[DRY RUN] Would index interaction: %s",
                interaction.object_id,
            )

    return stats


def _get_all_file_interactions(storage: "FileActivityPubStorage") -> list:
    """Get all interactions from a FileActivityPubStorage backend."""
    import os
    import json

    interactions = []
    interactions_dir = storage.data_dir / "interactions"

    if not os.path.isdir(interactions_dir):
        return interactions

    # Iterate through all target resource directories
    for target_hash in os.listdir(interactions_dir):
        target_dir = os.path.join(interactions_dir, target_hash)
        if not os.path.isdir(target_dir):
            continue

        # Read all interaction files in this directory
        for filename in os.listdir(target_dir):
            if not filename.endswith(".json"):
                continue

            filepath = os.path.join(target_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)

                from .._model import Interaction

                interaction = Interaction.build(data)
                interactions.append(interaction)
            except Exception:
                logger.warning("Failed to load interaction from %s", filepath)

    return interactions
