"""
Tests for the Flask server adapter.
"""

import json

import pytest
from flask import Flask

from pubby._rate_limit import RateLimiter
from pubby.crypto._keys import generate_rsa_keypair
from pubby.handlers import ActivityPubHandler
from pubby.server.adapters.flask import bind_activitypub
from pubby.storage.adapters.db import init_db_storage


@pytest.fixture
def handler():
    """Create a handler with in-memory DB storage."""
    storage = init_db_storage("sqlite:///:memory:")
    private_key, _ = generate_rsa_keypair()

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


@pytest.fixture
def app(handler):
    """Create a Flask test app with ActivityPub routes."""
    app = Flask(__name__)
    app.config["TESTING"] = True
    bind_activitypub(app, handler)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


class TestWebFingerRoute:
    def test_webfinger_success(self, client):
        resp = client.get("/.well-known/webfinger?resource=acct:blog@blog.example.com")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["subject"] == "acct:blog@blog.example.com"
        assert resp.content_type.startswith("application/jrd+json")

    def test_webfinger_missing_resource(self, client):
        resp = client.get("/.well-known/webfinger")
        assert resp.status_code == 400

    def test_webfinger_wrong_user(self, client):
        resp = client.get("/.well-known/webfinger?resource=acct:other@blog.example.com")
        assert resp.status_code == 404


class TestNodeInfoRoutes:
    def test_nodeinfo_discovery(self, client):
        resp = client.get("/.well-known/nodeinfo")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["links"]) == 1

    def test_nodeinfo_document(self, client):
        resp = client.get("/nodeinfo/2.1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["version"] == "2.1"
        assert "activitypub" in data["protocols"]


class TestActorRoute:
    def test_get_actor(self, client):
        resp = client.get(
            "/ap/actor",
            headers={"Accept": "application/activity+json"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["type"] == "Person"
        assert data["preferredUsername"] == "blog"
        assert "publicKey" in data
        assert data["inbox"].endswith("/ap/inbox")
        assert data["outbox"].endswith("/ap/outbox")

    def test_actor_content_type(self, client):
        resp = client.get(
            "/ap/actor",
            headers={"Accept": "application/activity+json"},
        )
        assert "application/activity+json" in resp.content_type


class TestInboxRoute:
    def test_inbox_invalid_json(self, client):
        resp = client.post(
            "/ap/inbox",
            data=b"not json",
            content_type="application/activity+json",
        )
        assert resp.status_code == 400

    def test_inbox_missing_signature(self, client):
        activity = {
            "id": "https://remote.example.com/activity/1",
            "type": "Follow",
            "actor": "https://remote.example.com/users/alice",
            "object": "https://blog.example.com/ap/actor",
        }
        resp = client.post(
            "/ap/inbox",
            data=json.dumps(activity),
            content_type="application/activity+json",
        )
        # Should fail signature verification
        assert resp.status_code == 401


class TestOutboxRoute:
    def test_get_outbox(self, client):
        resp = client.get("/ap/outbox")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["type"] == "OrderedCollection"
        assert "orderedItems" in data

    def test_outbox_content_type(self, client):
        resp = client.get("/ap/outbox")
        assert "application/activity+json" in resp.content_type


class TestFollowersRoute:
    def test_get_followers(self, client):
        resp = client.get("/ap/followers")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["type"] == "OrderedCollection"
        assert data["totalItems"] == 0

    def test_followers_content_type(self, client):
        resp = client.get("/ap/followers")
        assert "application/activity+json" in resp.content_type


class TestFollowingRoute:
    def test_get_following(self, client):
        resp = client.get("/ap/following")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["type"] == "OrderedCollection"
        assert data["totalItems"] == 0


class TestRateLimiting:
    def test_rate_limit_enforced(self, handler):
        app = Flask(__name__)
        app.config["TESTING"] = True
        rate_limiter = RateLimiter(max_requests=2, window_seconds=60)
        bind_activitypub(app, handler, rate_limiter=rate_limiter)
        client = app.test_client()

        activity = json.dumps(
            {"type": "Follow", "id": "x", "actor": "y", "object": "z"}
        )

        # First two requests should be OK (well, they'll fail signature verification)
        client.post(
            "/ap/inbox", data=activity, content_type="application/activity+json"
        )
        client.post(
            "/ap/inbox", data=activity, content_type="application/activity+json"
        )

        # Third request should be rate-limited
        resp3 = client.post(
            "/ap/inbox", data=activity, content_type="application/activity+json"
        )
        assert resp3.status_code == 429
