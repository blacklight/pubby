"""
Integration tests for multi-actor / per-actor follower isolation.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from pubby._model import Follower, Object
from pubby.crypto._keys import generate_rsa_keypair
from pubby.handlers import ActivityPubHandler
from pubby.storage.adapters.file import FileActivityPubStorage


def _make_handler(storage, base_url, username, private_key):
    return ActivityPubHandler(
        storage=storage,
        actor_config={
            "base_url": base_url,
            "username": username,
            "name": f"{username} blog",
            "summary": f"A test blog for {username}",
        },
        private_key=private_key,
        async_delivery=False,
    )


def _remote_actor_data(actor_id):
    return {
        "id": actor_id,
        "type": "Person",
        "preferredUsername": actor_id.split("/")[-1],
        "name": actor_id.split("/")[-1].title(),
        "inbox": f"{actor_id}/inbox",
        "outbox": f"{actor_id}/outbox",
        "url": actor_id,
        "publicKey": {
            "id": f"{actor_id}#main-key",
            "owner": actor_id,
            "publicKeyPem": "-----BEGIN PUBLIC KEY-----\ntest\n-----END PUBLIC KEY-----",
        },
        "endpoints": {"sharedInbox": f"{actor_id.rsplit('/', 2)[0]}/inbox"},
    }


@pytest.fixture
def shared_storage(tmp_path):
    return FileActivityPubStorage(data_dir=tmp_path)


@pytest.fixture
def actor_a_handler(shared_storage):
    private_key, _ = generate_rsa_keypair()
    return _make_handler(
        shared_storage,
        "https://alice.blog.example.com",
        "alice",
        private_key,
    )


@pytest.fixture
def actor_b_handler(shared_storage):
    private_key, _ = generate_rsa_keypair()
    return _make_handler(
        shared_storage,
        "https://bob.blog.example.com",
        "bob",
        private_key,
    )


class TestFollowerIsolation:
    @patch("pubby.handlers._inbox.requests")
    def test_follow_isolation_by_object(
        self, mock_requests, actor_a_handler, actor_b_handler, shared_storage
    ):
        remote_actor = "https://remote.example.com/users/carol"
        actor_data = _remote_actor_data(remote_actor)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = actor_data
        mock_resp.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_resp
        mock_requests.post.return_value = MagicMock(status_code=202)

        follow_a = {
            "id": f"{remote_actor}/activities/follow-a",
            "type": "Follow",
            "actor": remote_actor,
            "object": actor_a_handler.actor_id,
        }
        follow_b = {
            "id": f"{remote_actor}/activities/follow-b",
            "type": "Follow",
            "actor": remote_actor,
            "object": actor_b_handler.actor_id,
        }

        actor_a_handler.process_inbox_activity(follow_a, skip_verification=True)
        actor_b_handler.process_inbox_activity(follow_b, skip_verification=True)

        a_followers = shared_storage.get_followers(actor_id=actor_a_handler.actor_id)
        b_followers = shared_storage.get_followers(actor_id=actor_b_handler.actor_id)

        assert len(a_followers) == 1
        assert len(b_followers) == 1
        assert a_followers[0].target_actor_id == actor_a_handler.actor_id
        assert b_followers[0].target_actor_id == actor_b_handler.actor_id

    def test_follower_fallback_to_handler_actor(self, actor_a_handler, shared_storage):
        remote_actor = "https://remote.example.com/users/dave"
        actor_data = _remote_actor_data(remote_actor)

        with patch("pubby.handlers._inbox.requests") as mock_requests:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = actor_data
            mock_resp.raise_for_status = MagicMock()
            mock_requests.get.return_value = mock_resp
            mock_requests.post.return_value = MagicMock(status_code=202)

            follow = {
                "id": f"{remote_actor}/activities/follow",
                "type": "Follow",
                "actor": remote_actor,
            }

            actor_a_handler.process_inbox_activity(follow, skip_verification=True)

        followers = shared_storage.get_followers(actor_id=actor_a_handler.actor_id)
        assert len(followers) == 1
        assert followers[0].target_actor_id == actor_a_handler.actor_id


class TestOutboxIsolation:
    @patch("pubby.handlers._outbox.requests")
    def test_publish_only_delivers_to_same_actor_followers(
        self,
        mock_requests,
        actor_a_handler,
        actor_b_handler,
        shared_storage,
    ):
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_requests.post.return_value = mock_resp

        shared_storage.store_follower(
            Follower(
                actor_id="https://remote.example.com/users/alice",
                inbox="https://remote.example.com/users/alice/inbox",
                target_actor_id=actor_a_handler.actor_id,
            )
        )
        shared_storage.store_follower(
            Follower(
                actor_id="https://remote.example.com/users/bob",
                inbox="https://remote.example.com/users/bob/inbox",
                target_actor_id=actor_b_handler.actor_id,
            )
        )

        obj = Object(
            id="https://blog.example.com/posts/hello",
            type="Article",
            name="Hello",
            content="<p>Hello world</p>",
            attributed_to=actor_a_handler.actor_id,
        )

        actor_a_handler.publish_object(obj)

        delivered_urls = [call[0][0] for call in mock_requests.post.call_args_list]
        assert "https://remote.example.com/users/alice/inbox" in delivered_urls
        assert "https://remote.example.com/users/bob/inbox" not in delivered_urls


class TestFollowersCollection:
    def test_get_followers_collection_filters_by_actor(
        self, actor_a_handler, actor_b_handler, shared_storage
    ):
        now = datetime.now(timezone.utc)
        shared_storage.store_follower(
            Follower(
                actor_id="https://remote.example.com/users/alice",
                inbox="https://remote.example.com/users/alice/inbox",
                target_actor_id=actor_a_handler.actor_id,
                followed_at=now,
            )
        )
        shared_storage.store_follower(
            Follower(
                actor_id="https://remote.example.com/users/bob",
                inbox="https://remote.example.com/users/bob/inbox",
                target_actor_id=actor_b_handler.actor_id,
                followed_at=now,
            )
        )

        a_collection = actor_a_handler.get_followers_collection()
        b_collection = actor_b_handler.get_followers_collection()

        assert a_collection["totalItems"] == 1
        assert a_collection["orderedItems"] == [
            "https://remote.example.com/users/alice"
        ]
        assert b_collection["totalItems"] == 1
        assert b_collection["orderedItems"] == ["https://remote.example.com/users/bob"]

    def test_get_followers_collection_includes_legacy_unassigned(
        self, actor_a_handler, actor_b_handler, shared_storage
    ):
        now = datetime.now(timezone.utc)
        shared_storage.store_follower(
            Follower(
                actor_id="https://remote.example.com/users/carol",
                inbox="https://remote.example.com/users/carol/inbox",
                target_actor_id="",
                followed_at=now,
            )
        )

        a_collection = actor_a_handler.get_followers_collection()
        b_collection = actor_b_handler.get_followers_collection()

        # Unassigned/legacy followers are visible to all actors
        assert a_collection["totalItems"] == 1
        assert b_collection["totalItems"] == 1

        # After backfilling the follower to actor A, actor B no longer sees it.
        # Backfill means removing the unassigned record and re-storing it with
        # the correct target actor.
        shared_storage.remove_follower("https://remote.example.com/users/carol")
        backfilled = Follower(
            actor_id="https://remote.example.com/users/carol",
            inbox="https://remote.example.com/users/carol/inbox",
            target_actor_id=actor_a_handler.actor_id,
            followed_at=now,
        )
        shared_storage.store_follower(backfilled)

        assert actor_a_handler.get_followers_collection()["totalItems"] == 1
        assert actor_b_handler.get_followers_collection()["totalItems"] == 0


class TestUndoFollow:
    @patch("pubby.handlers._inbox.requests")
    def test_undo_follow_isolates_per_actor(
        self, mock_requests, actor_a_handler, actor_b_handler, shared_storage
    ):
        remote_actor = "https://remote.example.com/users/carol"
        actor_data = _remote_actor_data(remote_actor)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = actor_data
        mock_resp.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_resp
        mock_requests.post.return_value = MagicMock(status_code=202)

        follow_a = {
            "id": f"{remote_actor}/activities/follow-a",
            "type": "Follow",
            "actor": remote_actor,
            "object": actor_a_handler.actor_id,
        }
        follow_b = {
            "id": f"{remote_actor}/activities/follow-b",
            "type": "Follow",
            "actor": remote_actor,
            "object": actor_b_handler.actor_id,
        }

        actor_a_handler.process_inbox_activity(follow_a, skip_verification=True)
        actor_b_handler.process_inbox_activity(follow_b, skip_verification=True)

        assert len(shared_storage.get_followers(actor_id=actor_a_handler.actor_id)) == 1
        assert len(shared_storage.get_followers(actor_id=actor_b_handler.actor_id)) == 1

        undo_a = {
            "id": f"{remote_actor}/activities/undo-a",
            "type": "Undo",
            "actor": remote_actor,
            "object": {
                "id": f"{remote_actor}/activities/follow-a",
                "type": "Follow",
                "actor": remote_actor,
                "object": actor_a_handler.actor_id,
            },
        }

        actor_a_handler.process_inbox_activity(undo_a, skip_verification=True)

        a_followers = shared_storage.get_followers(actor_id=actor_a_handler.actor_id)
        b_followers = shared_storage.get_followers(actor_id=actor_b_handler.actor_id)

        assert len(a_followers) == 0
        assert len(b_followers) == 1
        assert b_followers[0].target_actor_id == actor_b_handler.actor_id
