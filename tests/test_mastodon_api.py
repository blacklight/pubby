"""
Tests for the Mastodon-compatible API layer.

Covers mappers, framework-agnostic route logic, and all three adapter
bindings (Flask, FastAPI, Tornado).
"""

from datetime import datetime, timezone

import pytest
import sqlalchemy

from pubby.crypto._keys import generate_rsa_keypair
from pubby.handlers import ActivityPubHandler
from pubby.server.mastodon._mappers import (
    _ACCOUNT_LOCAL_ID,
    activity_to_status,
    actor_to_account,
    follower_to_account,
    id_to_url,
    stable_id,
    tag_to_mastodon_tag,
)
from pubby.server.mastodon._routes import MastodonAPI
from pubby.storage.adapters.db import init_db_storage
from pubby._model import Follower


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine():
    return sqlalchemy.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
    )


def _make_handler(engine=None):
    if engine is None:
        engine = _make_engine()
    storage = init_db_storage(engine)
    private_key, _ = generate_rsa_keypair()
    return ActivityPubHandler(
        storage=storage,
        actor_config={
            "base_url": "https://blog.example.com",
            "username": "blog",
            "name": "Test Blog",
            "summary": "A test blog",
            "icon_url": "https://blog.example.com/icon.png",
        },
        private_key=private_key,
        software_name="TestSoftware",
        software_version="1.2.3",
    )


def _store_sample_activity(handler, object_id=None, published=None, tags=None):
    """Store a sample Create activity in the outbox."""
    object_id = object_id or "https://blog.example.com/posts/hello"
    published = published or datetime.now(timezone.utc).isoformat()
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{object_id}#activity",
        "type": "Create",
        "actor": handler.actor_id,
        "published": published,
        "object": {
            "id": object_id,
            "type": "Note",
            "content": "<p>Hello world</p>",
            "url": object_id,
            "attributedTo": handler.actor_id,
            "published": published,
            "tag": tags or [],
            "attachment": [],
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
            "cc": [handler.followers_url],
        },
    }
    handler.storage.store_activity(activity["id"], activity)
    return activity


def _store_sample_follower(handler, actor_id=None):
    """Store a sample follower."""
    actor_id = actor_id or "https://remote.example.com/users/alice"
    follower = Follower(
        actor_id=actor_id,
        inbox=f"{actor_id}/inbox",
        shared_inbox="https://remote.example.com/inbox",
        actor_data={
            "id": actor_id,
            "type": "Person",
            "preferredUsername": "alice",
            "name": "Alice",
            "summary": "A remote user",
            "url": actor_id,
            "icon": {"type": "Image", "url": "https://remote.example.com/avatar.png"},
        },
    )
    handler.storage.store_follower(follower)
    return follower


# ===========================================================================
# Mapper unit tests
# ===========================================================================


class TestStableId:
    def test_roundtrip(self):
        url = "https://blog.example.com/posts/hello-world"
        encoded = stable_id(url)
        assert id_to_url(encoded) == url

    def test_deterministic(self):
        url = "https://blog.example.com/posts/hello"
        assert stable_id(url) == stable_id(url)

    def test_url_safe(self):
        url = "https://blog.example.com/posts/hello?foo=bar&baz=1"
        encoded = stable_id(url)
        # URL-safe base64 uses - and _ instead of + and /
        assert "+" not in encoded
        assert "/" not in encoded


class TestActorToAccount:
    def test_basic_fields(self):
        handler = _make_handler()
        account = actor_to_account(handler)
        assert account["id"] == _ACCOUNT_LOCAL_ID
        assert account["username"] == "blog"
        assert account["acct"] == "blog@blog.example.com"
        assert account["display_name"] == "Test Blog"
        assert account["note"] == "A test blog"
        assert account["avatar"] == "https://blog.example.com/icon.png"
        assert account["bot"] is False
        assert account["discoverable"] is True

    def test_counts_empty(self):
        handler = _make_handler()
        account = actor_to_account(handler)
        assert account["followers_count"] == 0
        assert account["following_count"] == 0
        assert account["statuses_count"] == 0

    def test_counts_with_data(self):
        handler = _make_handler()
        _store_sample_activity(handler)
        _store_sample_follower(handler)
        account = actor_to_account(handler)
        assert account["followers_count"] == 1
        assert account["statuses_count"] == 1


class TestActivityToStatus:
    def test_basic_fields(self):
        handler = _make_handler()
        activity = _store_sample_activity(handler)
        status = activity_to_status(activity, handler)
        assert status["id"] == stable_id("https://blog.example.com/posts/hello")
        assert status["content"] == "<p>Hello world</p>"
        assert status["visibility"] == "public"
        assert status["url"] == "https://blog.example.com/posts/hello"
        assert status["account"]["id"] == _ACCOUNT_LOCAL_ID
        assert status["application"]["name"] == "TestSoftware"

    def test_with_tags(self):
        handler = _make_handler()
        tags = [
            {
                "type": "Hashtag",
                "name": "#python",
                "href": "https://blog.example.com/tags/python",
            }
        ]
        activity = _store_sample_activity(handler, tags=tags)
        status = activity_to_status(activity, handler)
        assert len(status["tags"]) == 1
        assert status["tags"][0]["name"] == "python"

    def test_object_url_as_link_list_prefers_html(self):
        handler = _make_handler()
        activity = _store_sample_activity(handler)
        activity["object"]["type"] = "Audio"
        activity["object"]["url"] = [
            {
                "type": "Link",
                "href": "https://blog.example.com/files/1/download",
                "mediaType": "audio/mpeg",
            },
            {
                "type": "Link",
                "href": "https://blog.example.com/posts/hello",
                "mediaType": "text/html",
            },
        ]
        status = activity_to_status(activity, handler)
        assert status["url"] == "https://blog.example.com/posts/hello"
        assert isinstance(status["url"], str)

    def test_object_url_as_link_list_falls_back_to_first_href(self):
        handler = _make_handler()
        activity = _store_sample_activity(handler)
        activity["object"]["url"] = [
            {
                "type": "Link",
                "href": "https://blog.example.com/files/1/download",
                "mediaType": "audio/mpeg",
            }
        ]
        status = activity_to_status(activity, handler)
        assert status["url"] == "https://blog.example.com/files/1/download"


class TestFollowerToAccount:
    def test_basic_fields(self):
        handler = _make_handler()
        follower = _store_sample_follower(handler)
        account = follower_to_account(follower)
        assert account["username"] == "alice"
        assert account["display_name"] == "Alice"
        assert "remote.example.com" in account["acct"]
        assert account["avatar"] == "https://remote.example.com/avatar.png"

    def test_minimal_follower(self):
        follower = Follower(
            actor_id="https://other.example.com/users/bob",
            inbox="https://other.example.com/users/bob/inbox",
        )
        account = follower_to_account(follower)
        assert account["uri"] == "https://other.example.com/users/bob"


class TestTagToMastodonTag:
    def test_basic(self):
        tag = tag_to_mastodon_tag("Python", "https://blog.example.com")
        assert tag["name"] == "python"
        assert tag["url"] == "https://blog.example.com/tags/python"
        assert tag["history"] == []


# ===========================================================================
# Route logic (framework-agnostic) tests
# ===========================================================================


class TestMastodonAPIRoutes:
    @pytest.fixture
    def api(self):
        handler = _make_handler()
        return MastodonAPI(
            handler,
            title="My Blog",
            contact_email="me@example.com",
        )

    @pytest.fixture
    def api_with_data(self):
        handler = _make_handler()
        _store_sample_activity(handler)
        _store_sample_activity(
            handler,
            object_id="https://blog.example.com/posts/second",
        )
        _store_sample_follower(handler)
        return MastodonAPI(
            handler,
            title="My Blog",
            contact_email="me@example.com",
        )

    def test_instance_v1(self, api):
        body, status = api.instance_v1()
        assert status == 200
        assert body["title"] == "My Blog"
        assert body["uri"] == "blog.example.com"
        assert body["registrations"] is False
        assert body["contact_account"]["id"] == _ACCOUNT_LOCAL_ID
        assert "Mastodon-compatible" in body["version"]

    def test_instance_v2(self, api):
        body, status = api.instance_v2()
        assert status == 200
        assert body["domain"] == "blog.example.com"
        assert body["registrations"]["enabled"] is False

    def test_instance_peers_empty(self, api):
        body, status = api.instance_peers()
        assert status == 200
        assert body == []

    def test_instance_peers_with_followers(self, api_with_data):
        body, status = api_with_data.instance_peers()
        assert status == 200
        assert "remote.example.com" in body

    def test_accounts_lookup_success(self, api):
        body, status = api.accounts_lookup("blog@blog.example.com")
        assert status == 200
        assert body["username"] == "blog"

    def test_accounts_lookup_with_leading_at(self, api):
        _, status = api.accounts_lookup("@blog@blog.example.com")
        assert status == 200

    def test_accounts_lookup_bare_username(self, api):
        _, status = api.accounts_lookup("blog")
        assert status == 200

    def test_accounts_lookup_not_found(self, api):
        _, status = api.accounts_lookup("other@remote.example.com")
        assert status == 404

    def test_accounts_lookup_missing(self, api):
        _, status = api.accounts_lookup(None)
        assert status == 400

    def test_accounts_get_success(self, api):
        body, status = api.accounts_get(_ACCOUNT_LOCAL_ID)
        assert status == 200
        assert body["username"] == "blog"

    def test_accounts_get_not_found(self, api):
        _, status = api.accounts_get("999")
        assert status == 404

    def test_accounts_statuses(self, api_with_data):
        body, status = api_with_data.accounts_statuses(_ACCOUNT_LOCAL_ID)
        assert status == 200
        assert isinstance(body, list)
        assert len(body) == 2

    def test_accounts_statuses_limit(self, api_with_data):
        body, status = api_with_data.accounts_statuses(_ACCOUNT_LOCAL_ID, limit=1)
        assert status == 200
        assert len(body) == 1

    def test_accounts_statuses_not_found(self, api):
        _, status = api.accounts_statuses("999")
        assert status == 404

    def test_accounts_followers(self, api_with_data):
        body, status = api_with_data.accounts_followers(_ACCOUNT_LOCAL_ID)
        assert status == 200
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["username"] == "alice"

    def test_accounts_followers_not_found(self, api):
        _, status = api.accounts_followers("999")
        assert status == 404

    def test_statuses_get(self, api_with_data):
        sid = stable_id("https://blog.example.com/posts/hello")
        body, status = api_with_data.statuses_get(sid)
        assert status == 200
        assert body["content"] == "<p>Hello world</p>"

    def test_statuses_get_not_found(self, api):
        _, status = api.statuses_get("nonexistent")
        assert status == 404


# ===========================================================================
# Adapter integration tests (Flask, FastAPI, Tornado)
# ===========================================================================


class _AdapterClient:
    """Thin wrapper unifying HTTP test clients across frameworks."""

    def get(self, *_, **__):
        raise NotImplementedError

    def post(self, *_, **__):
        raise NotImplementedError


class _FlaskMastodonClient(_AdapterClient):
    def __init__(self, handler):
        from flask import Flask
        from pubby.server.adapters.flask_mastodon import bind_mastodon_api

        app = Flask(__name__)
        app.config["TESTING"] = True
        bind_mastodon_api(
            app, handler, title="Test Blog", contact_email="test@example.com"
        )
        self._client = app.test_client()

    def get(self, path, headers=None):
        resp = self._client.get(path, headers=headers or {})
        try:
            data = resp.get_json()
        except Exception:
            data = None
        return resp.status_code, data, resp.content_type or ""


class _FastAPIMastodonClient(_AdapterClient):
    def __init__(self, handler):
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        from pubby.server.adapters.fastapi_mastodon import bind_mastodon_api

        app = FastAPI()
        bind_mastodon_api(
            app, handler, title="Test Blog", contact_email="test@example.com"
        )
        self._client = TestClient(app)

    def get(self, path, headers=None):
        resp = self._client.get(path, headers=headers or {})
        ct = resp.headers.get("content-type", "")
        try:
            data = resp.json()
        except Exception:
            data = None
        return resp.status_code, data, ct


class _TornadoMastodonClient(_AdapterClient):
    def __init__(self, handler):
        import threading

        import tornado.ioloop
        import tornado.web
        from tornado.httpserver import HTTPServer
        from tornado.testing import bind_unused_port

        from pubby.server.adapters.tornado_mastodon import bind_mastodon_api

        self._loop = tornado.ioloop.IOLoop()
        app = tornado.web.Application()
        bind_mastodon_api(
            app, handler, title="Test Blog", contact_email="test@example.com"
        )
        server = HTTPServer(app)
        sock, port = bind_unused_port()
        server.add_socket(sock)
        self._port = port
        self._server = server
        self._sock = sock
        self._thread = threading.Thread(target=self._loop.start, daemon=True)
        self._loop.add_callback(lambda: None)
        self._thread.start()

    def stop(self):
        self._server.stop()
        self._sock.close()
        self._loop.add_callback(self._loop.stop)
        self._thread.join(timeout=5)

    def _url(self, path):
        return f"http://127.0.0.1:{self._port}{path}"

    def get(self, path, headers=None):
        import requests as req

        resp = req.get(self._url(path), headers=headers or {}, timeout=5)
        ct = resp.headers.get("content-type", "")
        try:
            data = resp.json()
        except Exception:
            data = None
        return resp.status_code, data, ct


@pytest.fixture(params=["flask", "fastapi", "tornado"])
def mastodon_client(request):
    handler = _make_handler()
    _store_sample_activity(handler)
    _store_sample_follower(handler)

    client_cls = {
        "flask": _FlaskMastodonClient,
        "fastapi": _FastAPIMastodonClient,
        "tornado": _TornadoMastodonClient,
    }[request.param]

    client = client_cls(handler)
    yield client

    if request.param == "tornado":
        client.stop()


class TestMastodonInstance:
    def test_v1(self, mastodon_client):
        status, data, _ = mastodon_client.get("/api/v1/instance")
        assert status == 200
        assert data["title"] == "Test Blog"
        assert data["stats"]["user_count"] == 1
        assert data["stats"]["status_count"] >= 1
        assert data["registrations"] is False

    def test_v2(self, mastodon_client):
        status, data, _ = mastodon_client.get("/api/v2/instance")
        assert status == 200
        assert data["domain"] == "blog.example.com"

    def test_peers(self, mastodon_client):
        status, data, _ = mastodon_client.get("/api/v1/instance/peers")
        assert status == 200
        assert isinstance(data, list)
        assert "remote.example.com" in data


class TestMastodonNodeInfoAliases:
    def test_nodeinfo_21_json(self, mastodon_client):
        status, data, _ = mastodon_client.get("/nodeinfo/2.1.json")
        assert status == 200
        assert data["version"] == "2.1"


class TestMastodonAccounts:
    def test_lookup(self, mastodon_client):
        status, data, _ = mastodon_client.get(
            "/api/v1/accounts/lookup?acct=blog@blog.example.com"
        )
        assert status == 200
        assert data["username"] == "blog"

    def test_lookup_not_found(self, mastodon_client):
        status, _, __ = mastodon_client.get(
            "/api/v1/accounts/lookup?acct=nope@other.com"
        )
        assert status == 404

    def test_get(self, mastodon_client):
        status, data, _ = mastodon_client.get(f"/api/v1/accounts/{_ACCOUNT_LOCAL_ID}")
        assert status == 200
        assert data["username"] == "blog"

    def test_get_not_found(self, mastodon_client):
        status, _, __ = mastodon_client.get("/api/v1/accounts/999")
        assert status == 404

    def test_statuses(self, mastodon_client):
        status, data, _ = mastodon_client.get(
            f"/api/v1/accounts/{_ACCOUNT_LOCAL_ID}/statuses"
        )
        assert status == 200
        assert isinstance(data, list)
        assert len(data) >= 1
        assert "content" in data[0]

    def test_followers(self, mastodon_client):
        status, data, _ = mastodon_client.get(
            f"/api/v1/accounts/{_ACCOUNT_LOCAL_ID}/followers"
        )
        assert status == 200
        assert isinstance(data, list)
        assert len(data) == 1


class TestMastodonStatuses:
    def test_get(self, mastodon_client):
        sid = stable_id("https://blog.example.com/posts/hello")
        status, data, _ = mastodon_client.get(f"/api/v1/statuses/{sid}")
        assert status == 200
        assert data["content"] == "<p>Hello world</p>"

    def test_get_not_found(self, mastodon_client):
        status, _, __ = mastodon_client.get("/api/v1/statuses/nonexistent")
        assert status == 404
