"""
Shared test fixtures.
"""

from dataclasses import dataclass
from typing import Optional

import pytest

from pubby._rate_limit import RateLimiter
from pubby.crypto._keys import export_public_key_pem, generate_rsa_keypair
from pubby.handlers import ActivityPubHandler
from pubby.storage.adapters.db import init_db_storage


@pytest.fixture
def rsa_keypair():
    """Generate an RSA key pair for tests."""
    private_key, public_key = generate_rsa_keypair(key_size=2048)
    return private_key, public_key


@pytest.fixture
def private_key(rsa_keypair):
    return rsa_keypair[0]


@pytest.fixture
def public_key(rsa_keypair):
    return rsa_keypair[1]


@pytest.fixture
def public_key_pem(public_key):
    return export_public_key_pem(public_key)


@pytest.fixture
def actor_config():
    """Standard test actor configuration."""
    return {
        "base_url": "https://blog.example.com",
        "username": "blog",
        "name": "Test Blog",
        "summary": "A test blog",
        "icon_url": "https://blog.example.com/icon.png",
    }


# ---------------------------------------------------------------------------
# Adapter client infrastructure
# ---------------------------------------------------------------------------


@dataclass
class AdapterResponse:
    status_code: int
    data: Optional[dict]
    content_type: str


class AdapterClient:
    """Thin wrapper unifying HTTP test clients across frameworks."""

    def get(self, path, headers=None):
        raise NotImplementedError

    def post(self, path, data=None, headers=None):
        raise NotImplementedError


class FlaskAdapterClient(AdapterClient):
    def __init__(self, handler, rate_limiter=None):
        from flask import Flask
        from pubby.server.adapters.flask import bind_activitypub

        app = Flask(__name__)
        app.config["TESTING"] = True
        bind_activitypub(app, handler, rate_limiter=rate_limiter)
        self._client = app.test_client()
        self._handler = handler

    def get(self, path, headers=None):
        resp = self._client.get(path, headers=headers or {})
        try:
            data = resp.get_json()
        except Exception:
            data = None
        return resp.status_code, data, resp.content_type or ""

    def post(self, path, data=None, headers=None):
        resp = self._client.post(path, data=data, headers=headers or {})
        try:
            d = resp.get_json()
        except Exception:
            d = None
        return resp.status_code, d, resp.content_type or ""


class FastAPIAdapterClient(AdapterClient):
    def __init__(self, handler, rate_limiter=None):
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        from pubby.server.adapters.fastapi import bind_activitypub

        app = FastAPI()
        bind_activitypub(app, handler, rate_limiter=rate_limiter)
        self._client = TestClient(app)
        self._handler = handler

    def get(self, path, headers=None):
        resp = self._client.get(path, headers=headers or {})
        ct = resp.headers.get("content-type", "")
        try:
            data = resp.json()
        except Exception:
            data = None
        return resp.status_code, data, ct

    def post(self, path, data=None, headers=None):
        resp = self._client.post(path, content=data, headers=headers or {})
        ct = resp.headers.get("content-type", "")
        try:
            d = resp.json()
        except Exception:
            d = None
        return resp.status_code, d, ct


class TornadoAdapterClient(AdapterClient):
    def __init__(self, handler, rate_limiter=None):
        import threading

        import tornado.ioloop
        import tornado.web
        from tornado.httpserver import HTTPServer
        from tornado.testing import bind_unused_port

        from pubby.server.adapters.tornado import bind_activitypub

        # Create a dedicated IOLoop in a background thread
        self._loop = tornado.ioloop.IOLoop()
        app = tornado.web.Application()
        bind_activitypub(app, handler, rate_limiter=rate_limiter)
        server = HTTPServer(app)
        sock, port = bind_unused_port()
        server.add_socket(sock)
        self._port = port
        self._server = server
        self._sock = sock
        self._handler = handler
        self._thread = threading.Thread(target=self._loop.start, daemon=True)
        self._loop.add_callback(lambda: None)  # ensure loop is started
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

    def post(self, path, data=None, headers=None):
        import requests as req

        resp = req.post(self._url(path), data=data, headers=headers or {}, timeout=5)
        ct = resp.headers.get("content-type", "")
        try:
            d = resp.json()
        except Exception:
            d = None
        return resp.status_code, d, ct


def _make_handler(rate_limiter=None):
    # Use StaticPool to share in-memory SQLite across threads
    # (needed for Tornado's threaded test client)
    import sqlalchemy

    engine = sqlalchemy.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
    )
    storage = init_db_storage(engine)
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


@pytest.fixture(params=["flask", "fastapi", "tornado"])
def adapter_client(request):
    """Parametrized fixture yielding an AdapterClient for each framework."""
    handler = _make_handler()
    client_cls = {
        "flask": FlaskAdapterClient,
        "fastapi": FastAPIAdapterClient,
        "tornado": TornadoAdapterClient,
    }[request.param]

    client = client_cls(handler)
    yield client

    if request.param == "tornado":
        client.stop()


@pytest.fixture(params=["flask", "fastapi", "tornado"])
def rate_limited_client(request):
    """Parametrized fixture yielding a rate-limited AdapterClient."""
    handler = _make_handler()
    rate_limiter = RateLimiter(max_requests=2, window_seconds=60)
    client_cls = {
        "flask": FlaskAdapterClient,
        "fastapi": FastAPIAdapterClient,
        "tornado": TornadoAdapterClient,
    }[request.param]

    client = client_cls(handler, rate_limiter=rate_limiter)
    yield client

    if request.param == "tornado":
        client.stop()
