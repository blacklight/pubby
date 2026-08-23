"""
Tests for storage migration utilities.
"""

import json
import tempfile
from datetime import datetime, timezone

import pytest

from pubby._model import Follower, Interaction, InteractionType
from pubby.storage import backfill_mentions
from pubby.storage._migrations import extract_mentions_from_tags
from pubby.storage.adapters.file import FileActivityPubStorage


class TestExtractMentionsFromTags:
    """Tests for the mention extraction helper."""

    def test_extract_mentions(self):
        obj_data = {
            "tag": [
                {"type": "Mention", "href": "https://example.com/users/alice"},
                {"type": "Mention", "href": "https://example.com/users/bob"},
                {"type": "Hashtag", "name": "#test"},
            ]
        }
        result = extract_mentions_from_tags(obj_data)
        assert result == [
            "https://example.com/users/alice",
            "https://example.com/users/bob",
        ]

    def test_empty_tags(self):
        assert extract_mentions_from_tags({}) == []
        assert extract_mentions_from_tags({"tag": []}) == []

    def test_no_mentions(self):
        obj_data = {"tag": [{"type": "Hashtag", "name": "#test"}]}
        assert extract_mentions_from_tags(obj_data) == []

    def test_invalid_structures(self):
        obj_data = {
            "tag": [
                {"type": "Mention"},  # Missing href
                {"type": "Mention", "href": ""},  # Empty href
                {"type": "Mention", "href": "https://valid.example.com/user"},
                "not a dict",
            ]
        }
        result = extract_mentions_from_tags(obj_data)
        assert result == ["https://valid.example.com/user"]


class TestBackfillMentions:
    """Tests for the backfill_mentions migration."""

    @pytest.fixture
    def storage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield FileActivityPubStorage(tmpdir)

    def test_backfill_updates_interactions_with_raw_object(self, storage):
        """Should extract mentions from raw_object in metadata."""
        now = datetime.now(timezone.utc)

        # Create an interaction with raw_object containing mentions
        interaction = Interaction(
            source_actor_id="https://remote.example.com/users/alice",
            target_resource="https://blog.example.com/posts/1",
            interaction_type=InteractionType.REPLY,
            content="Hello @blog",
            metadata={
                "raw_object": {
                    "type": "Note",
                    "tag": [
                        {
                            "type": "Mention",
                            "href": "https://blog.example.com/ap/actor",
                        }
                    ],
                }
            },
            created_at=now,
            updated_at=now,
        )
        storage.store_interaction(interaction)

        # Run migration
        stats = backfill_mentions(storage)

        assert stats["scanned"] == 1
        assert stats["updated"] == 1

        # Verify mentions were added
        interactions = storage.get_interactions("https://blog.example.com/posts/1")
        assert len(interactions) == 1
        assert interactions[0].mentioned_actors == ["https://blog.example.com/ap/actor"]

    def test_backfill_skips_interactions_with_existing_mentions(self, storage):
        """Should skip interactions that already have mentioned_actors."""
        now = datetime.now(timezone.utc)

        interaction = Interaction(
            source_actor_id="https://remote.example.com/users/alice",
            target_resource="https://blog.example.com/posts/1",
            interaction_type=InteractionType.REPLY,
            mentioned_actors=["https://already.example.com/actor"],
            metadata={
                "raw_object": {
                    "tag": [
                        {"type": "Mention", "href": "https://new.example.com/actor"}
                    ]
                }
            },
            created_at=now,
            updated_at=now,
        )
        storage.store_interaction(interaction)

        stats = backfill_mentions(storage)

        assert stats["skipped_already_has_mentions"] == 1
        assert stats["updated"] == 0

    def test_backfill_skips_interactions_without_metadata(self, storage):
        """Should skip interactions without metadata."""
        now = datetime.now(timezone.utc)

        interaction = Interaction(
            source_actor_id="https://remote.example.com/users/alice",
            target_resource="https://blog.example.com/posts/1",
            interaction_type=InteractionType.LIKE,
            created_at=now,
            updated_at=now,
        )
        storage.store_interaction(interaction)

        stats = backfill_mentions(storage)

        assert stats["skipped_no_metadata"] == 1
        assert stats["updated"] == 0

    def test_backfill_dry_run(self, storage):
        """Dry run should not modify data."""
        now = datetime.now(timezone.utc)

        interaction = Interaction(
            source_actor_id="https://remote.example.com/users/alice",
            target_resource="https://blog.example.com/posts/1",
            interaction_type=InteractionType.REPLY,
            metadata={
                "raw_object": {
                    "tag": [
                        {"type": "Mention", "href": "https://blog.example.com/ap/actor"}
                    ]
                }
            },
            created_at=now,
            updated_at=now,
        )
        storage.store_interaction(interaction)

        stats = backfill_mentions(storage, dry_run=True)

        assert stats["updated"] == 1

        # Verify data was not modified
        interactions = storage.get_interactions("https://blog.example.com/posts/1")
        assert interactions[0].mentioned_actors == []

    def test_backfill_multiple_interactions(self, storage):
        """Should process multiple interactions correctly."""
        now = datetime.now(timezone.utc)

        # Interaction with mentions
        storage.store_interaction(
            Interaction(
                source_actor_id="https://remote.example.com/users/alice",
                target_resource="https://blog.example.com/posts/1",
                interaction_type=InteractionType.REPLY,
                metadata={
                    "raw_object": {
                        "tag": [
                            {"type": "Mention", "href": "https://mentioned.example.com"}
                        ]
                    }
                },
                created_at=now,
                updated_at=now,
            )
        )

        # Interaction without metadata (like)
        storage.store_interaction(
            Interaction(
                source_actor_id="https://remote.example.com/users/bob",
                target_resource="https://blog.example.com/posts/1",
                interaction_type=InteractionType.LIKE,
                created_at=now,
                updated_at=now,
            )
        )

        # Interaction with existing mentions
        storage.store_interaction(
            Interaction(
                source_actor_id="https://remote.example.com/users/carol",
                target_resource="https://blog.example.com/posts/2",
                interaction_type=InteractionType.REPLY,
                mentioned_actors=["https://existing.example.com"],
                metadata={"raw_object": {"tag": []}},
                created_at=now,
                updated_at=now,
            )
        )

        stats = backfill_mentions(storage)

        assert stats["scanned"] == 3
        assert stats["updated"] == 1
        assert stats["skipped_no_metadata"] == 1
        assert stats["skipped_already_has_mentions"] == 1


class TestFileSchemaV4Migration:
    """Tests for the v3 to v4 file storage follower migration."""

    def test_v3_to_v4_migrates_legacy_followers(self, tmp_path):
        from pubby.storage.adapters.file._storage import (
            SCHEMA_VERSION,
            _sanitize,
        )

        remote = "https://remote.example.com/users/alice"
        local_actor = "https://blog.example.com/ap/actor"

        # Simulate a v3 store: a legacy follower file without target_actor_id
        followers_dir = tmp_path / "followers"
        followers_dir.mkdir(parents=True)
        legacy_path = followers_dir / f"{_sanitize(remote)}.json"
        legacy_path.write_text(
            json.dumps(
                Follower(
                    actor_id=remote,
                    inbox="https://remote.example.com/users/alice/inbox",
                ).to_dict()
            ),
            encoding="utf-8",
        )

        # Mark the store as schema version 3
        version_path = tmp_path / ".schema_version"
        version_path.write_text("3", encoding="utf-8")

        # Open the storage: this should run the v4 migration
        storage = FileActivityPubStorage(data_dir=tmp_path)

        assert storage._get_schema_version() == SCHEMA_VERSION

        # Legacy/unassigned followers are still returned for any actor
        actor_followers = storage.get_followers(actor_id=local_actor)
        all_followers = storage.get_followers()

        assert len(actor_followers) == 1
        assert actor_followers[0].actor_id == remote
        assert actor_followers[0].target_actor_id == ""
        assert len(all_followers) == 1
