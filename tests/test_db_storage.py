"""
Tests for SQLAlchemy database storage CRUD operations (in-memory SQLite).
"""

from datetime import datetime, timezone

import pytest

from pubby._model import (
    Follower,
    Interaction,
    InteractionStatus,
    InteractionType,
)
from pubby.storage.adapters.db import init_db_storage


@pytest.fixture
def storage():
    return init_db_storage("sqlite:///:memory:")


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
        storage.store_follower(
            Follower(
                actor_id="https://mastodon.social/users/alice",
                inbox="https://mastodon.social/users/alice/inbox",
            )
        )
        assert len(storage.get_followers()) == 1

        storage.remove_follower("https://mastodon.social/users/alice")
        assert len(storage.get_followers()) == 0

    def test_update_follower(self, storage):
        storage.store_follower(
            Follower(
                actor_id="https://mastodon.social/users/alice",
                inbox="https://mastodon.social/users/alice/inbox",
                actor_data={"name": "Alice"},
            )
        )

        # Store again — should update
        storage.store_follower(
            Follower(
                actor_id="https://mastodon.social/users/alice",
                inbox="https://mastodon.social/users/alice/inbox",
                shared_inbox="https://mastodon.social/inbox",
                actor_data={"name": "Alice Updated"},
            )
        )

        followers = storage.get_followers()
        assert len(followers) == 1
        assert followers[0].actor_data == {"name": "Alice Updated"}
        assert followers[0].shared_inbox == "https://mastodon.social/inbox"

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
        now = datetime.now(timezone.utc)
        interaction = Interaction(
            source_actor_id="https://mastodon.social/users/alice",
            target_resource="https://blog.example.com/post/1",
            interaction_type=InteractionType.REPLY,
            content="<p>Great post!</p>",
            author_name="Alice",
            published=now,
            status=InteractionStatus.CONFIRMED,
            created_at=now,
            updated_at=now,
        )

        storage.store_interaction(interaction)
        interactions = storage.get_interactions("https://blog.example.com/post/1")

        assert len(interactions) == 1
        assert interactions[0].content == "<p>Great post!</p>"
        assert interactions[0].interaction_type == InteractionType.REPLY
        assert interactions[0].author_name == "Alice"

    def test_filter_by_type(self, storage):
        now = datetime.now(timezone.utc)
        storage.store_interaction(
            Interaction(
                source_actor_id="https://example.com/users/alice",
                target_resource="https://blog.example.com/post/1",
                interaction_type=InteractionType.REPLY,
                content="Reply",
                created_at=now,
                updated_at=now,
            )
        )
        storage.store_interaction(
            Interaction(
                source_actor_id="https://example.com/users/bob",
                target_resource="https://blog.example.com/post/1",
                interaction_type=InteractionType.LIKE,
                created_at=now,
                updated_at=now,
            )
        )

        replies = storage.get_interactions(
            "https://blog.example.com/post/1",
            interaction_type=InteractionType.REPLY,
        )
        assert len(replies) == 1

        likes = storage.get_interactions(
            "https://blog.example.com/post/1",
            interaction_type=InteractionType.LIKE,
        )
        assert len(likes) == 1

    def test_delete_interaction(self, storage):
        now = datetime.now(timezone.utc)
        storage.store_interaction(
            Interaction(
                source_actor_id="https://example.com/users/alice",
                target_resource="https://blog.example.com/post/1",
                interaction_type=InteractionType.LIKE,
                created_at=now,
                updated_at=now,
            )
        )

        storage.delete_interaction(
            "https://example.com/users/alice",
            "https://blog.example.com/post/1",
            InteractionType.LIKE,
        )

        confirmed = storage.get_interactions("https://blog.example.com/post/1")
        assert len(confirmed) == 0

        deleted = storage.get_interactions(
            "https://blog.example.com/post/1",
            status=InteractionStatus.DELETED,
        )
        assert len(deleted) == 1

    def test_update_interaction(self, storage):
        now = datetime.now(timezone.utc)
        storage.store_interaction(
            Interaction(
                source_actor_id="https://example.com/users/alice",
                target_resource="https://blog.example.com/post/1",
                interaction_type=InteractionType.REPLY,
                content="Original",
                created_at=now,
                updated_at=now,
            )
        )

        storage.store_interaction(
            Interaction(
                source_actor_id="https://example.com/users/alice",
                target_resource="https://blog.example.com/post/1",
                interaction_type=InteractionType.REPLY,
                content="Updated",
                created_at=now,
                updated_at=now,
            )
        )

        interactions = storage.get_interactions("https://blog.example.com/post/1")
        assert len(interactions) == 1
        assert interactions[0].content == "Updated"


class TestActivities:
    def test_store_and_get(self, storage):
        storage.store_activity("act-1", {"id": "act-1", "type": "Create"})
        storage.store_activity("act-2", {"id": "act-2", "type": "Update"})

        activities = storage.get_activities()
        assert len(activities) == 2

    def test_pagination(self, storage):
        for i in range(10):
            storage.store_activity(f"act-{i}", {"id": f"act-{i}", "type": "Create"})

        page1 = storage.get_activities(limit=3, offset=0)
        assert len(page1) == 3

        page2 = storage.get_activities(limit=3, offset=3)
        assert len(page2) == 3

    def test_update_activity(self, storage):
        storage.store_activity("act-1", {"id": "act-1", "version": 1})
        storage.store_activity("act-1", {"id": "act-1", "version": 2})

        activities = storage.get_activities()
        assert len(activities) == 1
        assert activities[0]["version"] == 2


class TestActorCache:
    def test_cache_and_retrieve(self, storage):
        storage.cache_remote_actor(
            "https://mastodon.social/users/alice",
            {"name": "Alice", "type": "Person"},
        )

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

        cached = storage.get_cached_actor("https://mastodon.social/users/alice")
        assert cached is None

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
        assert cached is not None
        assert cached["name"] == "Alice Updated"

    def test_cache_miss(self, storage):
        assert storage.get_cached_actor("https://nonexistent.example.com") is None
