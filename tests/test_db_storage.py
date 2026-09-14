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

    def test_filter_followers_by_target_actor(self, storage):
        actor_a = "https://blog.example.com/ap/actor"
        actor_b = "https://blog.example.com/ap/other"

        storage.store_follower(
            Follower(
                actor_id="https://remote.example.com/users/alice",
                inbox="https://remote.example.com/users/alice/inbox",
                target_actor_id=actor_a,
            )
        )
        storage.store_follower(
            Follower(
                actor_id="https://remote.example.com/users/bob",
                inbox="https://remote.example.com/users/bob/inbox",
                target_actor_id=actor_b,
            )
        )

        a_followers = storage.get_followers(actor_id=actor_a)
        assert len(a_followers) == 1
        assert a_followers[0].actor_id == "https://remote.example.com/users/alice"

        all_followers = storage.get_followers()
        assert len(all_followers) == 2

    def test_get_followers_of_targets(self, storage):
        """Followers of a mix of actor and object targets are returned."""
        actor = "https://blog.example.com/users/nemo"
        obj = f"{actor}/objects/obj-1"
        other = "https://blog.example.com/users/fabio"

        storage.store_follower(
            Follower(
                actor_id="https://remote.example.com/users/alice",
                inbox="https://remote.example.com/users/alice/inbox",
                target_actor_id=actor,
            )
        )
        storage.store_follower(
            Follower(
                actor_id="https://remote.example.com/users/bob",
                inbox="https://remote.example.com/users/bob/inbox",
                target_actor_id=obj,
            )
        )
        storage.store_follower(
            Follower(
                actor_id="https://remote.example.com/users/carol",
                inbox="https://remote.example.com/users/carol/inbox",
                target_actor_id=other,
            )
        )
        # Unassigned legacy followers are not included.
        storage.store_follower(
            Follower(
                actor_id="https://remote.example.com/users/dan",
                inbox="https://remote.example.com/users/dan/inbox",
            )
        )

        followers = storage.get_followers_of_targets({actor, obj})
        assert {f.actor_id for f in followers} == {
            "https://remote.example.com/users/alice",
            "https://remote.example.com/users/bob",
        }
        assert (
            storage.get_followers_of_targets({"https://blog.example.com/unknown"}) == []
        )

    def test_same_remote_actor_can_follow_multiple_local_actors(self, storage):
        actor_a = "https://blog.example.com/ap/actor"
        actor_b = "https://blog.example.com/ap/other"
        remote = "https://remote.example.com/users/alice"

        storage.store_follower(
            Follower(
                actor_id=remote,
                inbox="https://remote.example.com/users/alice/inbox",
                target_actor_id=actor_a,
            )
        )
        storage.store_follower(
            Follower(
                actor_id=remote,
                inbox="https://remote.example.com/users/alice/inbox",
                target_actor_id=actor_b,
            )
        )

        assert len(storage.get_followers()) == 2
        assert len(storage.get_followers(actor_id=actor_a)) == 1
        assert len(storage.get_followers(actor_id=actor_b)) == 1

    def test_remove_follower_by_target_actor(self, storage):
        actor_a = "https://blog.example.com/ap/actor"
        actor_b = "https://blog.example.com/ap/other"
        remote = "https://remote.example.com/users/alice"

        storage.store_follower(
            Follower(
                actor_id=remote,
                inbox="https://remote.example.com/users/alice/inbox",
                target_actor_id=actor_a,
            )
        )
        storage.store_follower(
            Follower(
                actor_id=remote,
                inbox="https://remote.example.com/users/alice/inbox",
                target_actor_id=actor_b,
            )
        )

        storage.remove_follower(remote, target_actor_id=actor_a)

        assert len(storage.get_followers(actor_id=actor_a)) == 0
        assert len(storage.get_followers(actor_id=actor_b)) == 1

    def test_remove_follower_without_target_removes_all(self, storage, caplog):
        actor_a = "https://blog.example.com/ap/actor"
        actor_b = "https://blog.example.com/ap/other"
        remote = "https://remote.example.com/users/alice"

        storage.store_follower(
            Follower(
                actor_id=remote,
                inbox="https://remote.example.com/users/alice/inbox",
                target_actor_id=actor_a,
            )
        )
        storage.store_follower(
            Follower(
                actor_id=remote,
                inbox="https://remote.example.com/users/alice/inbox",
                target_actor_id=actor_b,
            )
        )

        with caplog.at_level("WARNING", logger="pubby.storage.adapters.db._storage"):
            storage.remove_follower(remote)

        assert len(storage.get_followers()) == 0
        assert "without target_actor_id" in caplog.text

    def test_unassigned_legacy_follower_visible_to_all_actors(self, storage):
        actor_a = "https://blog.example.com/ap/actor"
        actor_b = "https://blog.example.com/ap/other"

        storage.store_follower(
            Follower(
                actor_id="https://remote.example.com/users/alice",
                inbox="https://remote.example.com/users/alice/inbox",
                # no target_actor_id - legacy/unassigned
            )
        )

        a_followers = storage.get_followers(actor_id=actor_a)
        b_followers = storage.get_followers(actor_id=actor_b)

        assert len(a_followers) == 1
        assert len(b_followers) == 1
        assert a_followers[0].target_actor_id == ""


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


class TestMentionIndex:
    """Tests for the DB mention index (requires interaction_mention_model)."""

    @pytest.fixture
    def storage_with_mentions(self):
        """Create storage with mention model configured."""
        import sqlalchemy as sa
        from sqlalchemy.orm import declarative_base, sessionmaker

        from pubby.storage.adapters.db import (
            DbActivity,
            DbActorCache,
            DbActivityPubStorage,
            DbFollower,
            DbInteraction,
            DbInteractionMention,
        )

        Base = declarative_base()

        class Follower(Base, DbFollower):
            __tablename__ = "followers"

        class InteractionModel(Base, DbInteraction):
            __tablename__ = "interactions"

        class Activity(Base, DbActivity):
            __tablename__ = "activities"

        class ActorCache(Base, DbActorCache):
            __tablename__ = "actor_cache"

        class InteractionMention(Base, DbInteractionMention):
            __tablename__ = "interaction_mentions"

        engine = sa.create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)

        return DbActivityPubStorage(
            engine=engine,
            follower_model=Follower,
            interaction_model=InteractionModel,
            activity_model=Activity,
            actor_cache_model=ActorCache,
            interaction_mention_model=InteractionMention,
            session_factory=session_factory,
        )

    def test_store_with_mentions(self, storage_with_mentions):
        now = datetime.now(timezone.utc)
        interaction = Interaction(
            source_actor_id="https://remote.example.com/users/alice",
            target_resource="https://other.example.com/posts/1",
            interaction_type=InteractionType.REPLY,
            content="@blog Hello!",
            mentioned_actors=["https://blog.example.com/ap/actor"],
            created_at=now,
            updated_at=now,
        )
        storage_with_mentions.store_interaction(interaction)

        results = storage_with_mentions.get_interactions_mentioning(
            "https://blog.example.com/ap/actor"
        )
        assert len(results) == 1
        assert results[0].source_actor_id == "https://remote.example.com/users/alice"
        assert "https://blog.example.com/ap/actor" in results[0].mentioned_actors

    def test_get_interactions_mentioning_empty(self, storage_with_mentions):
        results = storage_with_mentions.get_interactions_mentioning(
            "https://blog.example.com/ap/actor"
        )
        assert results == []

    def test_get_interactions_mentioning_filter_by_type(self, storage_with_mentions):
        now = datetime.now(timezone.utc)
        storage_with_mentions.store_interaction(
            Interaction(
                source_actor_id="https://remote.example.com/users/alice",
                target_resource="https://other.example.com/posts/1",
                interaction_type=InteractionType.REPLY,
                mentioned_actors=["https://blog.example.com/ap/actor"],
                created_at=now,
                updated_at=now,
            )
        )
        storage_with_mentions.store_interaction(
            Interaction(
                source_actor_id="https://remote.example.com/users/bob",
                target_resource="https://other.example.com/posts/2",
                interaction_type=InteractionType.MENTION,
                mentioned_actors=["https://blog.example.com/ap/actor"],
                created_at=now,
                updated_at=now,
            )
        )

        replies = storage_with_mentions.get_interactions_mentioning(
            "https://blog.example.com/ap/actor",
            interaction_type=InteractionType.REPLY,
        )
        assert len(replies) == 1
        assert replies[0].interaction_type == InteractionType.REPLY

    def test_multiple_mentions(self, storage_with_mentions):
        now = datetime.now(timezone.utc)
        storage_with_mentions.store_interaction(
            Interaction(
                source_actor_id="https://remote.example.com/users/alice",
                target_resource="https://other.example.com/posts/1",
                interaction_type=InteractionType.REPLY,
                mentioned_actors=[
                    "https://blog.example.com/ap/actor",
                    "https://other.example.com/ap/actor",
                ],
                created_at=now,
                updated_at=now,
            )
        )

        results1 = storage_with_mentions.get_interactions_mentioning(
            "https://blog.example.com/ap/actor"
        )
        results2 = storage_with_mentions.get_interactions_mentioning(
            "https://other.example.com/ap/actor"
        )

        assert len(results1) == 1
        assert len(results2) == 1

    def test_without_mention_model_returns_empty(self, storage):
        """Storage without mention model should return empty list."""
        results = storage.get_interactions_mentioning(
            "https://blog.example.com/ap/actor"
        )
        assert results == []

    def test_store_mentions_idempotent(self, storage_with_mentions):
        """Storing the same interaction twice should not create duplicate mentions."""
        now = datetime.now(timezone.utc)
        interaction = Interaction(
            source_actor_id="https://remote.example.com/users/alice",
            target_resource="https://other.example.com/posts/1",
            interaction_type=InteractionType.REPLY,
            mentioned_actors=["https://blog.example.com/ap/actor"],
            created_at=now,
            updated_at=now,
        )

        # Store twice
        storage_with_mentions.store_interaction(interaction)
        storage_with_mentions.store_interaction(interaction)

        results = storage_with_mentions.get_interactions_mentioning(
            "https://blog.example.com/ap/actor"
        )
        assert len(results) == 1

    def test_multiple_replies_from_same_actor(self, storage):
        """Multiple replies from the same actor to the same target are all stored."""
        now = datetime.now(timezone.utc)
        actor = "https://mastodon.social/users/alice"
        target = "https://blog.example.com/post/1"

        reply1 = Interaction(
            source_actor_id=actor,
            target_resource=target,
            interaction_type=InteractionType.REPLY,
            object_id="https://mastodon.social/users/alice/statuses/111",
            content="First reply",
            created_at=now,
            updated_at=now,
        )
        reply2 = Interaction(
            source_actor_id=actor,
            target_resource=target,
            interaction_type=InteractionType.REPLY,
            object_id="https://mastodon.social/users/alice/statuses/222",
            content="Second reply",
            created_at=now,
            updated_at=now,
        )
        reply3 = Interaction(
            source_actor_id=actor,
            target_resource=target,
            interaction_type=InteractionType.REPLY,
            object_id="https://mastodon.social/users/alice/statuses/333",
            content="Third reply",
            created_at=now,
            updated_at=now,
        )

        storage.store_interaction(reply1)
        storage.store_interaction(reply2)
        storage.store_interaction(reply3)

        interactions = storage.get_interactions(target_resource=target)
        assert len(interactions) == 3

        contents = {i.content for i in interactions}
        assert contents == {"First reply", "Second reply", "Third reply"}

    def test_like_still_one_per_actor(self, storage):
        """Likes from the same actor to the same target overwrite (one per actor)."""
        now = datetime.now(timezone.utc)
        actor = "https://mastodon.social/users/alice"
        target = "https://blog.example.com/post/1"

        like1 = Interaction(
            source_actor_id=actor,
            target_resource=target,
            interaction_type=InteractionType.LIKE,
            activity_id="activity1",
            created_at=now,
            updated_at=now,
        )
        like2 = Interaction(
            source_actor_id=actor,
            target_resource=target,
            interaction_type=InteractionType.LIKE,
            activity_id="activity2",
            created_at=now,
            updated_at=now,
        )

        storage.store_interaction(like1)
        storage.store_interaction(like2)

        interactions = storage.get_interactions(
            target_resource=target, interaction_type=InteractionType.LIKE
        )
        # Both likes have object_id="" so they collide on the unique constraint
        assert len(interactions) == 1
        assert interactions[0].activity_id == "activity2"
