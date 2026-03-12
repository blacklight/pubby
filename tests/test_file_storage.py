"""
Tests for file-based storage CRUD operations.
"""

from datetime import datetime, timezone

import pytest

from pubby._model import (
    Follower,
    Interaction,
    InteractionStatus,
    InteractionType,
)
from pubby.storage.adapters.file import FileActivityPubStorage


@pytest.fixture
def storage(tmp_path):
    return FileActivityPubStorage(data_dir=tmp_path)


class TestFollowers:
    def test_store_and_get_follower(self, storage):
        follower = Follower(
            actor_id="https://mastodon.social/users/alice",
            inbox="https://mastodon.social/users/alice/inbox",
            shared_inbox="https://mastodon.social/inbox",
            followed_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            actor_data={"name": "Alice"},
        )

        storage.store_follower(follower)
        followers = storage.get_followers()

        assert len(followers) == 1
        assert followers[0].actor_id == "https://mastodon.social/users/alice"
        assert followers[0].inbox == "https://mastodon.social/users/alice/inbox"
        assert followers[0].shared_inbox == "https://mastodon.social/inbox"
        assert followers[0].actor_data == {"name": "Alice"}

    def test_remove_follower(self, storage):
        follower = Follower(
            actor_id="https://mastodon.social/users/alice",
            inbox="https://mastodon.social/users/alice/inbox",
        )
        storage.store_follower(follower)
        assert len(storage.get_followers()) == 1

        storage.remove_follower("https://mastodon.social/users/alice")
        assert len(storage.get_followers()) == 0

    def test_update_follower(self, storage):
        follower = Follower(
            actor_id="https://mastodon.social/users/alice",
            inbox="https://mastodon.social/users/alice/inbox",
        )
        storage.store_follower(follower)

        # Update with new data
        updated = Follower(
            actor_id="https://mastodon.social/users/alice",
            inbox="https://mastodon.social/users/alice/inbox",
            shared_inbox="https://mastodon.social/inbox",
            actor_data={"name": "Alice Updated"},
        )
        storage.store_follower(updated)

        followers = storage.get_followers()
        assert len(followers) == 1
        assert followers[0].actor_data == {"name": "Alice Updated"}

    def test_get_followers_empty(self, storage):
        assert storage.get_followers() == []

    def test_multiple_followers(self, storage):
        for i in range(5):
            storage.store_follower(
                Follower(
                    actor_id=f"https://example.com/users/user{i}",
                    inbox=f"https://example.com/users/user{i}/inbox",
                )
            )
        assert len(storage.get_followers()) == 5


class TestInteractions:
    def test_store_and_get_interaction(self, storage):
        interaction = Interaction(
            source_actor_id="https://mastodon.social/users/alice",
            target_resource="https://blog.example.com/post/1",
            interaction_type=InteractionType.REPLY,
            content="<p>Great post!</p>",
            author_name="Alice",
            published=datetime(2024, 1, 1, tzinfo=timezone.utc),
            status=InteractionStatus.CONFIRMED,
        )

        storage.store_interaction(interaction)
        interactions = storage.get_interactions("https://blog.example.com/post/1")

        assert len(interactions) == 1
        assert interactions[0].content == "<p>Great post!</p>"
        assert interactions[0].interaction_type == InteractionType.REPLY

    def test_get_interactions_by_type(self, storage):
        storage.store_interaction(
            Interaction(
                source_actor_id="https://example.com/users/alice",
                target_resource="https://blog.example.com/post/1",
                interaction_type=InteractionType.REPLY,
                content="Reply",
            )
        )
        storage.store_interaction(
            Interaction(
                source_actor_id="https://example.com/users/bob",
                target_resource="https://blog.example.com/post/1",
                interaction_type=InteractionType.LIKE,
            )
        )

        replies = storage.get_interactions(
            "https://blog.example.com/post/1",
            interaction_type=InteractionType.REPLY,
        )
        assert len(replies) == 1
        assert replies[0].interaction_type == InteractionType.REPLY

        likes = storage.get_interactions(
            "https://blog.example.com/post/1",
            interaction_type=InteractionType.LIKE,
        )
        assert len(likes) == 1

    def test_delete_interaction(self, storage):
        storage.store_interaction(
            Interaction(
                source_actor_id="https://example.com/users/alice",
                target_resource="https://blog.example.com/post/1",
                interaction_type=InteractionType.LIKE,
            )
        )

        storage.delete_interaction(
            "https://example.com/users/alice",
            "https://blog.example.com/post/1",
            InteractionType.LIKE,
        )

        # Should not appear in confirmed results
        interactions = storage.get_interactions("https://blog.example.com/post/1")
        assert len(interactions) == 0

        # But should appear with DELETED status
        deleted = storage.get_interactions(
            "https://blog.example.com/post/1",
            status=InteractionStatus.DELETED,
        )
        assert len(deleted) == 1

    def test_delete_interaction_by_object_id(self, storage):
        storage.store_interaction(
            Interaction(
                source_actor_id="https://example.com/users/alice",
                target_resource="https://blog.example.com/post/1",
                interaction_type=InteractionType.REPLY,
                object_id="https://example.com/users/alice/statuses/123",
                content="A reply",
            )
        )

        found = storage.delete_interaction_by_object_id(
            "https://example.com/users/alice",
            "https://example.com/users/alice/statuses/123",
        )
        assert found is True

        interactions = storage.get_interactions("https://blog.example.com/post/1")
        assert len(interactions) == 0

        deleted = storage.get_interactions(
            "https://blog.example.com/post/1",
            status=InteractionStatus.DELETED,
        )
        assert len(deleted) == 1

    def test_delete_interaction_by_object_id_not_found(self, storage):
        found = storage.delete_interaction_by_object_id(
            "https://example.com/users/alice",
            "https://example.com/users/alice/statuses/999",
        )
        assert found is False

    def test_delete_interaction_by_object_id_wrong_actor(self, storage):
        storage.store_interaction(
            Interaction(
                source_actor_id="https://example.com/users/alice",
                target_resource="https://blog.example.com/post/1",
                interaction_type=InteractionType.REPLY,
                object_id="https://example.com/users/alice/statuses/123",
            )
        )

        found = storage.delete_interaction_by_object_id(
            "https://example.com/users/bob",
            "https://example.com/users/alice/statuses/123",
        )
        assert found is False

        # Original still confirmed
        interactions = storage.get_interactions("https://blog.example.com/post/1")
        assert len(interactions) == 1

    def test_delete_interaction_by_object_id_idempotent(self, storage):
        storage.store_interaction(
            Interaction(
                source_actor_id="https://example.com/users/alice",
                target_resource="https://blog.example.com/post/1",
                interaction_type=InteractionType.REPLY,
                object_id="https://example.com/users/alice/statuses/123",
            )
        )

        storage.delete_interaction_by_object_id(
            "https://example.com/users/alice",
            "https://example.com/users/alice/statuses/123",
        )
        # Second call returns False (already deleted)
        found = storage.delete_interaction_by_object_id(
            "https://example.com/users/alice",
            "https://example.com/users/alice/statuses/123",
        )
        assert found is False

    def test_delete_interaction_by_object_id_ignores_mentions_index(self, storage):
        storage.store_interaction(
            Interaction(
                source_actor_id="https://example.com/users/alice",
                target_resource="https://blog.example.com/post/1",
                interaction_type=InteractionType.REPLY,
                object_id="https://example.com/users/alice/statuses/123",
                mentioned_actors=["https://example.com/users/bob"],
            )
        )

        # Mention index is stored as a JSON list under interactions/_mentions.
        mentions_dir = storage.data_dir / "interactions" / "_mentions"
        mention_files = list(mentions_dir.glob("*.json"))
        assert len(mention_files) == 1

        found = storage.delete_interaction_by_object_id(
            "https://example.com/users/alice",
            "https://example.com/users/alice/statuses/123",
        )
        assert found is True

    def test_get_interactions_empty(self, storage):
        assert storage.get_interactions("https://blog.example.com/nonexistent") == []

    def test_get_interaction_by_object_id(self, storage):
        storage.store_interaction(
            Interaction(
                source_actor_id="https://mastodon.social/users/alice",
                target_resource="https://blog.example.com/post/1",
                interaction_type=InteractionType.REPLY,
                object_id="https://mastodon.social/users/alice/statuses/123",
                content="<p>Great post!</p>",
            )
        )

        result = storage.get_interaction_by_object_id(
            "https://mastodon.social/users/alice/statuses/123"
        )
        assert result is not None
        assert result.source_actor_id == "https://mastodon.social/users/alice"
        assert result.content == "<p>Great post!</p>"

    def test_get_interaction_by_object_id_not_found(self, storage):
        result = storage.get_interaction_by_object_id(
            "https://mastodon.social/users/alice/statuses/999"
        )
        assert result is None

    def test_get_interaction_by_object_id_respects_status(self, storage):
        storage.store_interaction(
            Interaction(
                source_actor_id="https://mastodon.social/users/alice",
                target_resource="https://blog.example.com/post/1",
                interaction_type=InteractionType.REPLY,
                object_id="https://mastodon.social/users/alice/statuses/123",
            )
        )

        # Delete the interaction
        storage.delete_interaction(
            "https://mastodon.social/users/alice",
            "https://blog.example.com/post/1",
            InteractionType.REPLY,
        )

        # Should not find with default CONFIRMED status
        result = storage.get_interaction_by_object_id(
            "https://mastodon.social/users/alice/statuses/123"
        )
        assert result is None

        # Should find with DELETED status
        result = storage.get_interaction_by_object_id(
            "https://mastodon.social/users/alice/statuses/123",
            status=InteractionStatus.DELETED,
        )
        assert result is not None

    def test_get_interaction_by_object_id_multiple_targets(self, storage):
        """Find interaction when same object_id could be under different targets."""
        storage.store_interaction(
            Interaction(
                source_actor_id="https://mastodon.social/users/alice",
                target_resource="https://blog.example.com/post/1",
                interaction_type=InteractionType.REPLY,
                object_id="https://mastodon.social/users/alice/statuses/100",
            )
        )
        storage.store_interaction(
            Interaction(
                source_actor_id="https://mastodon.social/users/bob",
                target_resource="https://blog.example.com/post/2",
                interaction_type=InteractionType.REPLY,
                object_id="https://mastodon.social/users/bob/statuses/200",
            )
        )

        # Find Bob's interaction without knowing target_resource
        result = storage.get_interaction_by_object_id(
            "https://mastodon.social/users/bob/statuses/200"
        )
        assert result is not None
        assert result.source_actor_id == "https://mastodon.social/users/bob"


class TestMentionIndex:
    def test_store_with_mentions_creates_index(self, storage):
        interaction = Interaction(
            source_actor_id="https://remote.example.com/users/alice",
            target_resource="https://other.example.com/posts/1",
            interaction_type=InteractionType.REPLY,
            content="@blog Hello!",
            mentioned_actors=["https://blog.example.com/ap/actor"],
        )
        storage.store_interaction(interaction)

        # Should be retrievable by mention
        results = storage.get_interactions_mentioning(
            "https://blog.example.com/ap/actor"
        )
        assert len(results) == 1
        assert results[0].source_actor_id == "https://remote.example.com/users/alice"

    def test_get_interactions_mentioning_empty(self, storage):
        results = storage.get_interactions_mentioning(
            "https://blog.example.com/ap/actor"
        )
        assert results == []

    def test_get_interactions_mentioning_filter_by_type(self, storage):
        storage.store_interaction(
            Interaction(
                source_actor_id="https://remote.example.com/users/alice",
                target_resource="https://other.example.com/posts/1",
                interaction_type=InteractionType.REPLY,
                mentioned_actors=["https://blog.example.com/ap/actor"],
            )
        )
        storage.store_interaction(
            Interaction(
                source_actor_id="https://remote.example.com/users/bob",
                target_resource="https://other.example.com/posts/2",
                interaction_type=InteractionType.MENTION,
                mentioned_actors=["https://blog.example.com/ap/actor"],
            )
        )

        replies = storage.get_interactions_mentioning(
            "https://blog.example.com/ap/actor",
            interaction_type=InteractionType.REPLY,
        )
        assert len(replies) == 1
        assert replies[0].interaction_type == InteractionType.REPLY

    def test_delete_removes_from_mention_index(self, storage):
        storage.store_interaction(
            Interaction(
                source_actor_id="https://remote.example.com/users/alice",
                target_resource="https://other.example.com/posts/1",
                interaction_type=InteractionType.REPLY,
                mentioned_actors=["https://blog.example.com/ap/actor"],
            )
        )

        storage.delete_interaction(
            "https://remote.example.com/users/alice",
            "https://other.example.com/posts/1",
            InteractionType.REPLY,
        )

        # Should not appear in mention results (filtered by status)
        results = storage.get_interactions_mentioning(
            "https://blog.example.com/ap/actor"
        )
        assert len(results) == 0

    def test_multiple_mentions_indexed(self, storage):
        storage.store_interaction(
            Interaction(
                source_actor_id="https://remote.example.com/users/alice",
                target_resource="https://other.example.com/posts/1",
                interaction_type=InteractionType.REPLY,
                mentioned_actors=[
                    "https://blog.example.com/ap/actor",
                    "https://other.example.com/ap/actor",
                ],
            )
        )

        results1 = storage.get_interactions_mentioning(
            "https://blog.example.com/ap/actor"
        )
        results2 = storage.get_interactions_mentioning(
            "https://other.example.com/ap/actor"
        )

        assert len(results1) == 1
        assert len(results2) == 1


class TestActivities:
    def test_store_and_get_activities(self, storage):
        storage.store_activity("act-1", {"id": "act-1", "type": "Create"})
        storage.store_activity("act-2", {"id": "act-2", "type": "Update"})

        activities = storage.get_activities()
        assert len(activities) == 2

    def test_get_activities_with_limit(self, storage):
        for i in range(10):
            storage.store_activity(f"act-{i}", {"id": f"act-{i}", "type": "Create"})

        activities = storage.get_activities(limit=3)
        assert len(activities) == 3

    def test_get_activities_with_offset(self, storage):
        for i in range(5):
            storage.store_activity(f"act-{i}", {"id": f"act-{i}", "type": "Create"})

        activities = storage.get_activities(limit=2, offset=3)
        assert len(activities) == 2

    def test_get_activities_empty(self, storage):
        assert storage.get_activities() == []


class TestActorCache:
    def test_cache_and_get_actor(self, storage):
        actor_data = {
            "id": "https://mastodon.social/users/alice",
            "type": "Person",
            "name": "Alice",
        }

        storage.cache_remote_actor("https://mastodon.social/users/alice", actor_data)

        cached = storage.get_cached_actor("https://mastodon.social/users/alice")
        assert cached is not None
        assert cached["name"] == "Alice"

    def test_cache_expiry(self, storage):
        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        storage.cache_remote_actor(
            "https://mastodon.social/users/alice",
            {"name": "Alice"},
            fetched_at=old_time,
        )

        # With default max_age (24h), this should be expired
        cached = storage.get_cached_actor("https://mastodon.social/users/alice")
        assert cached is None

    def test_cache_fresh(self, storage):
        storage.cache_remote_actor(
            "https://mastodon.social/users/alice",
            {"name": "Alice"},
        )

        cached = storage.get_cached_actor(
            "https://mastodon.social/users/alice",
            max_age_seconds=3600,
        )
        assert cached is not None

    def test_cache_miss(self, storage):
        assert storage.get_cached_actor("https://nonexistent.example.com/user") is None

    def test_cache_update(self, storage):
        storage.cache_remote_actor(
            "https://mastodon.social/users/alice",
            {"name": "Alice"},
        )
        storage.cache_remote_actor(
            "https://mastodon.social/users/alice",
            {"name": "Alice Updated"},
        )

        cached = storage.get_cached_actor("https://mastodon.social/users/alice")
        assert cached["name"] == "Alice Updated"


class TestSchemaMigration:
    def test_new_storage_sets_current_version(self, tmp_path):
        from pubby.storage.adapters.file._storage import SCHEMA_VERSION

        storage = FileActivityPubStorage(data_dir=tmp_path)
        assert storage._get_schema_version() == SCHEMA_VERSION

    def test_auto_migrate_disabled(self, tmp_path):
        storage = FileActivityPubStorage(data_dir=tmp_path, auto_migrate=False)
        # No version file should be created
        assert storage._get_schema_version() == 0

    def test_migration_backfills_object_id_index(self, tmp_path):
        # Create storage without auto-migrate
        storage = FileActivityPubStorage(data_dir=tmp_path, auto_migrate=False)

        # Store an interaction manually (bypassing migration)
        interaction = Interaction(
            source_actor_id="https://mastodon.social/users/alice",
            target_resource="https://blog.example.com/post/1",
            interaction_type=InteractionType.REPLY,
            object_id="https://mastodon.social/users/alice/statuses/123",
        )
        storage.store_interaction(interaction)

        # Remove the index to simulate pre-v2 state
        index_path = storage._object_id_index_path(interaction.object_id)
        if index_path.exists():
            index_path.unlink()

        # Verify index is gone
        assert not index_path.exists()

        # Now run migrations
        storage._run_migrations()

        # Index should be recreated
        assert index_path.exists()

        # And lookup should work
        result = storage.get_interaction_by_object_id(
            "https://mastodon.social/users/alice/statuses/123"
        )
        assert result is not None
        assert result.source_actor_id == "https://mastodon.social/users/alice"

    def test_migration_skips_if_already_current(self, tmp_path):
        from pubby.storage.adapters.file._storage import SCHEMA_VERSION

        # Create storage (runs migration)
        storage = FileActivityPubStorage(data_dir=tmp_path)
        assert storage._get_schema_version() == SCHEMA_VERSION

        # Create another instance - should not re-run migration
        storage2 = FileActivityPubStorage(data_dir=tmp_path)
        assert storage2._get_schema_version() == SCHEMA_VERSION
