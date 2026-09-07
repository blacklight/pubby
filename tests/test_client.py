"""
Tests for the pubby.client actor/inbox resolver.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests as real_requests

from pubby.client import extract_actor_inbox, resolve_actor_inbox
from pubby.crypto import export_private_key_pem


def _make_mock_response(data=None, status_code=200, json_error=None):
    resp = MagicMock()
    resp.status_code = status_code
    if json_error:
        resp.json.side_effect = json_error
    else:
        resp.json.return_value = data
    return resp


class TestExtractActorInbox:
    def test_prefers_shared_inbox(self):
        actor = {
            "id": "https://remote.example.com/users/alice",
            "inbox": "https://remote.example.com/users/alice/inbox",
            "endpoints": {"sharedInbox": "https://remote.example.com/inbox"},
        }
        assert extract_actor_inbox(actor) == "https://remote.example.com/inbox"

    def test_falls_back_to_inbox(self):
        actor = {
            "id": "https://remote.example.com/users/alice",
            "inbox": "https://remote.example.com/users/alice/inbox",
        }
        assert (
            extract_actor_inbox(actor) == "https://remote.example.com/users/alice/inbox"
        )

    def test_missing_inbox(self):
        actor = {"id": "https://remote.example.com/users/alice"}
        assert extract_actor_inbox(actor) is None

    def test_invalid_endpoints_type(self):
        actor = {
            "id": "https://remote.example.com/users/alice",
            "inbox": "https://remote.example.com/users/alice/inbox",
            "endpoints": "not-a-dict",
        }
        assert (
            extract_actor_inbox(actor) == "https://remote.example.com/users/alice/inbox"
        )


class TestResolveActorInbox:
    @pytest.fixture
    def storage(self):
        return MagicMock()

    def test_cached_actor_returns_inbox(self, storage):
        storage.get_cached_actor.return_value = {
            "id": "https://remote.example.com/users/alice",
            "inbox": "https://remote.example.com/users/alice/inbox",
        }

        inbox = resolve_actor_inbox(
            "https://remote.example.com/users/alice",
            storage,
        )

        assert inbox == "https://remote.example.com/users/alice/inbox"
        storage.get_cached_actor.assert_called_once_with(
            "https://remote.example.com/users/alice"
        )
        storage.cache_remote_actor.assert_not_called()

    def test_cached_actor_uses_shared_inbox(self, storage):
        storage.get_cached_actor.return_value = {
            "id": "https://remote.example.com/users/alice",
            "inbox": "https://remote.example.com/users/alice/inbox",
            "endpoints": {"sharedInbox": "https://remote.example.com/inbox"},
        }

        inbox = resolve_actor_inbox(
            "https://remote.example.com/users/alice",
            storage,
        )

        assert inbox == "https://remote.example.com/inbox"

    @patch("pubby.client.requests")
    def test_fetch_signs_and_caches(self, mock_requests, storage, private_key):
        actor_url = "https://remote.example.com/users/alice"
        storage.get_cached_actor.return_value = None

        mock_requests.get.return_value = _make_mock_response(
            {
                "id": actor_url,
                "type": "Person",
                "inbox": "https://remote.example.com/users/alice/inbox",
                "endpoints": {"sharedInbox": "https://remote.example.com/inbox"},
            }
        )

        inbox = resolve_actor_inbox(
            actor_url,
            storage,
            private_key=private_key,
            key_id="https://blog.example.com/ap/actor#main-key",
        )

        assert inbox == "https://remote.example.com/inbox"
        storage.cache_remote_actor.assert_called_once()
        mock_requests.get.assert_called_once()

        call_kwargs = mock_requests.get.call_args.kwargs
        headers = call_kwargs["headers"]
        assert headers["Accept"] == "application/activity+json, application/ld+json"
        assert "Signature" in headers
        assert "Date" in headers
        assert "Host" in headers

    @patch("pubby.client.requests")
    def test_unsigned_fetch(self, mock_requests, storage):
        actor_url = "https://remote.example.com/users/alice"
        storage.get_cached_actor.return_value = None

        mock_requests.get.return_value = _make_mock_response(
            {
                "id": actor_url,
                "type": "Person",
                "inbox": "https://remote.example.com/users/alice/inbox",
            }
        )

        inbox = resolve_actor_inbox(actor_url, storage)

        assert inbox == "https://remote.example.com/users/alice/inbox"
        mock_requests.get.assert_called_once()

        call_kwargs = mock_requests.get.call_args.kwargs
        headers = call_kwargs["headers"]
        assert "Signature" not in headers
        assert headers["Accept"] == "application/activity+json, application/ld+json"

    @patch("pubby.client.requests")
    def test_user_agent_override(self, mock_requests, storage):
        actor_url = "https://remote.example.com/users/alice"
        storage.get_cached_actor.return_value = None

        mock_requests.get.return_value = _make_mock_response(
            {
                "id": actor_url,
                "type": "Person",
                "inbox": "https://remote.example.com/users/alice/inbox",
            }
        )

        resolve_actor_inbox(
            actor_url,
            storage,
            user_agent="Songhive/0.0.16",
        )

        headers = mock_requests.get.call_args.kwargs["headers"]
        assert headers["User-Agent"] == "Songhive/0.0.16"

    @patch("pubby.client.requests")
    def test_private_key_pem_string(self, mock_requests, storage, private_key):
        actor_url = "https://remote.example.com/users/alice"
        storage.get_cached_actor.return_value = None

        mock_requests.get.return_value = _make_mock_response(
            {
                "id": actor_url,
                "type": "Person",
                "inbox": "https://remote.example.com/users/alice/inbox",
            }
        )

        pem = export_private_key_pem(private_key)
        inbox = resolve_actor_inbox(
            actor_url,
            storage,
            private_key=pem,
            key_id="https://blog.example.com/ap/actor#main-key",
        )

        assert inbox == "https://remote.example.com/users/alice/inbox"
        headers = mock_requests.get.call_args.kwargs["headers"]
        assert "Signature" in headers

    def test_blocked_instance(self, storage):
        inbox = resolve_actor_inbox(
            "https://spam.example/users/bot",
            storage,
            blocked_instances={"spam.example"},
        )
        assert inbox is None
        storage.get_cached_actor.assert_not_called()

    def test_non_allowed_instance(self, storage):
        inbox = resolve_actor_inbox(
            "https://other.example.com/users/bob",
            storage,
            allowed_instances={"remote.example"},
        )
        assert inbox is None
        storage.get_cached_actor.assert_not_called()

    def test_non_http_actor_url(self, storage):
        inbox = resolve_actor_inbox(
            "acct:alice@remote.example",
            storage,
        )
        assert inbox is None
        storage.get_cached_actor.assert_not_called()

    @patch("pubby.client.requests")
    def test_http_error_returns_none(self, mock_requests, storage):
        actor_url = "https://remote.example.com/users/alice"
        storage.get_cached_actor.return_value = None

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status.side_effect = real_requests.HTTPError(
            response=mock_resp
        )
        mock_requests.get.return_value = mock_resp

        inbox = resolve_actor_inbox(actor_url, storage)
        assert inbox is None

    @patch("pubby.client.requests")
    def test_gone_actor_returns_none(self, mock_requests, storage):
        actor_url = "https://remote.example.com/users/alice"
        storage.get_cached_actor.return_value = None

        mock_resp = MagicMock()
        mock_resp.status_code = 410
        mock_resp.raise_for_status.side_effect = real_requests.HTTPError(
            response=mock_resp
        )
        mock_requests.get.return_value = mock_resp

        inbox = resolve_actor_inbox(actor_url, storage)
        assert inbox is None

    @patch("pubby.client.requests")
    def test_invalid_json_returns_none(self, mock_requests, storage):
        actor_url = "https://remote.example.com/users/alice"
        storage.get_cached_actor.return_value = None

        mock_requests.get.return_value = _make_mock_response(
            json_error=ValueError("not json")
        )

        inbox = resolve_actor_inbox(actor_url, storage)
        assert inbox is None

    @patch("pubby.client.requests")
    def test_network_error_returns_none(self, mock_requests, storage):
        actor_url = "https://remote.example.com/users/alice"
        storage.get_cached_actor.return_value = None

        mock_requests.get.side_effect = real_requests.ConnectionError("refused")

        inbox = resolve_actor_inbox(actor_url, storage)
        assert inbox is None
