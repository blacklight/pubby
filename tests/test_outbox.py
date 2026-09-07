"""
Tests for outbox processing — publish, fan-out delivery, retry logic.
"""

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from pubby._model import Follower, Object
from pubby.handlers._outbox import (
    AS_PUBLIC,
    OutboxProcessor,
    build_announce_activity,
    build_delete_activity,
    build_like_activity,
    build_undo_activity,
    collect_inboxes,
    deliver_activity,
)


@pytest.fixture
def mock_storage():
    storage = MagicMock()
    storage.get_followers.return_value = []
    storage.get_activities.return_value = []
    return storage


@pytest.fixture
def outbox_processor(mock_storage, private_key):
    return OutboxProcessor(
        storage=mock_storage,
        actor_id="https://blog.example.com/ap/actor",
        private_key=private_key,
        key_id="https://blog.example.com/ap/actor#main-key",
        followers_collection_url="https://blog.example.com/ap/followers",
        max_retries=2,
        retry_base_delay=0.01,  # Very short for tests
        async_delivery=False,  # Synchronous for test assertions
    )


class TestBuildActivities:
    def test_build_create_activity(self, outbox_processor):
        obj = Object(
            id="https://blog.example.com/post/1",
            type="Article",
            name="My Post",
            content="<p>Hello world</p>",
            attributed_to="https://blog.example.com/ap/actor",
            url="https://blog.example.com/post/1",
            published=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )

        activity = outbox_processor.build_create_activity(obj)
        assert activity["type"] == "Create"
        assert activity["actor"] == "https://blog.example.com/ap/actor"
        assert activity["object"]["type"] == "Article"
        assert activity["object"]["name"] == "My Post"
        assert AS_PUBLIC in activity["to"]

    def test_build_update_activity(self, outbox_processor):
        obj = Object(
            id="https://blog.example.com/post/1",
            type="Article",
            name="Updated Post",
            content="<p>Updated content</p>",
            attributed_to="https://blog.example.com/ap/actor",
        )

        activity = outbox_processor.build_update_activity(obj)
        assert activity["type"] == "Update"
        assert activity["object"]["name"] == "Updated Post"

    def test_build_delete_activity(self, outbox_processor):
        activity = outbox_processor.build_delete_activity(
            "https://blog.example.com/post/1"
        )
        assert activity["type"] == "Delete"
        assert activity["actor"] == "https://blog.example.com/ap/actor"
        assert activity["object"]["type"] == "Tombstone"
        assert activity["object"]["id"] == "https://blog.example.com/post/1"
        assert AS_PUBLIC in activity["to"]
        assert "https://blog.example.com/ap/followers" in activity["cc"]
        assert "published" in activity
        assert "id" in activity

    def test_build_delete_activity_no_followers_url(self, mock_storage, private_key):
        processor = OutboxProcessor(
            storage=mock_storage,
            actor_id="https://blog.example.com/ap/actor",
            private_key=private_key,
            key_id="https://blog.example.com/ap/actor#main-key",
            followers_collection_url="",
        )
        activity = processor.build_delete_activity("https://blog.example.com/post/1")
        assert activity["cc"] == []
        assert AS_PUBLIC in activity["to"]

    @patch("pubby.handlers._outbox.build_delete_activity")
    def test_build_delete_activity_delegates(self, mock_build_delete, outbox_processor):
        from pubby._model import AP_CONTEXT

        outbox_processor.build_delete_activity("https://blog.example.com/post/1")
        mock_build_delete.assert_called_once_with(
            actor_id="https://blog.example.com/ap/actor",
            object_id="https://blog.example.com/post/1",
            cc=["https://blog.example.com/ap/followers"],
            context=AP_CONTEXT,
        )

    def test_build_like_activity(self, outbox_processor):
        activity = outbox_processor.build_like_activity(
            "https://remote.example.com/post/42"
        )
        assert activity["type"] == "Like"
        assert activity["actor"] == "https://blog.example.com/ap/actor"
        assert activity["object"] == "https://remote.example.com/post/42"
        assert AS_PUBLIC in activity["to"]
        assert "https://blog.example.com/ap/followers" in activity["cc"]
        assert "published" in activity
        assert "id" in activity

    def test_build_like_activity_with_explicit_id(self, outbox_processor):
        activity = outbox_processor.build_like_activity(
            "https://remote.example.com/post/42",
            activity_id="https://blog.example.com/reply/my-like#like",
        )
        assert activity["id"] == "https://blog.example.com/reply/my-like#like"

    def test_build_like_activity_with_published(self, outbox_processor):
        ts = datetime(2025, 3, 14, 12, 0, 0, tzinfo=timezone.utc)
        activity = outbox_processor.build_like_activity(
            "https://remote.example.com/post/42",
            published=ts,
        )
        assert activity["published"] == ts.isoformat()

    def test_build_like_activity_no_followers_url(self, mock_storage, private_key):
        processor = OutboxProcessor(
            storage=mock_storage,
            actor_id="https://blog.example.com/ap/actor",
            private_key=private_key,
            key_id="https://blog.example.com/ap/actor#main-key",
            followers_collection_url="",
        )
        activity = processor.build_like_activity("https://remote.example.com/post/1")
        assert activity["cc"] == []

    def test_build_undo_activity(self, outbox_processor):
        like = outbox_processor.build_like_activity(
            "https://remote.example.com/post/42"
        )
        undo = outbox_processor.build_undo_activity(like)

        assert undo["type"] == "Undo"
        assert undo["actor"] == "https://blog.example.com/ap/actor"
        assert undo["object"] is like
        assert undo["to"] == like["to"]
        assert undo["cc"] == like["cc"]
        assert "published" in undo
        assert undo["id"] != like["id"]

    def test_build_undo_activity_inherits_addressing(self, outbox_processor):
        inner = {
            "id": "https://blog.example.com/activities/123",
            "type": "Like",
            "actor": "https://blog.example.com/ap/actor",
            "object": "https://remote.example.com/post/1",
            "to": ["https://specific.example.com/users/alice"],
            "cc": ["https://blog.example.com/ap/followers"],
        }
        undo = outbox_processor.build_undo_activity(inner)
        assert undo["to"] == ["https://specific.example.com/users/alice"]
        assert undo["cc"] == ["https://blog.example.com/ap/followers"]

    def test_build_undo_activity_defaults_addressing(self, outbox_processor):
        inner = {
            "id": "https://blog.example.com/activities/123",
            "type": "Like",
            "actor": "https://blog.example.com/ap/actor",
            "object": "https://remote.example.com/post/1",
        }
        undo = outbox_processor.build_undo_activity(inner)
        assert AS_PUBLIC in undo["to"]
        assert undo["cc"] == []

    def test_build_announce_activity(self, outbox_processor):
        activity = outbox_processor.build_announce_activity(
            "https://remote.example.com/post/42"
        )
        assert activity["type"] == "Announce"
        assert activity["actor"] == "https://blog.example.com/ap/actor"
        assert activity["object"] == "https://remote.example.com/post/42"
        assert AS_PUBLIC in activity["to"]
        assert "https://blog.example.com/ap/followers" in activity["cc"]
        assert "published" in activity
        assert "id" in activity

    def test_build_announce_activity_with_explicit_id(self, outbox_processor):
        activity = outbox_processor.build_announce_activity(
            "https://remote.example.com/post/42",
            activity_id="https://blog.example.com/reply/my-boost#boost",
        )
        assert activity["id"] == "https://blog.example.com/reply/my-boost#boost"

    def test_build_announce_activity_with_published(self, outbox_processor):
        ts = datetime(2025, 3, 14, 12, 0, 0, tzinfo=timezone.utc)
        activity = outbox_processor.build_announce_activity(
            "https://remote.example.com/post/42",
            published=ts,
        )
        assert activity["published"] == ts.isoformat()

    def test_build_announce_activity_no_followers_url(self, mock_storage, private_key):
        processor = OutboxProcessor(
            storage=mock_storage,
            actor_id="https://blog.example.com/ap/actor",
            private_key=private_key,
            key_id="https://blog.example.com/ap/actor#main-key",
            followers_collection_url="",
        )
        activity = processor.build_announce_activity(
            "https://remote.example.com/post/1"
        )
        assert activity["cc"] == []

    def test_build_undo_announce_activity(self, outbox_processor):
        boost = outbox_processor.build_announce_activity(
            "https://remote.example.com/post/42"
        )
        undo = outbox_processor.build_undo_activity(boost)

        assert undo["type"] == "Undo"
        assert undo["object"] is boost
        assert undo["object"]["type"] == "Announce"
        assert undo["to"] == boost["to"]
        assert undo["cc"] == boost["cc"]


class TestFreeBuildActivities:
    """Tests for the module-level activity builders."""

    def test_build_like_activity_defaults(self):
        activity = build_like_activity(
            actor_id="https://blog.example.com/ap/actor",
            object_id="https://remote.example.com/post/42",
        )
        assert activity["type"] == "Like"
        assert activity["actor"] == "https://blog.example.com/ap/actor"
        assert activity["object"] == "https://remote.example.com/post/42"
        assert AS_PUBLIC in activity["to"]
        assert activity["cc"] == []
        assert "published" in activity
        assert "id" in activity
        assert activity["id"].startswith(
            "https://blog.example.com/ap/actor/activities/"
        )

    def test_build_like_activity_explicit_audience(self):
        activity = build_like_activity(
            actor_id="https://blog.example.com/ap/actor",
            object_id="https://remote.example.com/post/42",
            to=["https://remote.example.com/users/bob"],
            cc=["https://blog.example.com/ap/followers"],
        )
        assert activity["to"] == ["https://remote.example.com/users/bob"]
        assert activity["cc"] == ["https://blog.example.com/ap/followers"]

    def test_build_like_activity_explicit_id_and_published(self):
        ts = datetime(2025, 3, 14, 12, 0, 0, tzinfo=timezone.utc)
        activity = build_like_activity(
            actor_id="https://blog.example.com/ap/actor",
            object_id="https://remote.example.com/post/42",
            activity_id="https://blog.example.com/activities/like-1",
            published=ts,
        )
        assert activity["id"] == "https://blog.example.com/activities/like-1"
        assert activity["published"] == ts.isoformat()

    def test_build_like_activity_published_string(self):
        activity = build_like_activity(
            actor_id="https://blog.example.com/ap/actor",
            object_id="https://remote.example.com/post/42",
            published="2025-03-14T12:00:00+00:00",
        )
        assert activity["published"] == "2025-03-14T12:00:00+00:00"

    def test_build_like_activity_context_override(self):
        activity = build_like_activity(
            actor_id="https://blog.example.com/ap/actor",
            object_id="https://remote.example.com/post/42",
            context=["https://www.w3.org/ns/activitystreams"],
        )
        assert activity["@context"] == ["https://www.w3.org/ns/activitystreams"]

    def test_build_announce_activity_defaults(self):
        activity = build_announce_activity(
            actor_id="https://blog.example.com/ap/actor",
            object_id="https://remote.example.com/post/42",
        )
        assert activity["type"] == "Announce"
        assert activity["actor"] == "https://blog.example.com/ap/actor"
        assert activity["object"] == "https://remote.example.com/post/42"
        assert AS_PUBLIC in activity["to"]
        assert activity["cc"] == []

    def test_build_announce_activity_with_followers_cc(self):
        activity = build_announce_activity(
            actor_id="https://blog.example.com/ap/actor",
            object_id="https://remote.example.com/post/42",
            cc=["https://blog.example.com/ap/followers"],
        )
        assert activity["cc"] == ["https://blog.example.com/ap/followers"]

    def test_build_undo_activity_inherits_addressing(self):
        inner = build_like_activity(
            actor_id="https://blog.example.com/ap/actor",
            object_id="https://remote.example.com/post/1",
            to=["https://specific.example.com/users/alice"],
            cc=["https://blog.example.com/ap/followers"],
        )
        undo = build_undo_activity(
            inner,
            actor_id="https://blog.example.com/ap/actor",
        )
        assert undo["type"] == "Undo"
        assert undo["actor"] == "https://blog.example.com/ap/actor"
        assert undo["object"] is inner
        assert undo["to"] == ["https://specific.example.com/users/alice"]
        assert undo["cc"] == ["https://blog.example.com/ap/followers"]

    def test_build_undo_activity_defaults_addressing(self):
        inner = {
            "id": "https://blog.example.com/activities/123",
            "type": "Like",
            "actor": "https://blog.example.com/ap/actor",
            "object": "https://remote.example.com/post/1",
        }
        undo = build_undo_activity(
            inner,
            actor_id="https://blog.example.com/ap/actor",
        )
        assert AS_PUBLIC in undo["to"]
        assert undo["cc"] == []

    def test_build_undo_activity_overrides_addressing(self):
        inner = build_like_activity(
            actor_id="https://blog.example.com/ap/actor",
            object_id="https://remote.example.com/post/1",
            to=["https://remote.example.com/users/alice"],
            cc=["https://blog.example.com/ap/followers"],
        )
        undo = build_undo_activity(
            inner,
            actor_id="https://blog.example.com/ap/actor",
            to=[AS_PUBLIC],
            cc=[],
        )
        assert undo["to"] == [AS_PUBLIC]
        assert undo["cc"] == []

    def test_build_delete_activity_defaults(self):
        activity = build_delete_activity(
            actor_id="https://blog.example.com/ap/actor",
            object_id="https://blog.example.com/post/1",
        )
        assert activity["type"] == "Delete"
        assert activity["actor"] == "https://blog.example.com/ap/actor"
        assert activity["object"]["type"] == "Tombstone"
        assert activity["object"]["id"] == "https://blog.example.com/post/1"
        assert AS_PUBLIC in activity["to"]
        assert activity["cc"] == ["https://blog.example.com/ap/actor/followers"]
        assert "published" in activity
        assert "id" in activity
        assert activity["id"].startswith(
            "https://blog.example.com/ap/actor/activities/"
        )

    def test_build_delete_activity_explicit_id_and_published(self):
        ts = datetime(2025, 3, 14, 12, 0, 0, tzinfo=timezone.utc)
        activity = build_delete_activity(
            actor_id="https://blog.example.com/ap/actor",
            object_id="https://blog.example.com/post/1",
            activity_id="https://blog.example.com/activities/delete-1",
            published=ts,
        )
        assert activity["id"] == "https://blog.example.com/activities/delete-1"
        assert activity["published"] == ts.isoformat()

    def test_build_delete_activity_explicit_audience(self):
        activity = build_delete_activity(
            actor_id="https://blog.example.com/ap/actor",
            object_id="https://blog.example.com/post/1",
            to=["https://remote.example.com/users/bob"],
            cc=["https://specific.example.com/users/alice"],
        )
        assert activity["to"] == ["https://remote.example.com/users/bob"]
        assert activity["cc"] == ["https://specific.example.com/users/alice"]

    def test_build_delete_activity_context_override(self):
        activity = build_delete_activity(
            actor_id="https://blog.example.com/ap/actor",
            object_id="https://blog.example.com/post/1",
            context="https://www.w3.org/ns/activitystreams",
        )
        assert activity["@context"] == "https://www.w3.org/ns/activitystreams"


class TestCollectInboxes:
    def test_shared_inbox_dedup(self, outbox_processor):
        followers = [
            Follower(
                actor_id="https://mastodon.social/users/alice",
                inbox="https://mastodon.social/users/alice/inbox",
                shared_inbox="https://mastodon.social/inbox",
            ),
            Follower(
                actor_id="https://mastodon.social/users/bob",
                inbox="https://mastodon.social/users/bob/inbox",
                shared_inbox="https://mastodon.social/inbox",
            ),
            Follower(
                actor_id="https://other.example.com/users/carol",
                inbox="https://other.example.com/users/carol/inbox",
                shared_inbox="",
            ),
        ]

        inboxes = outbox_processor._collect_inboxes(followers)
        # Should deduplicate shared inboxes
        assert len(inboxes) == 2
        assert "https://mastodon.social/inbox" in inboxes
        assert "https://other.example.com/users/carol/inbox" in inboxes

    def test_empty_followers(self, outbox_processor):
        assert outbox_processor._collect_inboxes([]) == []


class TestDelivery:
    @patch("pubby.handlers._outbox.sign_request")
    @patch("pubby.handlers._outbox.requests")
    def test_delivery_signs_content_headers(
        self, mock_requests, mock_sign_request, outbox_processor, mock_storage
    ):
        mock_storage.get_followers.return_value = [
            Follower(
                actor_id="https://remote.example.com/users/alice",
                inbox="https://remote.example.com/users/alice/inbox",
            ),
        ]

        mock_sign_request.return_value = {
            "Signature": "sig",
            "Date": "now",
            "Host": "remote.example.com",
            "Digest": "SHA-256=...",
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_requests.post.return_value = mock_resp

        obj = Object(
            id="https://blog.example.com/post/1",
            type="Article",
            content="<p>Test</p>",
            attributed_to="https://blog.example.com/ap/actor",
        )

        activity = outbox_processor.build_create_activity(obj)
        outbox_processor.publish(activity)

        mock_sign_request.assert_called_once()
        kwargs = mock_sign_request.call_args.kwargs
        assert "signed_headers" in kwargs
        assert "content-type" in [h.lower() for h in kwargs["signed_headers"]]
        assert "content-length" in [h.lower() for h in kwargs["signed_headers"]]
        assert kwargs["headers"]["Content-Type"] == "application/activity+json"
        assert "Content-Length" in kwargs["headers"]

    @patch("pubby.handlers._outbox.requests")
    def test_publish_delivers_to_followers(
        self, mock_requests, outbox_processor, mock_storage
    ):
        mock_storage.get_followers.return_value = [
            Follower(
                actor_id="https://remote.example.com/users/alice",
                inbox="https://remote.example.com/users/alice/inbox",
            ),
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_requests.post.return_value = mock_resp

        obj = Object(
            id="https://blog.example.com/post/1",
            type="Article",
            name="Test",
            content="<p>Test</p>",
            attributed_to="https://blog.example.com/ap/actor",
        )

        activity = outbox_processor.build_create_activity(obj)
        outbox_processor.publish(activity)

        # Should store the activity
        mock_storage.store_activity.assert_called_once()

        # Should deliver to the follower inbox
        mock_requests.post.assert_called_once()
        call_kwargs = mock_requests.post.call_args
        assert "remote.example.com" in call_kwargs[0][0] or "remote.example.com" in str(
            call_kwargs
        )

    @patch("pubby.handlers._outbox.requests")
    def test_delivery_retries_on_5xx(
        self, mock_requests, outbox_processor, mock_storage
    ):
        mock_storage.get_followers.return_value = [
            Follower(
                actor_id="https://remote.example.com/users/alice",
                inbox="https://remote.example.com/users/alice/inbox",
            ),
        ]

        # First attempt: 500, second attempt: 202
        mock_resp_500 = MagicMock()
        mock_resp_500.status_code = 500
        mock_resp_500.text = "Internal Server Error"

        mock_resp_202 = MagicMock()
        mock_resp_202.status_code = 202

        mock_requests.post.side_effect = [mock_resp_500, mock_resp_202]

        obj = Object(
            id="https://blog.example.com/post/1",
            type="Article",
            content="<p>Test</p>",
            attributed_to="https://blog.example.com/ap/actor",
        )

        activity = outbox_processor.build_create_activity(obj)
        outbox_processor.publish(activity)

        # Should have retried
        assert mock_requests.post.call_count == 2

    @patch("pubby.handlers._outbox.requests")
    def test_delivery_gives_up_after_max_retries(
        self, mock_requests, outbox_processor, mock_storage
    ):
        mock_storage.get_followers.return_value = [
            Follower(
                actor_id="https://remote.example.com/users/alice",
                inbox="https://remote.example.com/users/alice/inbox",
            ),
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_requests.post.return_value = mock_resp

        obj = Object(
            id="https://blog.example.com/post/1",
            type="Article",
            content="<p>Test</p>",
            attributed_to="https://blog.example.com/ap/actor",
        )

        activity = outbox_processor.build_create_activity(obj)
        outbox_processor.publish(activity)

        # Should exhaust all retries
        assert mock_requests.post.call_count == 2  # max_retries=2


class TestOutboxCollection:
    def test_get_outbox_collection(self, outbox_processor, mock_storage):
        mock_storage.get_activities.return_value = [
            {"id": "act-1", "type": "Create"},
            {"id": "act-2", "type": "Create"},
        ]

        collection = outbox_processor.get_outbox_collection(
            "https://blog.example.com/ap/outbox"
        )

        assert collection["type"] == "OrderedCollection"
        assert collection["totalItems"] == 2
        assert len(collection["orderedItems"]) == 2


class TestConcurrentDelivery:
    @patch("pubby.handlers._outbox.requests")
    def test_fanout_is_concurrent(self, mock_requests, mock_storage, private_key):
        """Verify deliveries run in parallel, not sequentially."""
        num_followers = 5
        sleep_per_delivery = 0.2

        mock_storage.get_followers.return_value = [
            Follower(
                actor_id=f"https://remote.example.com/users/user{i}",
                inbox=f"https://remote{i}.example.com/inbox",
            )
            for i in range(num_followers)
        ]

        def slow_post(*_, **__):
            time.sleep(sleep_per_delivery)
            resp = MagicMock()
            resp.status_code = 202
            return resp

        mock_requests.post.side_effect = slow_post

        processor = OutboxProcessor(
            storage=mock_storage,
            actor_id="https://blog.example.com/ap/actor",
            private_key=private_key,
            key_id="https://blog.example.com/ap/actor#main-key",
            max_retries=1,
            max_delivery_workers=num_followers,
            async_delivery=False,  # Synchronous for test assertions
        )

        activity = processor.build_create_activity(
            Object(
                id="https://blog.example.com/post/1",
                type="Article",
                content="<p>Test</p>",
                attributed_to="https://blog.example.com/ap/actor",
            )
        )

        start = time.monotonic()
        processor.publish(activity)
        elapsed = time.monotonic() - start

        assert mock_requests.post.call_count == num_followers
        # Sequential would take ~1.0s; concurrent should be ~0.2s
        sequential_time = sleep_per_delivery * num_followers
        assert elapsed < sequential_time * 0.6, (
            f"Delivery took {elapsed:.2f}s, expected < {sequential_time * 0.6:.2f}s "
            f"(sequential would be ~{sequential_time:.2f}s)"
        )


class TestMentionDelivery:
    """Tests for delivering activities to mentioned actors (CC'd users)."""

    def test_is_actor_url_filters_public(self, outbox_processor):
        """AS_PUBLIC should not be considered an actor URL."""
        assert not outbox_processor._is_actor_url(AS_PUBLIC)

    def test_is_actor_url_filters_collections(self, outbox_processor):
        """Collection URLs (followers, following, etc.) should be filtered."""
        assert not outbox_processor._is_actor_url("https://example.com/ap/followers")
        assert not outbox_processor._is_actor_url(
            "https://example.com/users/alice/following"
        )
        assert not outbox_processor._is_actor_url("https://example.com/ap/outbox")
        assert not outbox_processor._is_actor_url("https://example.com/users/bob/inbox")

    def test_is_actor_url_accepts_actor_urls(self, outbox_processor):
        """Valid actor URLs should be accepted."""
        assert outbox_processor._is_actor_url("https://mastodon.social/users/alice")
        assert outbox_processor._is_actor_url("https://example.com/@bob")
        assert outbox_processor._is_actor_url("https://remote.example.com/ap/actor")

    def test_is_actor_url_filters_own_followers(self, outbox_processor):
        """Our own followers collection URL should be filtered."""
        assert not outbox_processor._is_actor_url(
            "https://blog.example.com/ap/followers"
        )

    def test_extract_recipient_actors_from_cc(self, outbox_processor):
        """Should extract actor URLs from CC field."""
        activity = {
            "to": [AS_PUBLIC],
            "cc": [
                "https://blog.example.com/ap/followers",
                "https://mastodon.social/users/alice",
                "https://remote.example.com/users/bob",
            ],
        }
        actors = outbox_processor._extract_recipient_actors(activity)
        assert len(actors) == 2
        assert "https://mastodon.social/users/alice" in actors
        assert "https://remote.example.com/users/bob" in actors

    def test_extract_recipient_actors_from_to(self, outbox_processor):
        """Should extract actor URLs from to field (direct messages)."""
        activity = {
            "to": ["https://mastodon.social/users/alice"],
            "cc": [],
        }
        actors = outbox_processor._extract_recipient_actors(activity)
        assert actors == ["https://mastodon.social/users/alice"]

    def test_extract_recipient_actors_deduplicates(self, outbox_processor):
        """Should not return duplicates if same actor in to and cc."""
        activity = {
            "to": ["https://mastodon.social/users/alice"],
            "cc": ["https://mastodon.social/users/alice"],
        }
        actors = outbox_processor._extract_recipient_actors(activity)
        assert actors == ["https://mastodon.social/users/alice"]

    def test_extract_recipient_actors_handles_string_values(self, outbox_processor):
        """Should handle to/cc being single strings instead of lists."""
        activity = {
            "to": AS_PUBLIC,
            "cc": "https://mastodon.social/users/alice",
        }
        actors = outbox_processor._extract_recipient_actors(activity)
        assert actors == ["https://mastodon.social/users/alice"]

    @patch("pubby.handlers._outbox.requests")
    def test_collect_recipient_inboxes_fetches_actors(
        self, mock_requests, outbox_processor, mock_storage
    ):
        """Should fetch actor documents and extract inbox URLs."""
        mock_storage.get_cached_actor.return_value = None

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "https://mastodon.social/users/alice",
            "type": "Person",
            "inbox": "https://mastodon.social/users/alice/inbox",
            "endpoints": {"sharedInbox": "https://mastodon.social/inbox"},
        }
        mock_requests.get.return_value = mock_resp

        activity = {
            "to": [AS_PUBLIC],
            "cc": [
                "https://blog.example.com/ap/followers",
                "https://mastodon.social/users/alice",
            ],
        }

        inboxes = outbox_processor._collect_recipient_inboxes(activity)

        assert len(inboxes) == 1
        # Should prefer shared inbox
        assert "https://mastodon.social/inbox" in inboxes
        mock_requests.get.assert_called_once()

    @patch("pubby.handlers._outbox.requests")
    def test_collect_recipient_inboxes_uses_cache(
        self, mock_requests, outbox_processor, mock_storage
    ):
        """Should use cached actor data when available."""
        mock_storage.get_cached_actor.return_value = {
            "id": "https://mastodon.social/users/alice",
            "type": "Person",
            "inbox": "https://mastodon.social/users/alice/inbox",
        }

        activity = {
            "to": [AS_PUBLIC],
            "cc": ["https://mastodon.social/users/alice"],
        }

        inboxes = outbox_processor._collect_recipient_inboxes(activity)

        assert len(inboxes) == 1
        assert "https://mastodon.social/users/alice/inbox" in inboxes
        # Should not make HTTP request since cached
        mock_requests.get.assert_not_called()

    @patch("pubby.handlers._outbox.requests")
    def test_publish_delivers_to_mentioned_actors(
        self, mock_requests, outbox_processor, mock_storage
    ):
        """publish() should deliver to both followers and mentioned actors."""
        # Set up a follower
        mock_storage.get_followers.return_value = [
            Follower(
                actor_id="https://other.example.com/users/bob",
                inbox="https://other.example.com/users/bob/inbox",
            ),
        ]

        # Set up mentioned actor fetch
        mock_storage.get_cached_actor.return_value = None

        def mock_get(url, **_):
            resp = MagicMock()
            if "alice" in url:
                resp.status_code = 200
                resp.json.return_value = {
                    "id": "https://mastodon.social/users/alice",
                    "type": "Person",
                    "inbox": "https://mastodon.social/users/alice/inbox",
                }
            else:
                resp.status_code = 404
            return resp

        def mock_post(*_, **__):
            resp = MagicMock()
            resp.status_code = 202
            return resp

        mock_requests.get.side_effect = mock_get
        mock_requests.post.side_effect = mock_post

        obj = Object(
            id="https://blog.example.com/post/1",
            type="Note",
            content="<p>Hello @alice@mastodon.social!</p>",
            attributed_to="https://blog.example.com/ap/actor",
            cc=[
                "https://blog.example.com/ap/followers",
                "https://mastodon.social/users/alice",
            ],
        )

        activity = outbox_processor.build_create_activity(obj)
        outbox_processor.publish(activity)

        # Should store activity
        mock_storage.store_activity.assert_called_once()

        # Should deliver to both follower and mentioned actor
        assert mock_requests.post.call_count == 2
        delivered_urls = [call[0][0] for call in mock_requests.post.call_args_list]
        assert "https://other.example.com/users/bob/inbox" in delivered_urls
        assert "https://mastodon.social/users/alice/inbox" in delivered_urls

    @patch("pubby.handlers._outbox.requests")
    def test_publish_deduplicates_inboxes(
        self, mock_requests, outbox_processor, mock_storage
    ):
        """Should not deliver twice if mentioned actor is also a follower."""
        # Alice is both a follower and mentioned
        mock_storage.get_followers.return_value = [
            Follower(
                actor_id="https://mastodon.social/users/alice",
                inbox="https://mastodon.social/users/alice/inbox",
            ),
        ]

        mock_storage.get_cached_actor.return_value = {
            "id": "https://mastodon.social/users/alice",
            "type": "Person",
            "inbox": "https://mastodon.social/users/alice/inbox",
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_requests.post.return_value = mock_resp

        obj = Object(
            id="https://blog.example.com/post/1",
            type="Note",
            content="<p>Hello @alice!</p>",
            attributed_to="https://blog.example.com/ap/actor",
            cc=[
                "https://blog.example.com/ap/followers",
                "https://mastodon.social/users/alice",
            ],
        )

        activity = outbox_processor.build_create_activity(obj)
        outbox_processor.publish(activity)

        # Should only deliver once (deduplicated)
        assert mock_requests.post.call_count == 1


class TestDirectMessageDelivery:
    """Tests for direct message (visibility: direct) delivery behavior."""

    def test_is_addressed_to_followers_with_public(self, outbox_processor):
        """Should return True when AS_PUBLIC is in to."""
        activity = {"to": [AS_PUBLIC], "cc": []}
        assert outbox_processor._is_addressed_to_followers(activity) is True

    def test_is_addressed_to_followers_with_followers_url(self, outbox_processor):
        """Should return True when followers URL is in cc."""
        activity = {
            "to": [AS_PUBLIC],
            "cc": ["https://blog.example.com/ap/followers"],
        }
        assert outbox_processor._is_addressed_to_followers(activity) is True

    def test_is_addressed_to_followers_direct_message(self, outbox_processor):
        """Should return False for direct messages (no public, no followers)."""
        activity = {
            "to": ["https://mastodon.social/users/alice"],
            "cc": [],
        }
        assert outbox_processor._is_addressed_to_followers(activity) is False

    def test_is_addressed_to_followers_handles_string_values(self, outbox_processor):
        """Should handle to/cc being single strings instead of lists."""
        activity = {
            "to": AS_PUBLIC,
            "cc": "https://blog.example.com/ap/followers",
        }
        assert outbox_processor._is_addressed_to_followers(activity) is True

    @patch("pubby.handlers._outbox.requests")
    def test_direct_message_skips_follower_inboxes(
        self, mock_requests, outbox_processor, mock_storage
    ):
        """Direct messages should NOT be delivered to followers, only to recipients."""
        # Set up followers (should be skipped for direct messages)
        mock_storage.get_followers.return_value = [
            Follower(
                actor_id="https://other.example.com/users/bob",
                inbox="https://other.example.com/users/bob/inbox",
            ),
            Follower(
                actor_id="https://other.example.com/users/charlie",
                inbox="https://other.example.com/users/charlie/inbox",
            ),
        ]

        # Set up mentioned actor fetch
        mock_storage.get_cached_actor.return_value = {
            "id": "https://mastodon.social/users/alice",
            "type": "Person",
            "inbox": "https://mastodon.social/users/alice/inbox",
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_requests.post.return_value = mock_resp

        # Direct message: only to mentioned actor, no public/followers
        obj = Object(
            id="https://blog.example.com/post/dm-1",
            type="Note",
            content="<p>Private message to Alice</p>",
            attributed_to="https://blog.example.com/ap/actor",
            to=["https://mastodon.social/users/alice"],
            cc=[],
        )

        activity = outbox_processor.build_create_activity(obj)
        outbox_processor.publish(activity)

        # Should only deliver to Alice, NOT to followers bob/charlie
        assert mock_requests.post.call_count == 1
        delivered_urls = [call[0][0] for call in mock_requests.post.call_args_list]
        assert "https://mastodon.social/users/alice/inbox" in delivered_urls
        assert "https://other.example.com/users/bob/inbox" not in delivered_urls
        assert "https://other.example.com/users/charlie/inbox" not in delivered_urls

    @patch("pubby.handlers._outbox.requests")
    def test_unlisted_still_delivers_to_followers(
        self, mock_requests, outbox_processor, mock_storage
    ):
        """Unlisted posts (followers in to, public in cc) should go to followers."""
        mock_storage.get_followers.return_value = [
            Follower(
                actor_id="https://other.example.com/users/bob",
                inbox="https://other.example.com/users/bob/inbox",
            ),
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_requests.post.return_value = mock_resp

        # Unlisted: followers in to, public in cc
        obj = Object(
            id="https://blog.example.com/post/unlisted-1",
            type="Note",
            content="<p>Unlisted post</p>",
            attributed_to="https://blog.example.com/ap/actor",
            to=["https://blog.example.com/ap/followers"],
            cc=[AS_PUBLIC],
        )

        activity = outbox_processor.build_create_activity(obj)
        outbox_processor.publish(activity)

        # Should deliver to followers
        assert mock_requests.post.call_count == 1
        delivered_urls = [call[0][0] for call in mock_requests.post.call_args_list]
        assert "https://other.example.com/users/bob/inbox" in delivered_urls


class TestPublishActivity:
    """Tests for ActivityPubHandler.publish_activity (pass-through to outbox)."""

    def test_publish_activity_delegates_to_outbox(self):
        """publish_activity should store and deliver without wrapping."""
        from pubby.handlers._handler import ActivityPubHandler

        handler = MagicMock(spec=ActivityPubHandler)
        handler.outbox = MagicMock()
        handler.outbox.publish.return_value = {"id": "act-1", "type": "Like"}

        # Call the real method on the mock
        result = ActivityPubHandler.publish_activity(
            handler, {"id": "act-1", "type": "Like"}
        )

        handler.outbox.publish.assert_called_once_with({"id": "act-1", "type": "Like"})
        assert result["type"] == "Like"

    @patch("pubby.handlers._outbox.requests")
    def test_publish_like_via_handler(
        self, mock_requests, outbox_processor, mock_storage
    ):
        """End-to-end: build a Like, publish it, verify delivery."""
        mock_storage.get_followers.return_value = [
            Follower(
                actor_id="https://remote.example.com/users/alice",
                inbox="https://remote.example.com/users/alice/inbox",
            ),
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_requests.post.return_value = mock_resp

        like = outbox_processor.build_like_activity(
            "https://remote.example.com/post/42"
        )
        result = outbox_processor.publish(like)

        assert result["type"] == "Like"
        mock_storage.store_activity.assert_called_once()
        mock_requests.post.assert_called_once()

    @patch("pubby.handlers._outbox.requests")
    def test_publish_undo_like_via_handler(
        self, mock_requests, outbox_processor, mock_storage
    ):
        """End-to-end: build an Undo Like, publish it, verify delivery."""
        mock_storage.get_followers.return_value = [
            Follower(
                actor_id="https://remote.example.com/users/alice",
                inbox="https://remote.example.com/users/alice/inbox",
            ),
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_requests.post.return_value = mock_resp

        like = outbox_processor.build_like_activity(
            "https://remote.example.com/post/42"
        )
        undo = outbox_processor.build_undo_activity(like)
        result = outbox_processor.publish(undo)

        assert result["type"] == "Undo"
        assert result["object"]["type"] == "Like"
        mock_storage.store_activity.assert_called()
        mock_requests.post.assert_called()


class TestFetchActorSignedRequest:
    """Tests for OutboxProcessor._fetch_actor using HTTP Signatures on GET."""

    @patch("pubby.handlers._outbox.sign_request")
    @patch("pubby.handlers._outbox.requests")
    def test_fetch_actor_signs_get_request(
        self, mock_requests, mock_sign_request, outbox_processor, mock_storage
    ):
        """_fetch_actor should sign the GET request for authorized fetch."""
        actor_url = "https://remote.example.com/users/alice"
        mock_storage.get_cached_actor.return_value = None

        mock_sign_request.return_value = {
            "Signature": "sig",
            "Date": "now",
            "Host": "remote.example.com",
            "Accept": "application/activity+json, application/ld+json",
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": actor_url, "type": "Person"}
        mock_requests.get.return_value = mock_resp

        result = outbox_processor._fetch_actor(actor_url)

        assert result is not None
        mock_sign_request.assert_called_once()
        kwargs = mock_sign_request.call_args.kwargs
        assert kwargs["method"] == "GET"
        assert kwargs["url"] == actor_url
        assert "Accept" in kwargs["headers"]

    @patch("pubby.handlers._outbox.sign_request")
    @patch("pubby.handlers._outbox.requests")
    def test_fetch_actor_includes_signed_headers(
        self, mock_requests, mock_sign_request, outbox_processor, mock_storage
    ):
        """Signed headers should be included in the GET request."""
        actor_url = "https://remote.example.com/users/alice"
        mock_storage.get_cached_actor.return_value = None

        mock_sign_request.return_value = {
            "Signature": "test-sig",
            "Date": "Wed, 01 Jan 2025 00:00:00 GMT",
            "Host": "remote.example.com",
            "Accept": "application/activity+json, application/ld+json",
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": actor_url, "type": "Person"}
        mock_requests.get.return_value = mock_resp

        outbox_processor._fetch_actor(actor_url)

        call_kwargs = mock_requests.get.call_args.kwargs
        assert "Signature" in call_kwargs["headers"]
        assert "Date" in call_kwargs["headers"]
        assert "Host" in call_kwargs["headers"]


class TestCollectInboxesFunction:
    """Tests for the module-level collect_inboxes helper."""

    def test_shared_inbox_preferred(self):
        followers = [
            Follower(
                actor_id="https://mastodon.social/users/alice",
                inbox="https://mastodon.social/users/alice/inbox",
                shared_inbox="https://mastodon.social/inbox",
            ),
        ]
        assert collect_inboxes(followers) == ["https://mastodon.social/inbox"]

    def test_deduplicates_shared_inboxes(self):
        followers = [
            Follower(
                actor_id="https://mastodon.social/users/alice",
                inbox="https://mastodon.social/users/alice/inbox",
                shared_inbox="https://mastodon.social/inbox",
            ),
            Follower(
                actor_id="https://mastodon.social/users/bob",
                inbox="https://mastodon.social/users/bob/inbox",
                shared_inbox="https://mastodon.social/inbox",
            ),
            Follower(
                actor_id="https://other.example.com/users/carol",
                inbox="https://other.example.com/users/carol/inbox",
                shared_inbox="",
            ),
        ]
        assert collect_inboxes(followers) == [
            "https://mastodon.social/inbox",
            "https://other.example.com/users/carol/inbox",
        ]

    def test_skips_followers_without_inbox(self):
        followers = [
            Follower(actor_id="https://x.example.com/users/a", inbox=""),
            Follower(
                actor_id="https://x.example.com/users/b",
                inbox="",
                shared_inbox="",
            ),
            Follower(
                actor_id="https://y.example.com/users/c",
                inbox="https://y.example.com/users/c/inbox",
            ),
        ]
        assert collect_inboxes(followers) == ["https://y.example.com/users/c/inbox"]

    def test_empty(self):
        assert collect_inboxes([]) == []

    def test_processor_method_delegates(self, outbox_processor):
        """OutboxProcessor._collect_inboxes delegates to the module helper."""
        followers = [
            Follower(
                actor_id="https://mastodon.social/users/alice",
                inbox="https://mastodon.social/users/alice/inbox",
                shared_inbox="https://mastodon.social/inbox",
            ),
        ]
        assert outbox_processor._collect_inboxes(followers) == collect_inboxes(
            followers
        )


class TestDeliverActivity:
    """Tests for the module-level deliver_activity one-shot signed POST."""

    @patch("pubby.handlers._outbox.sign_request")
    @patch("pubby.handlers._outbox.requests")
    def test_signs_and_posts_activity(
        self, mock_requests, mock_sign_request, private_key
    ):
        mock_sign_request.return_value = {
            "Signature": "sig",
            "Date": "now",
            "Host": "remote.example.com",
            "Digest": "SHA-256=...",
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_requests.post.return_value = mock_resp

        activity = {"id": "act-1", "type": "Create", "object": {}}
        status = deliver_activity(
            activity,
            "https://remote.example.com/inbox",
            key_id="https://blog.example.com/ap/actor#main-key",
            private_key=private_key,
            user_agent="test-agent/1.0",
        )

        assert status == 202

        mock_sign_request.assert_called_once()
        sign_kwargs = mock_sign_request.call_args.kwargs
        assert sign_kwargs["method"] == "POST"
        assert sign_kwargs["url"] == "https://remote.example.com/inbox"
        assert sign_kwargs["key_id"] == ("https://blog.example.com/ap/actor#main-key")
        assert "digest" in [h.lower() for h in sign_kwargs["signed_headers"]]
        assert "content-type" in [h.lower() for h in sign_kwargs["signed_headers"]]

        post_kwargs = mock_requests.post.call_args.kwargs
        headers = post_kwargs["headers"]
        assert headers["Signature"] == "sig"
        assert headers["Content-Type"] == "application/activity+json"
        assert headers["User-Agent"] == "test-agent/1.0"
        assert mock_requests.post.call_args[0][0] == (
            "https://remote.example.com/inbox"
        )

    @patch("pubby.handlers._outbox.requests")
    def test_4xx_status_returned_not_raised(self, mock_requests, private_key):
        mock_resp = MagicMock()
        mock_resp.status_code = 410
        mock_resp.text = "Gone"
        mock_requests.post.return_value = mock_resp

        status = deliver_activity(
            {"id": "act-1", "type": "Delete"},
            "https://remote.example.com/inbox",
            key_id="https://blog.example.com/ap/actor#main-key",
            private_key=private_key,
        )
        assert status == 410

    @patch("pubby.handlers._outbox.requests")
    def test_5xx_status_returned_not_raised(self, mock_requests, private_key):
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.text = "Service Unavailable"
        mock_requests.post.return_value = mock_resp

        status = deliver_activity(
            {"id": "act-1", "type": "Create"},
            "https://remote.example.com/inbox",
            key_id="https://blog.example.com/ap/actor#main-key",
            private_key=private_key,
        )
        assert status == 503

    @patch("pubby.handlers._outbox.requests")
    def test_default_user_agent(self, mock_requests, private_key):
        import pubby

        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_requests.post.return_value = mock_resp

        deliver_activity(
            {"id": "act-1", "type": "Create"},
            "https://remote.example.com/inbox",
            key_id="https://blog.example.com/ap/actor#main-key",
            private_key=private_key,
        )

        headers = mock_requests.post.call_args.kwargs["headers"]
        assert headers["User-Agent"] == f"pubby/{pubby.__version__}"

    @patch("pubby.handlers._outbox.requests")
    def test_network_errors_propagate(self, mock_requests, private_key):
        import requests as real_requests

        mock_requests.post.side_effect = real_requests.ConnectionError("refused")
        # Keep the real exception class on the mocked module
        mock_requests.ConnectionError = real_requests.ConnectionError

        with pytest.raises(real_requests.ConnectionError):
            deliver_activity(
                {"id": "act-1", "type": "Create"},
                "https://remote.example.com/inbox",
                key_id="https://blog.example.com/ap/actor#main-key",
                private_key=private_key,
            )


class TestCustomDeliverCallable:
    """Tests for the pluggable deliver seam on OutboxProcessor."""

    @pytest.fixture
    def queue_processor(self, mock_storage, private_key):
        delivered: list[tuple[str, dict]] = []
        processor = OutboxProcessor(
            storage=mock_storage,
            actor_id="https://blog.example.com/ap/actor",
            private_key=private_key,
            key_id="https://blog.example.com/ap/actor#main-key",
            followers_collection_url="https://blog.example.com/ap/followers",
            deliver=lambda inbox_url, activity: delivered.append((inbox_url, activity)),
        )
        return processor, delivered

    @patch("pubby.handlers._outbox.requests")
    @patch("pubby.handlers._outbox.ThreadPoolExecutor")
    def test_publish_uses_custom_deliver(
        self,
        mock_pool,
        mock_requests,
        queue_processor,
        mock_storage,
    ):
        processor, delivered = queue_processor
        mock_storage.get_followers.return_value = [
            Follower(
                actor_id="https://mastodon.social/users/alice",
                inbox="https://mastodon.social/users/alice/inbox",
                shared_inbox="https://mastodon.social/inbox",
            ),
            Follower(
                actor_id="https://mastodon.social/users/bob",
                inbox="https://mastodon.social/users/bob/inbox",
                shared_inbox="https://mastodon.social/inbox",
            ),
            Follower(
                actor_id="https://other.example.com/users/carol",
                inbox="https://other.example.com/users/carol/inbox",
            ),
        ]

        obj = Object(
            id="https://blog.example.com/post/1",
            type="Article",
            content="<p>Test</p>",
            attributed_to="https://blog.example.com/ap/actor",
        )
        activity = processor.build_create_activity(obj)
        result = processor.publish(activity)

        # Activity still stored and returned
        mock_storage.store_activity.assert_called_once()
        assert result is activity

        # Custom callable got the deduplicated, shared-inbox-preferred set
        assert [url for url, _ in delivered] == [
            "https://mastodon.social/inbox",
            "https://other.example.com/users/carol/inbox",
        ]
        assert all(act is activity for _, act in delivered)

        # No thread pool, no direct HTTP
        mock_pool.assert_not_called()
        mock_requests.post.assert_not_called()

    @patch("pubby.handlers._outbox.requests")
    def test_custom_deliver_respects_blocked_instances(
        self, mock_requests, mock_storage, private_key
    ):
        delivered: list[str] = []
        processor = OutboxProcessor(
            storage=mock_storage,
            actor_id="https://blog.example.com/ap/actor",
            private_key=private_key,
            key_id="https://blog.example.com/ap/actor#main-key",
            followers_collection_url="https://blog.example.com/ap/followers",
            blocked_instances=["spam.example"],
            deliver=lambda inbox_url, activity: delivered.append(inbox_url),
        )
        mock_storage.get_followers.return_value = [
            Follower(
                actor_id="https://good.example.com/users/alice",
                inbox="https://good.example.com/inbox",
            ),
            Follower(
                actor_id="https://spam.example/users/bot",
                inbox="https://spam.example/inbox",
            ),
        ]

        obj = Object(
            id="https://blog.example.com/post/1",
            type="Article",
            content="<p>Test</p>",
            attributed_to="https://blog.example.com/ap/actor",
        )
        processor.publish(processor.build_create_activity(obj))

        assert delivered == ["https://good.example.com/inbox"]

    @patch("pubby.handlers._outbox.requests")
    def test_custom_deliver_error_does_not_stop_others(
        self, mock_requests, mock_storage, private_key
    ):
        delivered: list[str] = []

        def flaky(inbox_url, activity):
            if "bad" in inbox_url:
                raise RuntimeError("queue down")
            delivered.append(inbox_url)

        processor = OutboxProcessor(
            storage=mock_storage,
            actor_id="https://blog.example.com/ap/actor",
            private_key=private_key,
            key_id="https://blog.example.com/ap/actor#main-key",
            followers_collection_url="https://blog.example.com/ap/followers",
            deliver=flaky,
        )
        mock_storage.get_followers.return_value = [
            Follower(
                actor_id="https://bad.example.com/users/a",
                inbox="https://bad.example.com/inbox",
            ),
            Follower(
                actor_id="https://good.example.com/users/b",
                inbox="https://good.example.com/inbox",
            ),
        ]

        obj = Object(
            id="https://blog.example.com/post/1",
            type="Article",
            content="<p>Test</p>",
            attributed_to="https://blog.example.com/ap/actor",
        )
        processor.publish(processor.build_create_activity(obj))

        assert delivered == ["https://good.example.com/inbox"]

    def test_handler_forwards_deliver(self, mock_storage, private_key):
        from pubby.handlers import ActivityPubHandler

        sentinel = lambda inbox_url, activity: None  # noqa: E731
        handler = ActivityPubHandler(
            storage=mock_storage,
            actor_config={
                "base_url": "https://blog.example.com",
                "username": "blog",
            },
            private_key=private_key,
            deliver=sentinel,
        )
        assert handler.outbox.deliver is sentinel
