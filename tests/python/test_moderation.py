"""
Tests for pubby.moderation — domain normalization and instance allow/block
lists, and their enforcement in the inbox and outbox processors.
"""

from unittest.mock import MagicMock, patch

import pytest

from pubby._model import Follower, Object
from pubby.handlers._inbox import InboxProcessor
from pubby.handlers._outbox import OutboxProcessor
from pubby.moderation import extract_domain, is_domain_blocked, normalize_domain


class TestNormalizeDomain:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("example.com", "example.com"),
            ("Example.COM", "example.com"),
            ("  example.com  ", "example.com"),
            ("example.com/", "example.com"),
            ("example.com:8080", "example.com"),
            ("example.com/some/path", "example.com"),
            ("http://example.com", "example.com"),
            ("https://example.com", "example.com"),
            ("https://example.com/users/alice", "example.com"),
            ("https://example.com:8443/inbox", "example.com"),
            ("HTTPS://EXAMPLE.COM/Path", "example.com"),
            ("", ""),
            ("   ", ""),
        ],
    )
    def test_normalize(self, value, expected):
        assert normalize_domain(value) == expected


class TestExtractDomain:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("https://remote.example.com/users/alice", "remote.example.com"),
            ("https://remote.example.com/inbox", "remote.example.com"),
            ("remote.example.com", "remote.example.com"),
            ("", ""),
        ],
    )
    def test_extract(self, value, expected):
        assert extract_domain(value) == expected


class TestIsDomainBlocked:
    def test_no_lists_allows_everything(self):
        assert not is_domain_blocked("anything.example")

    def test_empty_lists_allow_everything(self):
        assert not is_domain_blocked("anything.example", allowed=[], blocked=[])

    def test_blocked_domain(self):
        assert is_domain_blocked("spam.example", blocked=["spam.example"])

    def test_blocked_domain_from_url(self):
        assert is_domain_blocked(
            "https://spam.example/users/bot", blocked=["spam.example"]
        )

    def test_blocked_matching_is_case_and_scheme_insensitive(self):
        assert is_domain_blocked(
            "SPAM.example",
            blocked=["https://Spam.Example/path"],
        )

    def test_blocked_wins_over_allowed(self):
        assert is_domain_blocked(
            "mixed.example",
            allowed=["mixed.example"],
            blocked=["mixed.example"],
        )

    def test_empty_allow_list_allows_all(self):
        assert not is_domain_blocked("foo.example", allowed=[])

    def test_allow_list_excludes_other_domains(self):
        assert is_domain_blocked("foo.example", allowed=["bar.example"])

    def test_allow_list_includes_domain(self):
        assert not is_domain_blocked("foo.example", allowed=["FOO.example"])

    def test_allow_list_entry_with_scheme_matches_bare_domain(self):
        assert not is_domain_blocked("good.example", allowed=["https://good.example"])

    def test_unparseable_domain_is_not_blocked(self):
        assert not is_domain_blocked("", blocked=["spam.example"])
        assert not is_domain_blocked("", allowed=["good.example"])


def _follow_activity(actor_url: str) -> dict:
    return {
        "id": f"{actor_url}/activities/follow-1",
        "type": "Follow",
        "actor": actor_url,
        "object": "https://blog.example.com/ap/actor",
    }


def _remote_actor_data(actor_id: str) -> dict:
    return {
        "id": actor_id,
        "type": "Person",
        "preferredUsername": "alice",
        "inbox": f"{actor_id}/inbox",
        "publicKey": {
            "id": f"{actor_id}#main-key",
            "owner": actor_id,
            "publicKeyPem": "-----BEGIN PUBLIC KEY-----\ntest\n-----END PUBLIC KEY-----",
        },
        "endpoints": {},
    }


@pytest.fixture
def mock_storage():
    storage = MagicMock()
    storage.get_cached_actor.return_value = None
    storage.get_followers.return_value = []
    storage.get_activities.return_value = []
    return storage


def _make_inbox(storage, private_key, **kwargs):
    return InboxProcessor(
        storage=storage,
        actor_id="https://blog.example.com/ap/actor",
        private_key=private_key,
        key_id="https://blog.example.com/ap/actor#main-key",
        **kwargs,
    )


def _make_outbox(storage, private_key, **kwargs):
    kwargs.setdefault("async_delivery", False)
    return OutboxProcessor(
        storage=storage,
        actor_id="https://blog.example.com/ap/actor",
        private_key=private_key,
        key_id="https://blog.example.com/ap/actor#main-key",
        followers_collection_url="https://blog.example.com/ap/followers",
        **kwargs,
    )


class TestInboxEnforcement:
    @patch("pubby.handlers._inbox.requests")
    def test_blocked_actor_domain_dropped_before_verification(
        self, mock_requests, mock_storage, private_key
    ):
        """A blocked actor domain returns None without any HTTP call."""
        processor = _make_inbox(
            mock_storage, private_key, blocked_instances=["spam.example"]
        )
        activity = _follow_activity("https://spam.example/users/bot")

        # Pass signed-looking headers: if the domain check ran *after*
        # signature verification, the actor key fetch would hit requests.get.
        result = processor.process(
            activity,
            headers={"Signature": 'keyId="https://spam.example/users/bot#main-key"'},
        )

        assert result is None
        mock_requests.get.assert_not_called()
        mock_storage.store_follower.assert_not_called()

    @patch("pubby.handlers._inbox.requests")
    def test_blocked_actor_domain_dropped_when_verification_skipped(
        self, mock_requests, mock_storage, private_key
    ):
        processor = _make_inbox(
            mock_storage, private_key, blocked_instances=["spam.example"]
        )
        activity = _follow_activity("https://spam.example/users/bot")

        result = processor.process(activity, skip_verification=True)

        assert result is None
        mock_requests.get.assert_not_called()
        mock_storage.store_follower.assert_not_called()

    @patch("pubby.handlers._inbox.requests")
    def test_non_allowed_actor_domain_dropped(
        self, mock_requests, mock_storage, private_key
    ):
        """With an allow-list, actors on unlisted domains are dropped."""
        processor = _make_inbox(
            mock_storage, private_key, allowed_instances=["good.example"]
        )
        activity = _follow_activity("https://spam.example/users/bot")

        result = processor.process(activity, skip_verification=True)

        assert result is None
        mock_requests.get.assert_not_called()
        mock_storage.store_follower.assert_not_called()

    @patch("pubby.handlers._inbox.requests")
    def test_allowed_actor_domain_is_processed(
        self, mock_requests, mock_storage, private_key
    ):
        """An actor on the allow-list goes through normal processing."""
        processor = _make_inbox(
            mock_storage, private_key, allowed_instances=["good.example"]
        )
        actor_id = "https://good.example/users/alice"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _remote_actor_data(actor_id)
        mock_resp.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_resp
        mock_requests.post.return_value = MagicMock(status_code=202)

        result = processor.process(_follow_activity(actor_id), skip_verification=True)

        assert result is not None
        assert result["type"] == "Accept"
        mock_storage.store_follower.assert_called_once()

    @patch("pubby.handlers._inbox.requests")
    def test_actor_as_dict_is_checked(self, mock_requests, mock_storage, private_key):
        """Blocked domains are detected when ``actor`` is an embedded object."""
        processor = _make_inbox(
            mock_storage, private_key, blocked_instances=["spam.example"]
        )
        activity = _follow_activity({"id": "https://spam.example/users/bot"})

        result = processor.process(activity, skip_verification=True)

        assert result is None
        mock_requests.get.assert_not_called()

    @patch("pubby.handlers._inbox.requests")
    def test_no_lists_processes_normally(
        self, mock_requests, mock_storage, private_key
    ):
        """Without allow/block lists, any domain is processed."""
        processor = _make_inbox(mock_storage, private_key)
        actor_id = "https://remote.example.com/users/alice"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _remote_actor_data(actor_id)
        mock_resp.raise_for_status = MagicMock()
        mock_requests.get.return_value = mock_resp
        mock_requests.post.return_value = MagicMock(status_code=202)

        result = processor.process(_follow_activity(actor_id), skip_verification=True)

        assert result is not None
        mock_storage.store_follower.assert_called_once()


class TestOutboxEnforcement:
    def _create_activity(self, processor):
        return processor.build_create_activity(
            Object(
                id="https://blog.example.com/post/1",
                type="Article",
                content="<p>Test</p>",
                attributed_to="https://blog.example.com/ap/actor",
            )
        )

    @patch("pubby.handlers._outbox.requests")
    def test_blocked_follower_inbox_skipped(
        self, mock_requests, mock_storage, private_key
    ):
        mock_storage.get_followers.return_value = [
            Follower(
                actor_id="https://good.example/users/alice",
                inbox="https://good.example/inbox",
            ),
            Follower(
                actor_id="https://spam.example/users/bot",
                inbox="https://spam.example/inbox",
            ),
        ]
        mock_requests.post.return_value = MagicMock(status_code=202)

        processor = _make_outbox(
            mock_storage, private_key, blocked_instances=["spam.example"]
        )
        processor.publish(self._create_activity(processor))

        mock_requests.post.assert_called_once()
        assert "good.example" in mock_requests.post.call_args[0][0]

    @patch("pubby.handlers._outbox.requests")
    def test_allow_list_filters_follower_inboxes(
        self, mock_requests, mock_storage, private_key
    ):
        mock_storage.get_followers.return_value = [
            Follower(
                actor_id="https://good.example/users/alice",
                inbox="https://good.example/inbox",
            ),
            Follower(
                actor_id="https://other.example/users/bob",
                inbox="https://other.example/inbox",
            ),
        ]
        mock_requests.post.return_value = MagicMock(status_code=202)

        processor = _make_outbox(
            mock_storage, private_key, allowed_instances=["good.example"]
        )
        processor.publish(self._create_activity(processor))

        mock_requests.post.assert_called_once()
        assert "good.example" in mock_requests.post.call_args[0][0]

    @patch("pubby.handlers._outbox.requests")
    def test_blocked_recipient_not_contacted(
        self, mock_requests, mock_storage, private_key
    ):
        """A recipient actor on a blocked domain is never fetched or delivered."""
        processor = _make_outbox(
            mock_storage, private_key, blocked_instances=["spam.example"]
        )

        activity = {
            "id": "https://blog.example.com/activities/1",
            "type": "Create",
            "actor": "https://blog.example.com/ap/actor",
            "to": ["https://spam.example/users/bot"],
            "cc": [],
            "object": {"id": "https://blog.example.com/post/1", "type": "Note"},
        }
        processor.publish(activity)

        # The actor document must not be fetched and no delivery attempted.
        mock_requests.get.assert_not_called()
        mock_requests.post.assert_not_called()

    @patch("pubby.handlers._outbox.requests")
    def test_no_lists_delivers_to_all(self, mock_requests, mock_storage, private_key):
        mock_storage.get_followers.return_value = [
            Follower(
                actor_id="https://a.example/users/alice",
                inbox="https://a.example/inbox",
            ),
            Follower(
                actor_id="https://b.example/users/bob",
                inbox="https://b.example/inbox",
            ),
        ]
        mock_requests.post.return_value = MagicMock(status_code=202)

        processor = _make_outbox(mock_storage, private_key)
        processor.publish(self._create_activity(processor))

        assert mock_requests.post.call_count == 2
