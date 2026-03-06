"""
Tests for outbox processing — publish, fan-out delivery, retry logic.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import pytest

from mypub._model import Follower, Object
from mypub.crypto._keys import generate_rsa_keypair
from mypub.handlers._outbox import OutboxProcessor, AS_PUBLIC


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
        assert activity["object"]["type"] == "Tombstone"
        assert (
            activity["object"]["id"] == "https://blog.example.com/post/1"
        )


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
    @patch("mypub.handlers._outbox.requests")
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
        assert (
            "remote.example.com" in call_kwargs[0][0]
            or "remote.example.com" in str(call_kwargs)
        )

    @patch("mypub.handlers._outbox.requests")
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

    @patch("mypub.handlers._outbox.requests")
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
