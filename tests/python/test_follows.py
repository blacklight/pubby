"""
Tests for pending follow-follow_request resolution helpers.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from pubby._model import FollowRequest
from pubby.follows import (
    accept_follow_request,
    build_follow_response,
    reject_follow_request,
)


@pytest.fixture
def follow_request():
    return FollowRequest(
        actor_id="https://remote.example.com/users/alice",
        target_actor_id="https://blog.example.com/ap/actor",
        inbox="https://remote.example.com/users/alice/inbox",
        shared_inbox="https://remote.example.com/inbox",
        actor_data={"name": "Alice"},
        activity={
            "id": "https://remote.example.com/users/alice/activities/follow-1",
            "type": "Follow",
            "actor": "https://remote.example.com/users/alice",
            "object": "https://blog.example.com/ap/actor",
        },
        requested_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


class TestBuildFollowResponse:
    def test_embeds_original_follow(self, follow_request):
        activity = build_follow_response(
            "https://blog.example.com/ap/actor", follow_request.activity, "Accept"
        )

        assert activity["type"] == "Accept"
        assert activity["actor"] == "https://blog.example.com/ap/actor"
        assert activity["object"] == follow_request.activity
        assert activity["id"].startswith("https://blog.example.com/ap/actor#accept-")


class TestAcceptFollowRequest:
    def test_promotes_request_to_follower(self, follow_request):
        storage = MagicMock()
        delivered = []

        activity = accept_follow_request(
            storage,
            follow_request,
            actor_id="https://blog.example.com/ap/actor",
            deliver=lambda inbox, act: delivered.append((inbox, act)),
        )

        storage.store_follower.assert_called_once()
        follower = storage.store_follower.call_args[0][0]
        assert follower.actor_id == follow_request.actor_id
        assert follower.target_actor_id == follow_request.target_actor_id
        assert follower.inbox == follow_request.inbox
        assert follower.followed_at == follow_request.requested_at

        storage.remove_follow_request.assert_called_once_with(
            follow_request.actor_id, follow_request.target_actor_id
        )

        assert activity["type"] == "Accept"
        assert delivered == [("https://remote.example.com/users/alice/inbox", activity)]

    def test_falls_back_to_shared_inbox(self, follow_request):
        storage = MagicMock()
        delivered = []
        follow_request.inbox = ""

        accept_follow_request(
            storage,
            follow_request,
            actor_id="https://blog.example.com/ap/actor",
            deliver=lambda inbox, act: delivered.append(inbox),
        )

        assert delivered == ["https://remote.example.com/inbox"]

    def test_no_inbox_skips_delivery(self, follow_request):
        storage = MagicMock()
        follow_request.inbox = ""
        follow_request.shared_inbox = ""

        activity = accept_follow_request(
            storage,
            follow_request,
            actor_id="https://blog.example.com/ap/actor",
            deliver=MagicMock(),
        )

        # The follower is still promoted; only delivery is skipped.
        storage.store_follower.assert_called_once()
        assert activity["type"] == "Accept"

    def test_synchronous_delivery_requires_key(self, follow_request):
        storage = MagicMock()

        with pytest.raises(ValueError, match="key_id and private_key"):
            accept_follow_request(
                storage, follow_request, actor_id="https://blog.example.com/ap/actor"
            )


class TestRejectFollowRequest:
    def test_removes_request_and_delivers_reject(self, follow_request):
        storage = MagicMock()
        delivered = []

        activity = reject_follow_request(
            storage,
            follow_request,
            actor_id="https://blog.example.com/ap/actor",
            deliver=lambda inbox, act: delivered.append((inbox, act)),
        )

        storage.store_follower.assert_not_called()
        storage.remove_follow_request.assert_called_once_with(
            follow_request.actor_id, follow_request.target_actor_id
        )

        assert activity["type"] == "Reject"
        assert activity["object"] == follow_request.activity
        assert delivered == [("https://remote.example.com/users/alice/inbox", activity)]
