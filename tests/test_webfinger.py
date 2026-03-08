"""
Tests for pubby.webfinger — mention extraction and actor URL resolution.
"""

from unittest.mock import MagicMock, patch

from pubby.webfinger import Mention, extract_mentions, resolve_actor_url


class TestMention:
    def test_acct(self):
        m = Mention(
            username="alice",
            domain="example.com",
            actor_url="https://example.com/users/alice",
        )
        assert m.acct == "@alice@example.com"

    def test_to_tag(self):
        m = Mention(
            username="alice",
            domain="example.com",
            actor_url="https://example.com/users/alice",
        )
        tag = m.to_tag()
        assert tag == {
            "type": "Mention",
            "href": "https://example.com/users/alice",
            "name": "@alice@example.com",
        }


class TestResolveActorUrl:
    @patch("pubby.webfinger.requests")
    def test_resolves_via_webfinger(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "links": [
                {
                    "rel": "self",
                    "type": "application/activity+json",
                    "href": "https://mastodon.social/users/alice",
                },
            ]
        }
        mock_requests.get.return_value = mock_resp

        url = resolve_actor_url("alice", "mastodon.social")
        assert url == "https://mastodon.social/users/alice"
        mock_requests.get.assert_called_once()

    @patch("pubby.webfinger.requests")
    def test_fallback_on_failure(self, mock_requests):
        mock_requests.get.side_effect = Exception("connection error")
        url = resolve_actor_url("alice", "mastodon.social")
        assert url == "https://mastodon.social/@alice"

    @patch("pubby.webfinger.requests")
    def test_fallback_on_no_self_link(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "links": [
                {"rel": "other", "type": "text/html", "href": "https://example.com"}
            ]
        }
        mock_requests.get.return_value = mock_resp

        url = resolve_actor_url("alice", "example.com")
        assert url == "https://example.com/@alice"

    @patch("pubby.webfinger.requests")
    def test_ignores_non_application_type(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "links": [
                {
                    "rel": "self",
                    "type": "text/html",
                    "href": "https://example.com/@alice",
                },
                {
                    "rel": "self",
                    "type": "application/ld+json",
                    "href": "https://example.com/users/alice",
                },
            ]
        }
        mock_requests.get.return_value = mock_resp

        url = resolve_actor_url("alice", "example.com")
        assert url == "https://example.com/users/alice"


class TestExtractMentions:
    @patch("pubby.webfinger.resolve_actor_url")
    def test_extracts_single_mention(self, mock_resolve):
        mock_resolve.return_value = "https://mastodon.social/users/alice"
        mentions = extract_mentions("Hello @alice@mastodon.social!")
        assert len(mentions) == 1
        assert mentions[0].username == "alice"
        assert mentions[0].domain == "mastodon.social"

    @patch("pubby.webfinger.resolve_actor_url")
    def test_extracts_multiple_mentions(self, mock_resolve):
        mock_resolve.side_effect = lambda u, d, **_: f"https://{d}/users/{u}"
        text = "Hey @alice@example.com and @bob@other.org"
        mentions = extract_mentions(text)
        assert len(mentions) == 2
        assert {m.username for m in mentions} == {"alice", "bob"}

    @patch("pubby.webfinger.resolve_actor_url")
    def test_deduplicates_mentions(self, mock_resolve):
        mock_resolve.return_value = "https://example.com/users/alice"
        text = "@alice@example.com said @alice@example.com"
        mentions = extract_mentions(text)
        assert len(mentions) == 1

    @patch("pubby.webfinger.resolve_actor_url")
    def test_deduplicates_case_insensitive(self, mock_resolve):
        mock_resolve.return_value = "https://example.com/users/alice"
        text = "@Alice@Example.COM said @alice@example.com"
        mentions = extract_mentions(text)
        assert len(mentions) == 1

    def test_no_mentions(self):
        mentions = extract_mentions("Hello world, no mentions here")
        assert mentions == []

    def test_ignores_email_like_patterns(self):
        # @ preceded by word char should not match
        mentions = extract_mentions("email alice@example.com")
        assert mentions == []
