"""
Tests for WebFinger and NodeInfo response builders.
"""

from mypub.handlers._discovery import (
    build_nodeinfo_discovery,
    build_nodeinfo_document,
    build_webfinger_response,
)


class TestWebFinger:
    def test_basic_response(self):
        result = build_webfinger_response(
            username="blog",
            domain="example.com",
            actor_url="https://example.com/ap/actor",
        )

        assert result["subject"] == "acct:blog@example.com"
        assert "https://example.com/ap/actor" in result["aliases"]

        links = result["links"]
        assert len(links) == 2

        # Self link
        self_link = next(l for l in links if l["rel"] == "self")
        assert self_link["type"] == "application/activity+json"
        assert self_link["href"] == "https://example.com/ap/actor"

        # Profile page link
        profile_link = next(
            l for l in links if l["rel"] == "http://webfinger.net/rel/profile-page"
        )
        assert profile_link["type"] == "text/html"

    def test_custom_domain(self):
        """WebFinger domain can differ from actor URL domain."""
        result = build_webfinger_response(
            username="blog",
            domain="apex.example.com",
            actor_url="https://blog.sub.example.com/ap/actor",
        )

        assert result["subject"] == "acct:blog@apex.example.com"
        assert result["links"][0]["href"] == "https://blog.sub.example.com/ap/actor"


class TestNodeInfoDiscovery:
    def test_discovery_document(self):
        result = build_nodeinfo_discovery("https://blog.example.com")

        assert len(result["links"]) == 1
        link = result["links"][0]
        assert (
            link["rel"]
            == "http://nodeinfo.diaspora.software/ns/schema/2.1"
        )
        assert link["href"] == "https://blog.example.com/nodeinfo/2.1"

    def test_trailing_slash_stripped(self):
        result = build_nodeinfo_discovery("https://blog.example.com/")
        assert result["links"][0]["href"] == "https://blog.example.com/nodeinfo/2.1"


class TestNodeInfoDocument:
    def test_default_document(self):
        result = build_nodeinfo_document()

        assert result["version"] == "2.1"
        assert result["software"]["name"] == "mypub"
        assert result["software"]["version"] == "0.0.1"
        assert "activitypub" in result["protocols"]
        assert result["usage"]["users"]["total"] == 1
        assert result["openRegistrations"] is False

    def test_custom_values(self):
        result = build_nodeinfo_document(
            software_name="myblog",
            software_version="1.0.0",
            total_users=5,
            total_posts=42,
            open_registrations=True,
        )

        assert result["software"]["name"] == "myblog"
        assert result["software"]["version"] == "1.0.0"
        assert result["usage"]["users"]["total"] == 5
        assert result["usage"]["localPosts"] == 42
        assert result["openRegistrations"] is True
