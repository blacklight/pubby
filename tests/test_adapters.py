"""
Shared parametrized tests for all server adapters (Flask, FastAPI, Tornado).

Each adapter provides a thin HTTP client wrapper via fixtures in conftest.py.
"""

import json


class TestWebFinger:
    def test_success(self, adapter_client):
        status, data, ct = adapter_client.get(
            "/.well-known/webfinger?resource=acct:blog@blog.example.com"
        )
        assert status == 200
        assert data["subject"] == "acct:blog@blog.example.com"
        assert "jrd+json" in ct

    def test_missing_resource(self, adapter_client):
        status, _, __ = adapter_client.get("/.well-known/webfinger")
        assert status == 400

    def test_wrong_user(self, adapter_client):
        status, _, __ = adapter_client.get(
            "/.well-known/webfinger?resource=acct:other@blog.example.com"
        )
        assert status == 404


class TestNodeInfo:
    def test_discovery(self, adapter_client):
        status, data, _ = adapter_client.get("/.well-known/nodeinfo")
        assert status == 200
        assert len(data["links"]) == 1

    def test_document(self, adapter_client):
        status, data, _ = adapter_client.get("/nodeinfo/2.1")
        assert status == 200
        assert data["version"] == "2.1"
        assert "activitypub" in data["protocols"]


class TestActor:
    def test_get_actor(self, adapter_client):
        status, data, _ = adapter_client.get(
            "/ap/actor",
            headers={"Accept": "application/activity+json"},
        )
        assert status == 200
        assert data["type"] == "Person"
        assert data["preferredUsername"] == "blog"
        assert "publicKey" in data
        assert data["inbox"].endswith("/ap/inbox")
        assert data["outbox"].endswith("/ap/outbox")

    def test_content_type(self, adapter_client):
        _, __, ct = adapter_client.get(
            "/ap/actor",
            headers={"Accept": "application/activity+json"},
        )
        assert "application/activity+json" in ct


class TestInbox:
    def test_invalid_json(self, adapter_client):
        status, _, __ = adapter_client.post(
            "/ap/inbox",
            data=b"not json",
            headers={"Content-Type": "application/activity+json"},
        )
        assert status == 400

    def test_missing_signature(self, adapter_client):
        activity = {
            "id": "https://remote.example.com/activity/1",
            "type": "Follow",
            "actor": "https://remote.example.com/users/alice",
            "object": "https://blog.example.com/ap/actor",
        }
        status, _, __ = adapter_client.post(
            "/ap/inbox",
            data=json.dumps(activity).encode(),
            headers={"Content-Type": "application/activity+json"},
        )
        assert status == 401


class TestOutbox:
    def test_get_outbox(self, adapter_client):
        status, data, _ = adapter_client.get("/ap/outbox")
        assert status == 200
        assert data["type"] == "OrderedCollection"
        assert "orderedItems" in data

    def test_content_type(self, adapter_client):
        _, __, ct = adapter_client.get("/ap/outbox")
        assert "application/activity+json" in ct


class TestFollowers:
    def test_get_followers(self, adapter_client):
        status, data, _ = adapter_client.get("/ap/followers")
        assert status == 200
        assert data["type"] == "OrderedCollection"
        assert data["totalItems"] == 0

    def test_content_type(self, adapter_client):
        _, __, ct = adapter_client.get("/ap/followers")
        assert "application/activity+json" in ct


class TestFollowing:
    def test_get_following(self, adapter_client):
        status, data, _ = adapter_client.get("/ap/following")
        assert status == 200
        assert data["type"] == "OrderedCollection"
        assert data["totalItems"] == 0


class TestRateLimiting:
    def test_rate_limit_enforced(self, rate_limited_client):
        activity = json.dumps(
            {"type": "Follow", "id": "x", "actor": "y", "object": "z"}
        )

        # First two should get through (will fail sig verification, that's fine)
        rate_limited_client.post(
            "/ap/inbox",
            data=activity.encode(),
            headers={"Content-Type": "application/activity+json"},
        )
        rate_limited_client.post(
            "/ap/inbox",
            data=activity.encode(),
            headers={"Content-Type": "application/activity+json"},
        )

        # Third should be rate-limited
        status, _, __ = rate_limited_client.post(
            "/ap/inbox",
            data=activity.encode(),
            headers={"Content-Type": "application/activity+json"},
        )
        assert status == 429
