"""
Tests for inbox processing — Follow, Undo, Create, Like, Announce, Delete.
"""

from unittest.mock import MagicMock, patch

import pytest

from pubby._model import (
    InteractionType,
)
from pubby.handlers._inbox import InboxProcessor


@pytest.fixture
def mock_storage():
    storage = MagicMock()
    storage.get_cached_actor.return_value = None
    storage.get_followers.return_value = []
    return storage


@pytest.fixture
def inbox_processor(mock_storage, private_key):
    return InboxProcessor(
        storage=mock_storage,
        actor_id="https://blog.example.com/ap/actor",
        private_key=private_key,
        key_id="https://blog.example.com/ap/actor#main-key",
    )


def _remote_actor_data(actor_id="https://remote.example.com/users/alice"):
    return {
        "id": actor_id,
        "type": "Person",
        "preferredUsername": "alice",
        "name": "Alice",
        "inbox": f"{actor_id}/inbox",
        "outbox": f"{actor_id}/outbox",
        "followers": f"{actor_id}/followers",
        "following": f"{actor_id}/following",
        "url": actor_id,
        "publicKey": {
            "id": f"{actor_id}#main-key",
            "owner": actor_id,
            "publicKeyPem": "-----BEGIN PUBLIC KEY-----\ntest\n-----END PUBLIC KEY-----",
        },
        "icon": {"type": "Image", "url": "https://remote.example.com/avatar.png"},
        "endpoints": {"sharedInbox": "https://remote.example.com/inbox"},
    }


class TestHandleFollow:
    @patch("pubby.handlers._inbox.requests")
    def test_follow_stores_follower_and_sends_accept(
        self, mock_requests, inbox_processor, mock_storage
    ):
        actor_id = "https://remote.example.com/users/alice"
        actor_data = _remote_actor_data(actor_id)

        # Mock fetching the actor
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = actor_data
        mock_resp.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_resp
        mock_requests.post.return_value = MagicMock(status_code=202)

        activity = {
            "id": f"{actor_id}/activities/follow-1",
            "type": "Follow",
            "actor": actor_id,
            "object": "https://blog.example.com/ap/actor",
        }

        result = inbox_processor.process(activity, skip_verification=True)

        # Should store follower
        mock_storage.store_follower.assert_called_once()
        follower = mock_storage.store_follower.call_args[0][0]
        assert follower.actor_id == actor_id
        assert follower.inbox == f"{actor_id}/inbox"
        assert follower.shared_inbox == "https://remote.example.com/inbox"

        # Should send Accept
        assert result is not None
        assert result["type"] == "Accept"

        # Should POST to the follower's inbox
        mock_requests.post.assert_called_once()


class TestHandleUndoFollow:
    def test_undo_follow_removes_follower(self, inbox_processor, mock_storage):
        actor_id = "https://remote.example.com/users/alice"
        activity = {
            "id": f"{actor_id}/activities/undo-1",
            "type": "Undo",
            "actor": actor_id,
            "object": {
                "id": f"{actor_id}/activities/follow-1",
                "type": "Follow",
                "actor": actor_id,
                "object": "https://blog.example.com/ap/actor",
            },
        }

        inbox_processor.process(activity, skip_verification=True)
        mock_storage.remove_follower.assert_called_once_with(actor_id)


class TestHandleCreate:
    @patch("pubby.handlers._inbox.requests")
    def test_create_note_stores_reply(
        self, mock_requests, inbox_processor, mock_storage
    ):
        actor_id = "https://remote.example.com/users/alice"
        actor_data = _remote_actor_data(actor_id)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = actor_data
        mock_resp.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_resp

        activity = {
            "id": f"{actor_id}/activities/create-1",
            "type": "Create",
            "actor": actor_id,
            "object": {
                "id": f"{actor_id}/notes/1",
                "type": "Note",
                "content": "<p>Great post!</p>",
                "attributedTo": actor_id,
                "inReplyTo": "https://blog.example.com/post/1",
                "published": "2024-01-01T00:00:00Z",
            },
        }

        inbox_processor.process(activity, skip_verification=True)
        mock_storage.store_interaction.assert_called_once()

        interaction = mock_storage.store_interaction.call_args[0][0]
        assert interaction.source_actor_id == actor_id
        assert interaction.target_resource == "https://blog.example.com/post/1"
        assert interaction.interaction_type == InteractionType.REPLY
        assert interaction.content == "<p>Great post!</p>"
        assert interaction.author_name == "Alice"

    @patch("pubby.handlers._inbox.requests")
    def test_create_without_reply_to_ignored(
        self, mock_requests, inbox_processor, mock_storage
    ):
        activity = {
            "id": "https://remote.example.com/activities/1",
            "type": "Create",
            "actor": "https://remote.example.com/users/alice",
            "object": {
                "id": "https://remote.example.com/notes/1",
                "type": "Note",
                "content": "Just a random note",
            },
        }

        inbox_processor.process(activity, skip_verification=True)
        mock_storage.store_interaction.assert_not_called()


class TestHandleLike:
    @patch("pubby.handlers._inbox.requests")
    def test_like_stores_interaction(
        self, mock_requests, inbox_processor, mock_storage
    ):
        actor_id = "https://remote.example.com/users/alice"
        actor_data = _remote_actor_data(actor_id)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = actor_data
        mock_resp.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_resp

        activity = {
            "id": f"{actor_id}/activities/like-1",
            "type": "Like",
            "actor": actor_id,
            "object": "https://blog.example.com/post/1",
        }

        inbox_processor.process(activity, skip_verification=True)
        mock_storage.store_interaction.assert_called_once()

        interaction = mock_storage.store_interaction.call_args[0][0]
        assert interaction.interaction_type == InteractionType.LIKE
        assert interaction.target_resource == "https://blog.example.com/post/1"


class TestHandleAnnounce:
    @patch("pubby.handlers._inbox.requests")
    def test_announce_stores_boost(self, mock_requests, inbox_processor, mock_storage):
        actor_id = "https://remote.example.com/users/alice"
        actor_data = _remote_actor_data(actor_id)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = actor_data
        mock_resp.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_resp

        activity = {
            "id": f"{actor_id}/activities/announce-1",
            "type": "Announce",
            "actor": actor_id,
            "object": "https://blog.example.com/post/2",
        }

        inbox_processor.process(activity, skip_verification=True)
        mock_storage.store_interaction.assert_called_once()

        interaction = mock_storage.store_interaction.call_args[0][0]
        assert interaction.interaction_type == InteractionType.BOOST
        assert interaction.target_resource == "https://blog.example.com/post/2"


class TestHandleDelete:
    def test_delete_removes_interactions(self, inbox_processor, mock_storage):
        actor_id = "https://remote.example.com/users/alice"
        activity = {
            "id": f"{actor_id}/activities/delete-1",
            "type": "Delete",
            "actor": actor_id,
            "object": {
                "id": f"{actor_id}/notes/1",
                "type": "Tombstone",
            },
        }

        inbox_processor.process(activity, skip_verification=True)
        # Should attempt to delete all interaction types
        assert mock_storage.delete_interaction.call_count == len(InteractionType)


class TestHandleUpdate:
    @patch("pubby.handlers._inbox.requests")
    def test_update_note_updates_interaction(
        self, mock_requests, inbox_processor, mock_storage
    ):
        actor_id = "https://remote.example.com/users/alice"
        actor_data = _remote_actor_data(actor_id)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = actor_data
        mock_resp.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_resp

        activity = {
            "id": f"{actor_id}/activities/update-1",
            "type": "Update",
            "actor": actor_id,
            "object": {
                "id": f"{actor_id}/notes/1",
                "type": "Note",
                "content": "<p>Updated reply</p>",
                "inReplyTo": "https://blog.example.com/post/1",
            },
        }

        inbox_processor.process(activity, skip_verification=True)
        mock_storage.store_interaction.assert_called_once()

        interaction = mock_storage.store_interaction.call_args[0][0]
        assert interaction.content == "<p>Updated reply</p>"


class TestUnknownActivity:
    def test_unknown_type_ignored(self, inbox_processor, mock_storage):
        activity = {
            "id": "https://example.com/activity/1",
            "type": "Add",
            "actor": "https://example.com/actor",
            "object": "something",
        }

        result = inbox_processor.process(activity, skip_verification=True)
        assert result is None
        mock_storage.store_follower.assert_not_called()
        mock_storage.store_interaction.assert_not_called()


class TestInteractionCallback:
    @patch("pubby.handlers._inbox.requests")
    def test_callback_called_on_like(self, mock_requests, mock_storage, private_key):
        callback = MagicMock()
        processor = InboxProcessor(
            storage=mock_storage,
            actor_id="https://blog.example.com/ap/actor",
            private_key=private_key,
            key_id="https://blog.example.com/ap/actor#main-key",
            on_interaction_received=callback,
        )

        actor_id = "https://remote.example.com/users/alice"
        actor_data = _remote_actor_data(actor_id)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = actor_data
        mock_resp.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_resp

        activity = {
            "id": f"{actor_id}/activities/like-1",
            "type": "Like",
            "actor": actor_id,
            "object": "https://blog.example.com/post/1",
        }

        processor.process(activity, skip_verification=True)
        callback.assert_called_once()
