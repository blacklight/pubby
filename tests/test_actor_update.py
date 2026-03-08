"""
Tests for publish_actor_update — pushing actor profile changes to followers.
"""

from unittest.mock import MagicMock, patch

import pytest

from pubby._model import AP_CONTEXT, Follower
from pubby.handlers import ActivityPubHandler


@pytest.fixture
def handler_with_attachment(private_key):
    """Handler configured with a PropertyValue attachment."""
    storage = MagicMock()
    storage.get_followers.return_value = []
    storage.get_activities.return_value = []

    return ActivityPubHandler(
        storage=storage,
        actor_config={
            "base_url": "https://blog.example.com",
            "username": "blog",
            "name": "Test Blog",
            "summary": "A test blog",
            "attachment": [
                {
                    "type": "PropertyValue",
                    "name": "Website",
                    "value": '<a href="https://blog.example.com" rel="me">'
                    "https://blog.example.com</a>",
                },
            ],
        },
        private_key=private_key,
    )


@pytest.fixture
def handler_no_attachment(private_key):
    """Handler configured without any attachment."""
    storage = MagicMock()
    storage.get_followers.return_value = []
    storage.get_activities.return_value = []

    return ActivityPubHandler(
        storage=storage,
        actor_config={
            "base_url": "https://blog.example.com",
            "username": "blog",
            "name": "Test Blog",
            "summary": "A test blog",
        },
        private_key=private_key,
    )


class TestPublishActorUpdate:
    def test_activity_type_is_update(self, handler_with_attachment):
        with patch("pubby.handlers._outbox.requests"):
            activity = handler_with_attachment.publish_actor_update()

        assert activity["type"] == "Update"

    def test_activity_actor_matches(self, handler_with_attachment):
        with patch("pubby.handlers._outbox.requests"):
            activity = handler_with_attachment.publish_actor_update()

        assert activity["actor"] == "https://blog.example.com/ap/actor"

    def test_activity_object_is_full_actor_document(self, handler_with_attachment):
        with patch("pubby.handlers._outbox.requests"):
            activity = handler_with_attachment.publish_actor_update()

        obj = activity["object"]
        assert obj["id"] == "https://blog.example.com/ap/actor"
        assert obj["type"] == "Person"
        assert obj["preferredUsername"] == "blog"
        assert obj["name"] == "Test Blog"
        assert obj["summary"] == "A test blog"

    def test_activity_includes_attachment(self, handler_with_attachment):
        with patch("pubby.handlers._outbox.requests"):
            activity = handler_with_attachment.publish_actor_update()

        obj = activity["object"]
        assert "attachment" in obj
        assert len(obj["attachment"]) == 1
        assert obj["attachment"][0]["type"] == "PropertyValue"
        assert obj["attachment"][0]["name"] == "Website"
        assert 'rel="me"' in obj["attachment"][0]["value"]

    def test_activity_has_no_attachment_when_empty(self, handler_no_attachment):
        with patch("pubby.handlers._outbox.requests"):
            activity = handler_no_attachment.publish_actor_update()

        obj = activity["object"]
        assert "attachment" not in obj or obj.get("attachment") == []

    def test_activity_has_context(self, handler_with_attachment):
        with patch("pubby.handlers._outbox.requests"):
            activity = handler_with_attachment.publish_actor_update()

        assert activity["@context"] == AP_CONTEXT

    def test_activity_addressed_to_public_and_followers(self, handler_with_attachment):
        with patch("pubby.handlers._outbox.requests"):
            activity = handler_with_attachment.publish_actor_update()

        assert "https://www.w3.org/ns/activitystreams#Public" in activity["to"]
        assert "https://blog.example.com/ap/followers" in activity["cc"]

    def test_activity_has_unique_id(self, handler_with_attachment):
        with patch("pubby.handlers._outbox.requests"):
            a1 = handler_with_attachment.publish_actor_update()
            a2 = handler_with_attachment.publish_actor_update()

        assert a1["id"] != a2["id"]

    def test_activity_id_anchored_to_actor(self, handler_with_attachment):
        with patch("pubby.handlers._outbox.requests"):
            activity = handler_with_attachment.publish_actor_update()

        assert activity["id"].startswith(
            "https://blog.example.com/ap/actor#update-profile-"
        )

    def test_activity_is_stored(self, handler_with_attachment):
        with patch("pubby.handlers._outbox.requests"):
            handler_with_attachment.publish_actor_update()

        handler_with_attachment.storage.store_activity.assert_called_once()

    @patch("pubby.handlers._outbox.requests")
    def test_activity_is_delivered_to_followers(
        self, mock_requests, handler_with_attachment
    ):
        handler_with_attachment.storage.get_followers.return_value = [
            Follower(
                actor_id="https://mastodon.social/users/alice",
                inbox="https://mastodon.social/users/alice/inbox",
                shared_inbox="https://mastodon.social/inbox",
            ),
            Follower(
                actor_id="https://akkoma.example/users/bob",
                inbox="https://akkoma.example/users/bob/inbox",
                shared_inbox="",
            ),
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_requests.post.return_value = mock_resp

        handler_with_attachment.publish_actor_update()

        # Should deliver to 2 inboxes (shared for mastodon, personal for akkoma)
        assert mock_requests.post.call_count == 2

    @patch("pubby.handlers._outbox.requests")
    def test_activity_deduplicates_shared_inboxes(
        self, mock_requests, handler_with_attachment
    ):
        handler_with_attachment.storage.get_followers.return_value = [
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
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_requests.post.return_value = mock_resp

        handler_with_attachment.publish_actor_update()

        # Should deliver only once (shared inbox dedup)
        assert mock_requests.post.call_count == 1
