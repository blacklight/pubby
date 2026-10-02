"""
Tests for pubby.cache — TTL document cache with single-flight coalescing.
"""

import asyncio
import json
import threading
import time
import urllib.parse
from unittest.mock import patch

import pytest

from pubby import (
    CachedResponse,
    DocumentCache,
    DocumentStore,
    InMemoryDocumentStore,
)
from pubby import cache as _cache
from pubby._rate_limit import RateLimiter
from pubby.cache import RedisDocumentStore
from pubby.crypto._keys import generate_rsa_keypair
from pubby.handlers import ActivityPubHandler
from pubby.storage.adapters.db import (
    get_db_storage,
    init_db_storage,
    reset_db_storage,
)

try:
    import fakeredis

    HAS_FAKEREDIS = True
except ImportError:
    HAS_FAKEREDIS = False


@pytest.fixture(autouse=True)
def _reset_memoized_storage():
    reset_db_storage()
    yield
    reset_db_storage()


# ---------------------------------------------------------------------------
# InMemoryDocumentStore
# ---------------------------------------------------------------------------


class TestInMemoryDocumentStore:
    def test_set_get_round_trip(self):
        store = InMemoryDocumentStore()
        store.set(("a", "b"), {"doc": 1}, ttl=60)
        entry = store.get(("a", "b"))
        assert entry is not None
        assert entry.value == {"doc": 1}
        assert entry.fresh

    def test_expired_entry_is_not_fresh_but_served_stale(self):
        store = InMemoryDocumentStore()
        store.set(("a",), 1, ttl=0.01, stale_ttl=60)
        time.sleep(0.02)
        entry = store.get(("a",))
        assert entry is not None
        assert not entry.fresh
        assert entry.usable_stale

    def test_entry_dropped_after_stale_window(self):
        store = InMemoryDocumentStore()
        store.set(("a",), 1, ttl=0.01, stale_ttl=0.01)
        time.sleep(0.03)
        assert store.get(("a",)) is None

    def test_bounded_evicts_lru(self):
        store = InMemoryDocumentStore(max_entries=3)
        store.set(("a",), 1, ttl=60)
        store.set(("b",), 2, ttl=60)
        store.set(("c",), 3, ttl=60)
        # Touch ("a",) so ("b",) becomes the LRU candidate.
        assert store.get(("a",)) is not None
        store.set(("d",), 4, ttl=60)
        assert store.get(("b",)) is None
        assert store.get(("a",)) is not None
        assert len(store) == 3

    def test_eviction_drops_expired_entries_first(self):
        store = InMemoryDocumentStore(max_entries=3)
        store.set(("old",), 0, ttl=0.001)
        store.set(("a",), 1, ttl=60)
        store.set(("b",), 2, ttl=60)
        time.sleep(0.01)
        store.set(("c",), 3, ttl=60)
        # The expired entry was dropped instead of a live one.
        assert store.get(("a",)) is not None
        assert store.get(("b",)) is not None
        assert store.get(("c",)) is not None

    def test_delete_prefix_matches_elements(self):
        store = InMemoryDocumentStore()
        store.set(("obj", "u", "x"), 1, ttl=60)
        store.set(("obj", "u", "x", "followers"), 2, ttl=60)
        store.set(("obj", "u2", "x"), 3, ttl=60)
        dropped = store.delete_prefix(("obj", "u"))
        assert dropped == 2
        assert store.get(("obj", "u", "x")) is None
        assert store.get(("obj", "u", "x", "followers")) is None
        assert store.get(("obj", "u2", "x")) is not None

    def test_delete_segment_matches_elements(self):
        store = InMemoryDocumentStore()
        store.set(("obj", "u", "x"), 1, ttl=60)
        store.set(("obj", "u", "x", "followers"), 2, ttl=60)
        store.set(("obj", "u", "x2"), 3, ttl=60)
        dropped = store.delete_segment("x")
        assert dropped == 2
        # "x2" is a different element — substring similarity must not match.
        assert store.get(("obj", "u", "x2")) is not None


# ---------------------------------------------------------------------------
# DocumentCache — synchronous single-flight
# ---------------------------------------------------------------------------


class TestDocumentCacheSync:
    def test_hit_skips_render(self):
        cache = DocumentCache()
        calls = 0

        def render():
            nonlocal calls
            calls += 1
            return {"doc": 1}

        assert cache.get_or_render(("k",), render, ttl=60) == {"doc": 1}
        assert cache.get_or_render(("k",), render, ttl=60) == {"doc": 1}
        assert calls == 1

    def test_concurrent_threads_share_one_render(self):
        cache = DocumentCache()
        started = threading.Event()
        release = threading.Event()
        calls = 0

        def render():
            nonlocal calls
            calls += 1
            started.set()
            release.wait(timeout=5)
            return {"doc": 2}

        results = []
        threads = [
            threading.Thread(
                target=lambda: results.append(
                    cache.get_or_render(("k",), render, ttl=60)
                )
            )
            for _ in range(5)
        ]
        threads[0].start()
        started.wait(timeout=5)
        for t in threads[1:]:
            t.start()
        release.set()
        for t in threads:
            t.join(timeout=5)

        assert results == [{"doc": 2}] * 5
        assert calls == 1

    def test_miss_is_cached_briefly(self):
        cache = DocumentCache()
        calls = 0

        def render():
            nonlocal calls
            calls += 1
            return None

        assert cache.get_or_render(("k",), render, ttl=60) is None
        assert cache.get_or_render(("k",), render, ttl=60) is None
        assert calls == 1

    def test_miss_ttl_capped(self):
        cache = DocumentCache()
        calls = 0

        def render():
            nonlocal calls
            calls += 1
            return None

        # A huge document TTL must not extend the miss TTL past the cap.
        assert cache.get_or_render(("k",), render, ttl=3600, miss_ttl=0.01) is None
        time.sleep(0.02)
        assert cache.get_or_render(("k",), render, ttl=3600, miss_ttl=0.01) is None
        assert calls == 2

    def test_exceptions_propagate_and_are_not_cached(self):
        cache = DocumentCache(stale_factor=0)
        calls = 0

        def render():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("boom")
            return {"doc": 3}

        with pytest.raises(RuntimeError):
            cache.get_or_render(("k",), render, ttl=60)
        assert cache.get_or_render(("k",), render, ttl=60) == {"doc": 3}
        assert calls == 2

    def test_concurrent_exception_propagates_to_all_waiters(self):
        cache = DocumentCache(stale_factor=0)
        started = threading.Event()
        release = threading.Event()
        errors = []

        def render():
            started.set()
            release.wait(timeout=5)
            raise RuntimeError("boom")

        def wait():
            try:
                cache.get_or_render(("k",), render, ttl=60)
            except RuntimeError as e:
                errors.append(e)

        threads = [threading.Thread(target=wait) for _ in range(3)]
        threads[0].start()
        started.wait(timeout=5)
        for t in threads[1:]:
            t.start()
        release.set()
        for t in threads:
            t.join(timeout=5)
        assert len(errors) == 3

    def test_zero_ttl_disables_caching_but_coalesces(self):
        cache = DocumentCache()
        started = threading.Event()
        release = threading.Event()
        calls = 0

        def render():
            nonlocal calls
            calls += 1
            started.set()
            release.wait(timeout=5)
            return calls

        results = []
        threads = [
            threading.Thread(
                target=lambda: results.append(
                    cache.get_or_render(("k",), render, ttl=0)
                )
            )
            for _ in range(3)
        ]
        threads[0].start()
        started.wait(timeout=5)
        for t in threads[1:]:
            t.start()
        release.set()
        for t in threads:
            t.join(timeout=5)
        assert results == [1, 1, 1]
        assert cache.get_or_render(("k",), render, ttl=0) == 2
        assert calls == 2

    def test_invalidation_during_render_prevents_caching(self):
        cache = DocumentCache()
        started = threading.Event()
        release = threading.Event()

        def render():
            started.set()
            release.wait(timeout=5)
            return {"doc": "stale"}

        result = [None]
        t = threading.Thread(
            target=lambda: result.__setitem__(
                0, cache.get_or_render(("k",), render, ttl=60)
            )
        )
        t.start()
        started.wait(timeout=5)
        cache.invalidate(("k",))
        release.set()
        t.join(timeout=5)
        assert result[0] == {"doc": "stale"}  # the waiter still gets it

        calls = 0

        def rerender():
            nonlocal calls
            calls += 1
            return {"doc": "fresh"}

        # …but it was not stored.
        assert cache.get_or_render(("k",), rerender, ttl=60) == {"doc": "fresh"}
        assert calls == 1

    def test_stale_if_error_serves_expired_entry(self):
        cache = DocumentCache(stale_factor=50)
        calls = 0

        def render():
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"doc": "v1"}
            raise RuntimeError("db down")

        assert cache.get_or_render(("k",), render, ttl=0.01) == {"doc": "v1"}
        time.sleep(0.02)  # entry expires but stays within the stale window
        # The re-render fails — the stale value is served instead.
        assert cache.get_or_render(("k",), render, ttl=0.01) == {"doc": "v1"}
        assert calls == 2

    def test_stale_if_error_disabled(self):
        cache = DocumentCache(stale_factor=0)

        def render():
            return {"doc": "v1"}

        cache.get_or_render(("k",), render, ttl=0.01)
        time.sleep(0.02)

        def failing():
            raise RuntimeError("db down")

        with pytest.raises(RuntimeError):
            cache.get_or_render(("k",), failing, ttl=0.01)

    def test_coroutine_render_rejected_in_sync_path(self):
        cache = DocumentCache()

        async def render():
            return {"doc": 1}

        with pytest.raises(TypeError):
            cache.get_or_render(("k",), render, ttl=60)

    def test_tuple_keys_do_not_collide_across_endpoints(self):
        """Regression test: an ``x:followers`` object id must not poison the
        followers collection of object ``x`` (flat ``a:b:c`` keys did)."""
        cache = DocumentCache()
        poisoned = cache.get_or_render(
            ("obj", "u", "x:followers"), lambda: None, ttl=60
        )
        assert poisoned is None

        calls = 0

        def render():
            nonlocal calls
            calls += 1
            return {"collection": True}

        # The real followers collection of object "x" renders unaffected.
        result = cache.get_or_render(("obj", "u", "x", "followers"), render, ttl=60)
        assert result == {"collection": True}
        assert calls == 1


# ---------------------------------------------------------------------------
# DocumentCache — asyncio single-flight
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


class TestDocumentCacheAsync:
    def test_hit_skips_render(self):
        async def main():
            cache = DocumentCache()
            calls = 0

            async def render():
                nonlocal calls
                calls += 1
                return {"doc": 1}

            assert await cache.get_or_render_async(("k",), render, ttl=60) == {"doc": 1}
            assert await cache.get_or_render_async(("k",), render, ttl=60) == {"doc": 1}
            assert calls == 1

        _run(main())

    def test_concurrent_waiters_share_one_render(self):
        async def main():
            cache = DocumentCache()
            started = asyncio.Event()
            release = asyncio.Event()
            calls = 0

            async def render():
                nonlocal calls
                calls += 1
                started.set()
                await release.wait()
                return {"doc": 2}

            first = asyncio.ensure_future(
                cache.get_or_render_async(("k",), render, ttl=60)
            )
            await started.wait()
            second = asyncio.ensure_future(
                cache.get_or_render_async(("k",), render, ttl=60)
            )
            third = asyncio.ensure_future(
                cache.get_or_render_async(("k",), render, ttl=60)
            )
            release.set()

            assert await first == {"doc": 2}
            assert await second == {"doc": 2}
            assert await third == {"doc": 2}
            assert calls == 1

        _run(main())

    def test_cancelled_waiter_does_not_cancel_render(self):
        async def main():
            cache = DocumentCache()
            started = asyncio.Event()
            release = asyncio.Event()

            async def render():
                started.set()
                await release.wait()
                return {"doc": 4}

            first = asyncio.ensure_future(
                cache.get_or_render_async(("k",), render, ttl=60)
            )
            await started.wait()
            second = asyncio.ensure_future(
                cache.get_or_render_async(("k",), render, ttl=60)
            )
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second

            release.set()
            assert await first == {"doc": 4}

            async def rerender():
                return {"doc": 5}

            # The completed render was cached for later callers.
            assert await cache.get_or_render_async(("k",), rerender, ttl=60) == {
                "doc": 4
            }

        _run(main())

    def test_exceptions_propagate_to_all_waiters(self):
        async def main():
            cache = DocumentCache(stale_factor=0)
            started = asyncio.Event()
            release = asyncio.Event()

            async def render():
                started.set()
                await release.wait()
                raise RuntimeError("boom")

            first = asyncio.ensure_future(
                cache.get_or_render_async(("k",), render, ttl=60)
            )
            await started.wait()
            second = asyncio.ensure_future(
                cache.get_or_render_async(("k",), render, ttl=60)
            )
            release.set()

            with pytest.raises(RuntimeError):
                await first
            with pytest.raises(RuntimeError):
                await second

        _run(main())

    def test_invalidation_during_render_prevents_caching(self):
        async def main():
            cache = DocumentCache()
            started = asyncio.Event()
            release = asyncio.Event()

            async def render():
                started.set()
                await release.wait()
                return {"doc": "stale"}

            first = asyncio.ensure_future(
                cache.get_or_render_async(("k",), render, ttl=60)
            )
            await started.wait()
            cache.invalidate(("k",))
            release.set()
            assert await first == {"doc": "stale"}

            calls = 0

            async def rerender():
                nonlocal calls
                calls += 1
                return {"doc": "fresh"}

            assert await cache.get_or_render_async(("k",), rerender, ttl=60) == {
                "doc": "fresh"
            }
            assert calls == 1

        _run(main())

    def test_stale_if_error_serves_expired_entry(self):
        async def main():
            cache = DocumentCache(stale_factor=50)
            calls = 0

            async def render():
                nonlocal calls
                calls += 1
                if calls == 1:
                    return {"doc": "v1"}
                raise RuntimeError("db down")

            assert await cache.get_or_render_async(("k",), render, ttl=0.01) == {
                "doc": "v1"
            }
            await asyncio.sleep(0.02)
            assert await cache.get_or_render_async(("k",), render, ttl=0.01) == {
                "doc": "v1"
            }
            assert calls == 2

        _run(main())

    def test_sync_render_is_supported(self):
        async def main():
            cache = DocumentCache()
            result = await cache.get_or_render_async(("k",), lambda: 42, ttl=60)
            assert result == 42

        _run(main())


# ---------------------------------------------------------------------------
# CachedResponse / header helpers
# ---------------------------------------------------------------------------


class TestCachedResponse:
    def test_document_serializes_and_computes_etag(self):
        resp = CachedResponse.document({"a": 1}, "application/activity+json")
        assert resp.status == 200
        assert json.loads(resp.body) == {"a": 1}
        assert resp.media_type == "application/activity+json"
        assert len(resp.etag) == 40  # sha1 hex

    def test_document_accepts_bytes(self):
        resp = CachedResponse.document(b'{"a":1}')
        assert resp.body == b'{"a":1}'

    def test_redirect(self):
        resp = CachedResponse.redirect("https://example.com/x", status=303)
        assert resp.status == 303
        assert resp.headers["Location"] == "https://example.com/x"

    def test_jsonable_round_trip(self):
        resp = CachedResponse.document({"a": 1}, "application/jrd+json")
        restored = CachedResponse.from_jsonable(resp.to_jsonable())
        assert restored == resp

    def test_cache_headers(self):
        headers = _cache.cache_headers(60, etag="abc123")
        assert headers["Cache-Control"] == "public, max-age=60"
        assert headers["Vary"] == "Accept"
        assert headers["ETag"] == '"abc123"'

    def test_cache_headers_zero_ttl(self):
        # No freshness left but caching is enabled: explicit zero-freshness
        # policy, representation headers preserved.
        headers = _cache.cache_headers(0, etag="abc123", stale=True)
        assert headers["Cache-Control"] == "max-age=0, must-revalidate"
        assert headers["Vary"] == "Accept"
        assert headers["ETag"] == '"abc123"'

    def test_cache_headers_disabled(self):
        # Caching disabled: no-store, but validators still emitted so a
        # 304 keeps its ETag.
        headers = _cache.cache_headers(0, etag="abc123")
        assert headers["Cache-Control"] == "no-store"
        assert headers["Vary"] == "Accept"
        assert headers["ETag"] == '"abc123"'
        assert _cache.cache_headers(-1) == {
            "Cache-Control": "no-store",
            "Vary": "Accept",
        }

    def test_etag_matches(self):
        assert _cache.etag_matches('"abc"', "abc")
        assert _cache.etag_matches('W/"abc"', "abc")
        assert _cache.etag_matches("*", "abc")
        assert _cache.etag_matches('"other", "abc"', "abc")
        assert not _cache.etag_matches('"other"', "abc")
        assert not _cache.etag_matches(None, "abc")


class TestKeyBuilders:
    def test_webfinger_key_normalizes(self):
        assert _cache.webfinger_key("acct:User@Example.com") == (
            "pubby",
            "webfinger",
            "user@example.com",
        )
        assert _cache.webfinger_key("ACCT:@user@example.com") == (
            "pubby",
            "webfinger",
            "user@example.com",
        )

    def test_route_keys_are_namespaced(self):
        """Adapter keys live under the ``pubby`` namespace so a cache shared
        with an application can never mix value types."""
        assert _cache.actor_document_key() == ("pubby", "actor")
        assert _cache.nodeinfo_key() == ("pubby", "nodeinfo")
        assert _cache.nodeinfo_discovery_key() == ("pubby", "nodeinfo-discovery")
        assert _cache.outbox_key(20, 0) == ("pubby", "outbox", 20, 0)
        assert _cache.followers_key() == ("pubby", "followers")
        assert _cache.following_key() == ("pubby", "following")
        assert _cache.quote_authorization_key("x") == (
            "pubby",
            "quote-authorization",
            "x",
        )
        assert _cache.route_key("webfinger") == ("pubby", "webfinger")


# ---------------------------------------------------------------------------
# RedisDocumentStore
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis not installed")
class TestRedisDocumentStore:
    def _store(self):
        from pubby.cache import RedisDocumentStore

        return RedisDocumentStore(fakeredis.FakeRedis())

    def test_set_get_round_trip(self):
        store = self._store()
        store.set(("a", "b"), {"doc": 1}, ttl=60)
        entry = store.get(("a", "b"))
        assert entry is not None
        assert entry.value == {"doc": 1}
        assert entry.fresh

    def test_cached_response_round_trip(self):
        store = self._store()
        resp = CachedResponse.document({"doc": 1}, "application/activity+json")
        store.set(("a",), resp, ttl=60)
        entry = store.get(("a",))
        assert entry is not None
        assert entry.value == resp

    def test_miss_cached(self):
        store = self._store()
        store.set(("a",), None, ttl=10)
        entry = store.get(("a",))
        assert entry is not None
        assert entry.value is None

    def test_delete_prefix(self):
        store = self._store()
        store.set(("obj", "u", "x"), 1, ttl=60)
        store.set(("obj", "u", "x", "followers"), 2, ttl=60)
        store.set(("obj", "u2", "x"), 3, ttl=60)
        assert store.delete_prefix(("obj", "u")) == 2
        assert store.get(("obj", "u", "x")) is None
        assert store.get(("obj", "u2", "x")) is not None

    def test_delete_segment(self):
        store = self._store()
        store.set(("obj", "u", "x"), 1, ttl=60)
        store.set(("obj", "u", "x", "followers"), 2, ttl=60)
        store.set(("obj", "u", "x2"), 3, ttl=60)
        assert store.delete_segment("x") == 2
        assert store.get(("obj", "u", "x2")) is not None

    def test_keys_with_separator_characters_do_not_collide(self):
        store = self._store()
        store.set(("obj", "u", "x:followers"), "poison", ttl=60)
        store.set(("obj", "u", "x", "followers"), "real", ttl=60)
        assert store.get(("obj", "u", "x:followers")).value == "poison"
        assert store.get(("obj", "u", "x", "followers")).value == "real"
        assert store.delete_prefix(("obj", "u", "x")) == 1
        assert store.get(("obj", "u", "x:followers")).value == "poison"

    def test_shared_store_across_cache_instances(self):
        client = fakeredis.FakeRedis()
        from pubby.cache import RedisDocumentStore

        cache_a = DocumentCache(store=RedisDocumentStore(client))
        cache_b = DocumentCache(store=RedisDocumentStore(client))
        cache_a.get_or_render(("k",), lambda: {"v": 1}, ttl=60)
        # A second process (cache instance) sees the stored document and a
        # delete invalidates it for both.
        assert cache_b.get_or_render(("k",), lambda: {"v": 2}, ttl=60) == {"v": 1}
        cache_a.invalidate(("k",))
        assert cache_b.get_or_render(("k",), lambda: {"v": 3}, ttl=60) == {"v": 3}


# ---------------------------------------------------------------------------
# init_db_storage / get_db_storage
# ---------------------------------------------------------------------------


class TestDbStorageHelpers:
    def test_create_tables_false_skips_ddl(self, tmp_path):
        url = f"sqlite:///{tmp_path}/pubby.db"
        storage = init_db_storage(url, create_tables=False)
        import sqlalchemy as sa

        inspector = sa.inspect(storage.engine)
        assert inspector.get_table_names() == []

    def test_get_db_storage_memoizes(self):
        url = "sqlite:///:memory:"
        assert get_db_storage(url) is get_db_storage(url)

    def test_get_db_storage_distinct_configs(self):
        url = "sqlite:///:memory:"
        a = get_db_storage(url, followers_table="followers_a")
        b = get_db_storage(url, followers_table="followers_b")
        assert a is not b

    def test_reset_db_storage(self):
        url = "sqlite:///:memory:"
        first = get_db_storage(url)
        reset_db_storage()
        assert get_db_storage(url) is not first


# ---------------------------------------------------------------------------
# Adapter-level cache behavior
# ---------------------------------------------------------------------------


@pytest.fixture(params=["flask", "fastapi", "tornado"])
def cached_client(request):
    """Adapter client bound with a shared DocumentCache."""
    from tests.conftest import (
        FlaskAdapterClient,
        FastAPIAdapterClient,
        TornadoAdapterClient,
        _make_handler,
    )

    document_cache = DocumentCache()
    handler = _make_handler(document_cache=document_cache)
    client_cls = {
        "flask": FlaskAdapterClient,
        "fastapi": FastAPIAdapterClient,
        "tornado": TornadoAdapterClient,
    }[request.param]
    client = client_cls(handler, document_cache=document_cache)
    yield client
    if request.param == "tornado":
        client.stop()


class TestAdapterDocumentCache:
    def test_second_fetch_is_a_cache_hit(self, cached_client):
        calls = 0
        original = cached_client._handler.get_actor_document

        def spy():
            nonlocal calls
            calls += 1
            return original()

        cached_client._handler.get_actor_document = spy

        status1, doc1, _ = cached_client.get("/ap/actor")
        status2, doc2, _ = cached_client.get("/ap/actor")
        assert status1 == status2 == 200
        assert doc1 == doc2
        assert calls == 1

    def test_cache_headers(self, cached_client):
        resp = cached_client.get_response("/ap/actor")
        assert resp.status_code == 200
        cache_control = resp.headers.get("Cache-Control", "")
        assert "public" in cache_control
        assert "max-age" in cache_control
        assert resp.headers.get("Vary") == "Accept"
        assert resp.headers.get("ETag")

    def test_conditional_request_returns_304(self, cached_client):
        resp = cached_client.get_response("/ap/actor")
        etag = resp.headers.get("ETag")
        assert etag

        resp2 = cached_client.get_response("/ap/actor", headers={"If-None-Match": etag})
        assert resp2.status_code == 304

    def test_uncached_adapter_behaviour_unchanged(self, adapter_client):
        """Without a document cache the adapter behaves as before."""
        status, doc, _ = adapter_client.get("/ap/actor")
        assert status == 200
        assert doc["type"] == "Person"

    def test_invalidated_document_re_renders(self, cached_client):
        calls = 0
        original = cached_client._handler.get_actor_document

        def spy():
            nonlocal calls
            calls += 1
            return original()

        cached_client._handler.get_actor_document = spy
        cached_client.get("/ap/actor")
        cached_client._handler.document_cache.invalidate(_cache.actor_document_key())
        cached_client.get("/ap/actor")
        assert calls == 2


# ---------------------------------------------------------------------------
# Handler-level invalidation
# ---------------------------------------------------------------------------


def _make_cached_handler(document_cache):
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
        document_cache=document_cache,
    )


class TestHandlerInvalidation:
    def test_publish_actor_update_invalidates_cached_documents(self):
        cache = DocumentCache()
        handler = _make_cached_handler(cache)
        cache.get_or_render(
            _cache.actor_document_key(), handler.get_actor_document, ttl=60
        )
        cache.get_or_render(
            _cache.webfinger_key("acct:blog@blog.example.com"),
            lambda: {"subject": "acct:blog@blog.example.com"},
            ttl=60,
        )
        cache.get_or_render(
            _cache.followers_key(), handler.get_followers_collection, ttl=60
        )

        with patch.object(handler.outbox, "publish", return_value={}):
            handler.publish_actor_update()

        assert cache.store.get(_cache.actor_document_key()) is None
        assert (
            cache.store.get(_cache.webfinger_key("acct:blog@blog.example.com")) is None
        )
        assert cache.store.get(_cache.followers_key()) is None

    def test_publish_actor_update_leaves_application_keys_alone(self):
        """pubby's invalidations stay inside the ``pubby`` namespace — an
        application sharing the cache keeps its own entries (N5)."""
        cache = DocumentCache()
        handler = _make_cached_handler(cache)
        cache.get_or_render(("actor", "alice"), lambda: {"app": True}, ttl=60)
        cache.get_or_render(
            ("webfinger", "alice@example.com"), lambda: {"app": True}, ttl=60
        )

        with patch.object(handler.outbox, "publish", return_value={}):
            handler.publish_actor_update()

        assert cache.store.get(("actor", "alice")) is not None
        assert cache.store.get(("webfinger", "alice@example.com")) is not None

    def test_publish_invalidates_outbox_and_nodeinfo(self):
        cache = DocumentCache()
        handler = _make_cached_handler(cache)
        cache.get_or_render(_cache.outbox_key(20, 0), handler.get_outbox, ttl=60)
        cache.get_or_render(
            _cache.nodeinfo_key(), handler.get_nodeinfo_document, ttl=60
        )

        handler.publish_activity(
            {"type": "Like", "actor": handler.actor_id, "object": "https://x/1"}
        )

        assert cache.store.get(_cache.outbox_key(20, 0)) is None
        assert cache.store.get(_cache.nodeinfo_key()) is None

    def test_inbox_processor_invalidates_followers(self):
        cache = DocumentCache()
        handler = _make_cached_handler(cache)
        cache.get_or_render(
            _cache.followers_key(), handler.get_followers_collection, ttl=60
        )

        handler.inbox._invalidate_cached_documents(_cache.followers_key())
        assert cache.store.get(_cache.followers_key()) is None


# ---------------------------------------------------------------------------
# bind_activitypub-only cache propagation (N2)
# ---------------------------------------------------------------------------


@pytest.fixture(params=["flask", "fastapi", "tornado"])
def bound_client(request):
    """Adapter client whose cache was given ONLY to ``bind_activitypub`` —
    the handler was constructed without one."""
    from tests.conftest import (
        FlaskAdapterClient,
        FastAPIAdapterClient,
        TornadoAdapterClient,
        _make_handler,
    )

    cache = DocumentCache()
    handler = _make_handler()
    client_cls = {
        "flask": FlaskAdapterClient,
        "fastapi": FastAPIAdapterClient,
        "tornado": TornadoAdapterClient,
    }[request.param]
    client = client_cls(handler, document_cache=cache)
    yield client, cache, handler
    if request.param == "tornado":
        client.stop()


class TestAdapterCachePropagation:
    def test_bind_cache_reaches_processors(self, bound_client):
        client, cache, handler = bound_client
        assert handler.document_cache is cache
        assert handler.inbox.document_cache is cache
        assert handler.outbox.document_cache is cache

    def test_inbox_follow_invalidates_cached_followers(self, bound_client):
        """A cache passed only to bind_activitypub must still let an
        incoming Follow invalidate the cached followers collection."""
        client, cache, handler = bound_client
        actor_url = "https://remote.example.com/users/alice"
        handler.storage.cache_remote_actor(
            actor_url,
            {
                "id": actor_url,
                "type": "Person",
                "preferredUsername": "alice",
                "inbox": "https://remote.example.com/inbox",
            },
        )

        status, data, _ = client.get("/ap/followers")
        assert status == 200
        assert data["totalItems"] == 0

        with patch.object(handler.inbox, "_deliver_to_inbox", return_value=True):
            handler.inbox.process(
                {
                    "id": "https://remote.example.com/activities/1",
                    "type": "Follow",
                    "actor": actor_url,
                    "object": handler.actor_id,
                },
                skip_verification=True,
            )

        status, data, _ = client.get("/ap/followers")
        assert status == 200
        assert data["totalItems"] == 1

    def test_outbox_publish_invalidates_cached_outbox(self, bound_client):
        client, cache, handler = bound_client
        calls = 0
        original = handler.get_outbox

        def spy(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        handler.get_outbox = spy
        client.get("/ap/outbox")
        handler.publish_activity(
            {"type": "Like", "actor": handler.actor_id, "object": "https://x/1"}
        )
        client.get("/ap/outbox")
        assert calls == 2


# ---------------------------------------------------------------------------
# rate_limit_key
# ---------------------------------------------------------------------------


class TestRateLimitKey:
    @pytest.fixture(params=["flask", "fastapi", "tornado"])
    def bucketed_client(self, request):
        from tests.conftest import (
            FlaskAdapterClient,
            FastAPIAdapterClient,
            TornadoAdapterClient,
            _make_handler,
        )

        self._buckets = []
        handler = _make_handler()
        client_cls = {
            "flask": FlaskAdapterClient,
            "fastapi": FastAPIAdapterClient,
            "tornado": TornadoAdapterClient,
        }[request.param]

        def key_fn(req):
            bucket = "shared-bucket"
            self._buckets.append(bucket)
            return bucket

        client = client_cls(
            handler,
            rate_limiter=RateLimiter(max_requests=2, window_seconds=60),
            rate_limit_key=key_fn,
        )
        yield client
        if request.param == "tornado":
            client.stop()

    def _post_follow(self, client):
        return client.post(
            "/ap/inbox",
            data=json.dumps(
                {"type": "Follow", "id": "x", "actor": "y", "object": "z"}
            ).encode(),
            headers={"Content-Type": "application/activity+json"},
        )

    def test_rate_limit_key_buckets_requests(self, bucketed_client):
        # Two requests allowed, third limited — all land in the callable's
        # bucket regardless of the client address.
        self._post_follow(bucketed_client)
        self._post_follow(bucketed_client)
        status, _, __ = self._post_follow(bucketed_client)
        assert status == 429
        assert self._buckets == ["shared-bucket"] * 3

    def test_distinct_buckets_are_not_limited(self, request):
        import itertools

        counter = itertools.count()
        for name in ("flask",):
            # One framework is enough to prove the callable decides the bucket.
            from tests.conftest import FlaskAdapterClient, _make_handler

            client = FlaskAdapterClient(
                _make_handler(),
                rate_limiter=RateLimiter(max_requests=2, window_seconds=60),
                rate_limit_key=lambda req: f"bucket-{next(counter)}",
            )
            for _ in range(5):
                status, _, __ = self._post_follow(client)
                assert status != 429


# ---------------------------------------------------------------------------
# Store failure containment
# ---------------------------------------------------------------------------


class _BrokenStore(DocumentStore):
    """A store whose every call fails (e.g. Redis down)."""

    def get(self, key):
        raise ConnectionError("store unreachable")

    def set(self, key, value, ttl, stale_ttl=0.0):
        raise ConnectionError("store unreachable")

    def delete(self, *keys):
        raise ConnectionError("store unreachable")

    def delete_prefix(self, *prefixes):
        raise ConnectionError("store unreachable")

    def delete_segment(self, *segments):
        raise ConnectionError("store unreachable")

    def clear(self):
        raise ConnectionError("store unreachable")


class TestStoreFailures:
    def test_broken_store_degrades_to_uncached_render(self):
        cache = DocumentCache(store=_BrokenStore())
        calls = 0

        def render():
            nonlocal calls
            calls += 1
            return {"v": calls}

        assert cache.get_or_render(("k",), render, ttl=60) == {"v": 1}
        # Nothing was stored — the next call renders again (no 500s).
        assert cache.get_or_render(("k",), render, ttl=60) == {"v": 2}
        assert cache.invalidate(("k",)) == 0
        assert cache.invalidate_prefix(("k",)) == 0
        assert cache.invalidate_segment("k") == 0
        cache.clear()  # must not raise

    def test_broken_store_degrades_in_async_path(self):
        async def main():
            cache = DocumentCache(store=_BrokenStore())
            calls = 0

            async def render():
                nonlocal calls
                calls += 1
                return {"v": calls}

            assert await cache.get_or_render_async(("k",), render, ttl=60) == {"v": 1}
            assert await cache.get_or_render_async(("k",), render, ttl=60) == {"v": 2}

        asyncio.run(main())

    def test_ttl_remaining_zero_on_broken_store(self):
        cache = DocumentCache(store=_BrokenStore())
        assert cache.ttl_remaining(("k",)) == 0.0


# ---------------------------------------------------------------------------
# Sync path edge cases
# ---------------------------------------------------------------------------


class TestSyncEdgeCases:
    def test_freshness_rechecked_under_lock(self):
        """A caller that passed the unlocked freshness check must re-check
        under the lock — an owner may have stored the result meanwhile."""
        cache = DocumentCache()
        fresh_calls = 0
        renders = 0
        original_fresh = cache._fresh

        def _raced_fresh(key):
            nonlocal fresh_calls
            fresh_calls += 1
            if fresh_calls == 2:
                # Simulate the other caller's store landing between the
                # unlocked check and the locked re-check.
                cache.store.set(key, {"v": "stored"}, ttl=60)
            return original_fresh(key)

        cache._fresh = _raced_fresh

        def render():
            nonlocal renders
            renders += 1
            return {"v": "rendered"}

        assert cache.get_or_render(("k",), render, ttl=60) == {"v": "stored"}
        assert renders == 0

    def test_waiter_timeout_serves_stale(self):
        from concurrent.futures import Future

        cache = DocumentCache(wait_timeout=0.05, stale_factor=60.0)
        # An expired-but-staleable entry.
        cache.store.set(("k",), {"v": "old"}, ttl=0.01, stale_ttl=60.0)
        time.sleep(0.02)
        # A render that never completes — the waiter must not hang.
        blocker = Future()
        cache._sync_inflight[("k",)] = blocker
        assert cache.get_or_render(("k",), lambda: {"v": "new"}) == {"v": "old"}

    def test_waiter_timeout_raises_without_stale(self):
        from concurrent.futures import Future
        from concurrent.futures import TimeoutError as FutureTimeoutError

        cache = DocumentCache(wait_timeout=0.05)
        blocker = Future()
        cache._sync_inflight[("k",)] = blocker
        with pytest.raises(FutureTimeoutError):
            cache.get_or_render(("k",), lambda: {"v": "new"})


# ---------------------------------------------------------------------------
# CachedResponse immutability / ttl_remaining
# ---------------------------------------------------------------------------


class TestCachedResponseAndTtl:
    def test_headers_are_immutable(self):
        resp = CachedResponse.document({"a": 1})
        with pytest.raises(TypeError):
            resp.headers["x"] = "y"  # type: ignore[index]

    def test_ttl_remaining_decreases(self):
        cache = DocumentCache()
        cache.get_or_render(("k",), lambda: {"v": 1}, ttl=60)
        remaining = cache.ttl_remaining(("k",))
        assert 0 < remaining <= 60
        assert cache.ttl_remaining(("absent",)) == 0.0


# ---------------------------------------------------------------------------
# Async store-write atomicity against threaded invalidation
# ---------------------------------------------------------------------------


class TestAsyncStoreAtomicity:
    def test_threaded_invalidation_wins_over_store_write(self):
        """An invalidation landing while the async done-callback is writing
        must not be overwritten by the stale result: the generation check
        and the store write are atomic against local invalidation."""
        cache = DocumentCache()
        store = cache.store
        real_set = store.set
        entered = threading.Event()
        release = threading.Event()

        def blocking_set(key, value, ttl, stale_ttl=0.0):
            entered.set()
            release.wait(timeout=5)
            real_set(key, value, ttl, stale_ttl=stale_ttl)

        store.set = blocking_set  # type: ignore[method-assign]

        def run():
            asyncio.run(cache.get_or_render_async(("k",), lambda: {"v": "doc"}, ttl=60))

        t = threading.Thread(target=run)
        t.start()
        assert entered.wait(timeout=5)
        # The done-callback is now inside store.set. Fire an invalidation
        # from another thread — with the write under the cache lock it can
        # only land after the write completes, so it must win.
        inv_started = threading.Event()

        def _invalidate():
            inv_started.set()
            cache.invalidate(("k",))

        inv = threading.Thread(target=_invalidate)
        inv.start()
        assert inv_started.wait(timeout=5)
        release.set()
        t.join(timeout=5)
        inv.join(timeout=5)
        assert not t.is_alive()
        assert not inv.is_alive()
        assert store.get(("k",)) is None


# ---------------------------------------------------------------------------
# WebFinger cache-poisoning (validate before cache lookup)
# ---------------------------------------------------------------------------


class TestWebFingerResourceValidation:
    """Malformed resources must be rejected uncached — never allowed to
    negative-cache (or positive-cache) the valid account's entry."""

    INVALID_RESOURCES = [
        "blog@blog.example.com",  # bare — no acct: scheme
        "@blog@blog.example.com",  # leading @ without acct:
        "acct: blog@blog.example.com",  # inner whitespace
    ]

    @staticmethod
    def _url(resource):
        return "/.well-known/webfinger?resource=" + urllib.parse.quote(resource)

    def test_invalid_forms_rejected_uncached(self, cached_client):
        cache = cached_client._handler.document_cache
        for resource in self.INVALID_RESOURCES:
            status, _, _ = cached_client.get(self._url(resource))
            assert status == 404, resource
        # Nothing was written under the valid account's key.
        valid_key = _cache.webfinger_key("acct:blog@blog.example.com")
        assert cache.store.get(valid_key) is None

    def test_invalid_then_valid_succeeds(self, cached_client):
        """The poisoning sequence: invalid requests first must not suppress
        discovery of the real account."""
        for resource in self.INVALID_RESOURCES:
            status, _, _ = cached_client.get(self._url(resource))
            assert status == 404, resource
        status, data, _ = cached_client.get(self._url("acct:blog@blog.example.com"))
        assert status == 200
        assert data["subject"] == "acct:blog@blog.example.com"

    def test_valid_then_invalid_keeps_valid_entry(self, cached_client):
        status, _, _ = cached_client.get(self._url("acct:blog@blog.example.com"))
        assert status == 200
        for resource in self.INVALID_RESOURCES:
            status, _, _ = cached_client.get(self._url(resource))
            assert status == 404, resource
        # The valid entry survived the malformed requests.
        valid_key = _cache.webfinger_key("acct:blog@blog.example.com")
        assert cached_client._handler.document_cache.store.get(valid_key) is not None

    def test_uncached_adapter_rejects_invalid_forms(self, adapter_client):
        for resource in self.INVALID_RESOURCES:
            status, _, _ = adapter_client.get(self._url(resource))
            assert status == 404, resource

    def test_accepted_spellings_share_one_entry(self, cached_client):
        """``acct:User@Dom`` and ``acct:@user@dom`` are the same resource —
        one render, one cache entry."""
        calls = 0
        handler = cached_client._handler
        original = handler.get_webfinger_response

        def spy(resource=None):
            nonlocal calls
            calls += 1
            return original(resource)

        handler.get_webfinger_response = spy
        status1, doc1, _ = cached_client.get(self._url("acct:blog@blog.example.com"))
        status2, doc2, _ = cached_client.get(self._url("ACCT:@BLOG@blog.example.com"))
        assert status1 == status2 == 200
        assert doc1 == doc2
        assert calls == 1


# ---------------------------------------------------------------------------
# NodeInfo media type
# ---------------------------------------------------------------------------


class TestNodeInfoMediaType:
    def test_document_is_plain_json(self, adapter_client):
        _, __, ct = adapter_client.get("/nodeinfo/2.1")
        assert "application/json" in ct
        assert "activity+json" not in ct

    def test_discovery_is_plain_json(self, adapter_client):
        _, __, ct = adapter_client.get("/.well-known/nodeinfo")
        assert "application/json" in ct
        assert "activity+json" not in ct

    def test_document_is_plain_json_with_cache(self, cached_client):
        _, __, ct = cached_client.get("/nodeinfo/2.1")
        assert "application/json" in ct
        assert "activity+json" not in ct

    def test_discovery_is_plain_json_with_cache(self, cached_client):
        _, __, ct = cached_client.get("/.well-known/nodeinfo")
        assert "application/json" in ct
        assert "activity+json" not in ct


# ---------------------------------------------------------------------------
# Freshness policy on cached adapter responses
# ---------------------------------------------------------------------------


class TestAdapterFreshnessPolicy:
    def test_hit_emits_remaining_freshness(self, cached_client):
        cache = cached_client._handler.document_cache
        key = _cache.actor_document_key()
        cached_client.get("/ap/actor")
        # Age the entry: only ~30 of its 60 seconds of freshness are left.
        entry = cache.store.get(key)
        entry.fresh_until = time.time() + 30
        resp = cached_client.get_response("/ap/actor")
        max_age = int(resp.headers["Cache-Control"].split("max-age=")[1])
        assert 25 <= max_age <= 30
        assert resp.headers["Vary"] == "Accept"
        assert resp.headers["ETag"]

    def test_stale_entry_served_with_zero_freshness_policy(self, cached_client):
        """A stale-if-error hit must not be advertised as fresh: explicit
        zero-freshness policy with the validator preserved."""
        cache = cached_client._handler.document_cache
        key = _cache.actor_document_key()
        doc = cached_client._handler.get_actor_document()
        # An already-expired entry, still inside the stale window.
        cache.store.set(key, CachedResponse.document(doc), ttl=0.001, stale_ttl=60.0)
        time.sleep(0.01)

        def boom():
            raise RuntimeError("render failed")

        cached_client._handler.get_actor_document = boom
        resp = cached_client.get_response("/ap/actor")
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == "max-age=0, must-revalidate"
        assert resp.headers["Vary"] == "Accept"
        assert resp.headers["ETag"]

    def test_disabled_cache_emits_no_store(self, cached_client):
        """A cache whose TTL is disabled must tell downstream caches not to
        store the response either."""
        cache = cached_client._handler.document_cache
        cache.default_ttl = 0
        resp = cached_client.get_response("/ap/actor")
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.headers["Vary"] == "Accept"
        assert resp.headers["ETag"]


# ---------------------------------------------------------------------------
# Redis shared-store guarantee (documented weaker distributed behavior)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis not installed")
class TestRedisCrossInstanceGuarantee:
    def test_completed_deletes_are_shared(self):
        client = fakeredis.FakeRedis()
        cache_a = DocumentCache(store=RedisDocumentStore(client))
        cache_b = DocumentCache(store=RedisDocumentStore(client))
        cache_a.get_or_render(("k",), lambda: {"v": 1}, ttl=60)
        assert cache_b.ttl_remaining(("k",)) > 0
        cache_b.invalidate(("k",))
        assert cache_a.ttl_remaining(("k",)) == 0.0

    def test_in_flight_render_repopulates_across_instances(self):
        """Documented weaker guarantee: generation counters are per cache
        instance, so a render started in A *before* B invalidated the key
        still stores its pre-mutation result. A shared store shares
        completed invalidations, not stale-repopulation protection."""
        client = fakeredis.FakeRedis()
        cache_a = DocumentCache(store=RedisDocumentStore(client))
        cache_b = DocumentCache(store=RedisDocumentStore(client))

        started = threading.Event()
        release = threading.Event()

        def slow_render():
            started.set()
            release.wait(timeout=5)
            return {"v": "pre-mutation"}

        def run_a():
            asyncio.run(cache_a.get_or_render_async(("k",), slow_render, ttl=60))

        t = threading.Thread(target=run_a)
        t.start()
        assert started.wait(timeout=5)
        # B invalidates while A's render is in flight — nothing stored yet.
        assert cache_b.invalidate(("k",)) == 0
        release.set()
        t.join(timeout=5)
        # A's generation never changed, so the pre-mutation result lands.
        assert cache_b.ttl_remaining(("k",)) > 0
        assert cache_b._fresh(("k",)) == {"v": "pre-mutation"}
