"""
Short-TTL document cache with request coalescing (single-flight).

When a post is boosted across the Fediverse, hundreds of remote instances
dereference the same URLs at once — actor documents, WebFinger records,
objects, collections. Rendering each fetch from the database turns a burst
of identical GETs into a burst of identical queries, and under enough
concurrency the database saturates (``sorry, too many clients already``).

This module provides the cache primitive that folds those stampedes:

- :class:`DocumentCache` — the public façade. ``get_or_render`` (sync) and
  ``get_or_render_async`` (asyncio) return a cached document or invoke the
  render once: concurrent callers share a single in-flight render instead
  of each running their own. ``None`` render results are cached as misses
  for a short TTL so repeated fetches of deleted or unknown objects also
  collapse. Exceptions propagate to every waiter and are never cached —
  but when ``stale_factor`` is non-zero a slightly expired entry is served
  instead of the error (stale-if-error).
- :class:`DocumentStore` — the pluggable backend interface.
  :class:`InMemoryDocumentStore` (default) is a bounded LRU per process;
  :class:`RedisDocumentStore` shares entries — and therefore completed
  invalidations — across processes (see its docstring for the limits of
  that guarantee).
- :class:`CachedResponse` — an immutable, serializable response fragment
  (status, serialized body, media type, headers, ETag) that adapters store
  and serve. Storing serialized bytes rather than mutable dicts keeps a
  stray ``doc["x"] = …`` in a route from corrupting the cache for everyone,
  and the precomputed ETag makes ``If-None-Match``/304 handling trivial.
- :func:`cache_headers`/:func:`etag_matches` — the matching HTTP emission
  helpers (``Cache-Control``, ``Vary: Accept``, ``ETag``).

Cache keys are tuples of elements, e.g. ``("actor", "alice")`` or
``("obj", "alice", "abc123", "followers")``. Tuple keys make prefix
invalidation element-exact — ``("obj", "alice", "abc")`` cannot collide
with ``("obj", "alice", "abc", "followers")`` or ``("obj", "alice2")``
the way flat ``a:b:c`` strings can.

Every key the built-in adapter routes and handler invalidations use is
prefixed with :data:`ROUTE_NAMESPACE` (``"pubby"``). Applications that
share a cache instance with pubby keep their own namespaces: the two can
never serve each other's values or invalidate each other's entries.

Invalidation is best-effort: entries also expire at their TTL, which bounds
staleness for mutations that happen in other processes (in-memory store) or
in code that never calls the ``invalidate_*`` methods. Renders started
before an invalidation lands do not re-populate the entry: the cache keeps
a generation counter, bumped by every invalidation, and only renders that
complete against an unchanged generation store their result. Writers that
mutate inside a transaction should therefore invalidate *after* commit —
e.g. from an ``after_commit`` session hook — so a render racing the commit
cannot re-cache the old row.
"""

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import (
    Any,
    Awaitable,
    Callable,
    Hashable,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
)
from urllib.parse import quote, unquote

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Cache key: a tuple of elements. Non-tuple keys are normalized to a
#: 1-element tuple by :func:`normalize_key`.
CacheKey = Tuple[Hashable, ...]

#: Type accepted wherever a key is passed: a tuple, or a bare value that is
#: wrapped into a 1-element tuple.
KeyLike = Union[CacheKey, Hashable]

#: Sentinel returned by lookups that distinguishes "no entry" from a cached
#: miss (``None`` is a legitimate cached value).
ABSENT: Any = object()

#: Default cap on the TTL of cached misses: object ids are assigned at
#: publish time, so a cached "not found" must not outlive the document by
#: long.
DEFAULT_MISS_TTL = 15.0

#: Default maximum number of entries in :class:`InMemoryDocumentStore`.
#: Cache keys are chosen by the requester (any object id, resource string or
#: page number becomes an entry), so the store must be bounded.
DEFAULT_MAX_ENTRIES = 10_000

#: Default stale-if-error factor: an entry is retained for
#: ``ttl * stale_factor`` seconds past expiry and is served when a re-render
#: fails (pool exhaustion, transient errors). ``0`` disables the behavior.
DEFAULT_STALE_FACTOR = 5.0


def normalize_key(key: KeyLike) -> CacheKey:
    """Normalize a cache key to a tuple of elements."""
    if isinstance(key, tuple):
        return key
    return (key,)


# ---------------------------------------------------------------------------
# Stored value shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CachedResponse:
    """
    An immutable, serializable HTTP response fragment stored in the cache.

    Adapters serve ``body`` verbatim and can answer ``If-None-Match`` with
    304 using the precomputed ``etag``. Renders that produce redirects store
    them through :meth:`redirect` instead of a document body.
    """

    status: int
    body: bytes = b""
    media_type: str = "application/json"
    headers: Mapping[str, str] = field(default_factory=dict)
    etag: str = ""

    def __post_init__(self) -> None:
        # Frozen alone does not keep ``headers`` immutable — a caller holding
        # the original dict could still mutate the stored fragment.
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))

    @classmethod
    def document(
        cls,
        doc: Any,
        media_type: str = "application/activity+json",
        *,
        status: int = 200,
        headers: Optional[Mapping[str, str]] = None,
    ) -> "CachedResponse":
        """Wrap a JSON-serializable document (or pre-serialized bytes)."""
        body = (
            bytes(doc)
            if isinstance(doc, (bytes, bytearray))
            else json.dumps(doc).encode("utf-8")
        )
        return cls(
            status=status,
            body=body,
            media_type=media_type,
            headers=dict(headers or {}),
            etag=hashlib.sha1(body).hexdigest(),
        )

    @classmethod
    def redirect(cls, url: str, status: int = 303) -> "CachedResponse":
        """Wrap a redirect target (stored as a ``Location`` header)."""
        return cls(status=status, body=b"", headers={"Location": url})

    def to_jsonable(self) -> dict:
        """Serialize to a JSON-compatible dict (for cross-process stores)."""
        return {
            "__cached_response__": True,
            "status": self.status,
            "media_type": self.media_type,
            "headers": dict(self.headers),
            "etag": self.etag,
            "body_b64": base64.b64encode(self.body).decode("ascii"),
        }

    @classmethod
    def from_jsonable(cls, data: dict) -> "CachedResponse":
        return cls(
            status=data["status"],
            body=base64.b64decode(data.get("body_b64") or ""),
            media_type=data.get("media_type") or "application/json",
            headers=dict(data.get("headers") or {}),
            etag=data.get("etag") or "",
        )


def _to_jsonable(value: Any) -> Any:
    """Convert a cached value to a JSON-compatible structure."""
    if isinstance(value, CachedResponse):
        return value.to_jsonable()
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes__": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def _from_jsonable(value: Any) -> Any:
    """Inverse of :func:`_to_jsonable`."""
    if isinstance(value, dict):
        if value.get("__cached_response__"):
            return CachedResponse.from_jsonable(value)
        if "__bytes__" in value:
            return base64.b64decode(value["__bytes__"])
    return value


# ---------------------------------------------------------------------------
# HTTP emission helpers
# ---------------------------------------------------------------------------


def cache_headers(
    ttl: float,
    *,
    etag: str = "",
    public: bool = True,
    vary: Sequence[str] = ("Accept",),
    stale: bool = False,
) -> dict:
    """
    HTTP headers for a cached dereference response.

    Emits ``Cache-Control`` matching the cache TTL, ``Vary: Accept`` (the
    same URLs often serve JSON to ActivityPub clients and HTML to browsers,
    so shared caches must not mix the variants) and, when ``etag`` is given,
    an ``ETag`` for conditional requests. ``Vary`` and ``ETag`` are
    representation headers — they are emitted even when no freshness is
    advertised so a ``304`` keeps its validator and downstream caches keep
    negotiating variants.

    :param ttl: Freshness lifetime in seconds. ``<= 0`` emits an explicit
        zero-freshness policy instead of dropping the headers: with
        ``stale=True`` (an expired or otherwise not provably fresh
        representation is being served) that is ``max-age=0,
        must-revalidate``; otherwise (caching disabled) ``no-store``.
        Omitting Cache-Control entirely would let intermediaries apply
        heuristic freshness or their own defaults.
    :param etag: Optional ETag value (unquoted; quotes are added here).
    :param public: ``public`` (default) or ``private`` cache scope.
    :param vary: Header names for the ``Vary`` header; empty disables it.
    :param stale: The served representation is not backed by a fresh cache
        entry (stale-if-error, invalidated mid-render, store write
        failure). Only meaningful when ``ttl <= 0``.
    :return: A headers dict.
    """
    headers = {}
    if ttl > 0:
        cache_control = f"{'public' if public else 'private'}, max-age={int(ttl)}"
    elif stale:
        cache_control = "max-age=0, must-revalidate"
    else:
        cache_control = "no-store"
    headers["Cache-Control"] = cache_control
    if vary:
        headers["Vary"] = ", ".join(vary)
    if etag:
        headers["ETag"] = f'"{etag}"'
    return headers


def etag_matches(if_none_match: Optional[str], etag: str) -> bool:
    """
    Check an ``If-None-Match`` header against an ETag (weak comparison).

    :param if_none_match: Raw ``If-None-Match`` header value.
    :param etag: The stored ETag (unquoted hex string).
    :return: ``True`` if a 304 Not Modified can be served.
    """
    if not if_none_match or not etag:
        return False
    quoted = f'"{etag}"'
    return any(
        candidate in ("*", quoted, f"W/{quoted}")
        for candidate in (c.strip() for c in if_none_match.split(","))
    )


# ---------------------------------------------------------------------------
# Storage backends
# ---------------------------------------------------------------------------


@dataclass
class CacheEntry:
    """
    A stored document with freshness bookkeeping.

    Deadlines are wall-clock (``time.time()``) so entries can be shared
    across processes through a common store (e.g. Redis).
    """

    value: Any
    fresh_until: float  # time.time() deadline for fresh hits
    drop_after: float  # time.time() deadline for hard eviction

    @property
    def fresh(self) -> bool:
        return self.fresh_until > time.time()

    @property
    def usable_stale(self) -> bool:
        """Expired but still inside the stale-if-error grace window."""
        now = time.time()
        return self.fresh_until <= now < self.drop_after


class DocumentStore:
    """
    Storage backend interface for :class:`DocumentCache`.

    Implementations must be safe to call from any thread. ``get`` returns
    the :class:`CacheEntry` — which may be expired but still retained for
    stale-if-error serving — or ``None`` when nothing is stored. All
    ``delete_*`` methods return the number of entries dropped.
    """

    def get(self, key: CacheKey) -> Optional[CacheEntry]:
        raise NotImplementedError

    def set(
        self, key: CacheKey, value: Any, ttl: float, stale_ttl: float = 0.0
    ) -> None:
        """
        Store ``value`` under ``key``.

        :param ttl: Seconds the entry is served as fresh.
        :param stale_ttl: Extra seconds the entry is retained past expiry
            for stale-if-error serving.
        """
        raise NotImplementedError

    def delete(self, *keys: CacheKey) -> int:
        raise NotImplementedError

    def delete_prefix(self, *prefixes: CacheKey) -> int:
        """Drop keys whose elements start with any of ``prefixes``."""
        raise NotImplementedError

    def delete_segment(self, *segments: Hashable) -> int:
        """Drop keys containing any of ``segments`` as an element."""
        raise NotImplementedError

    def clear(self) -> None:
        raise NotImplementedError


class InMemoryDocumentStore(DocumentStore):
    """
    Bounded in-process LRU + TTL store.

    Entries are evicted least-recently-used first once ``max_entries`` is
    exceeded; entries past their ``drop_after`` deadline are evicted before
    live ones and also dropped lazily on access. :meth:`sweep` drops every
    expired entry and can be called periodically by the application.
    """

    #: How many over-capacity inserts pass with a plain LRU pop before the
    #: store re-scans for expired entries. The expired-first sweep is O(n)
    #: under the lock, so running it on every insert lets an attacker who
    #: fills the store make each write quadratic.
    _EVICT_SWEEP_INTERVAL = 32

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES):
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._entries: OrderedDict = OrderedDict()
        self._lock = threading.RLock()
        self._unswept_inserts = 0

    def get(self, key: CacheKey) -> Optional[CacheEntry]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.drop_after <= time.time():
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return entry

    def set(
        self, key: CacheKey, value: Any, ttl: float, stale_ttl: float = 0.0
    ) -> None:
        now = time.time()
        entry = CacheEntry(
            value=value,
            fresh_until=now + ttl,
            drop_after=now + ttl + stale_ttl,
        )
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            if len(self._entries) > self.max_entries:
                self._unswept_inserts += 1
                if self._unswept_inserts >= self._EVICT_SWEEP_INTERVAL:
                    self._unswept_inserts = 0
                    self._evict(now)
                else:
                    self._entries.popitem(last=False)

    def _evict(self, now: float) -> None:
        """Drop expired entries first, then the least-recently-used."""
        for key in [k for k, e in self._entries.items() if e.drop_after <= now]:
            del self._entries[key]
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def sweep(self) -> int:
        """Drop every entry past its ``drop_after`` deadline."""
        now = time.time()
        with self._lock:
            doomed = [k for k, e in self._entries.items() if e.drop_after <= now]
            for key in doomed:
                del self._entries[key]
        return len(doomed)

    def delete(self, *keys: CacheKey) -> int:
        dropped = 0
        with self._lock:
            for key in keys:
                if key in self._entries:
                    del self._entries[key]
                    dropped += 1
        return dropped

    def delete_prefix(self, *prefixes: CacheKey) -> int:
        normalized = [tuple(p) for p in prefixes]
        with self._lock:
            doomed = [
                k for k in self._entries if any(k[: len(p)] == p for p in normalized)
            ]
            for key in doomed:
                del self._entries[key]
        return len(doomed)

    def delete_segment(self, *segments: Hashable) -> int:
        wanted = set(segments)
        with self._lock:
            doomed = [k for k in self._entries if wanted & set(k)]
            for key in doomed:
                del self._entries[key]
        return len(doomed)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class RedisDocumentStore(DocumentStore):
    """
    Redis-backed store — shares cached documents, and therefore
    invalidations, across processes.

    ``client`` is any synchronous redis-py-compatible client
    (``redis.Redis``, ``fakeredis.FakeRedis``, …); no Redis dependency is
    required to import this class. Single-flight coalescing stays
    per-process — two processes may still render the same key concurrently,
    but the stored result is shared.

    **Distributed guarantee — read before relying on this in production.**
    Sharing the store shares *completed* invalidations: a delete issued by
    process B is seen by process A's next lookup. It does **not** extend
    the stale-repopulation protection to renders that were already in
    flight: generation counters live on each :class:`DocumentCache`
    instance, so a render started in A *before* B invalidated the key can
    still store its pre-mutation result after B's delete, restarting a full
    TTL on stale data. Applications needing a hard guarantee across
    processes must either invalidate again after the render window (e.g.
    re-issue the invalidation a few seconds later) or implement a shared
    compare-and-set protocol — this store deliberately does not pretend to
    solve distributed mutation races.

    Values are JSON-serialized together with their deadlines; ``bytes`` and
    :class:`CachedResponse` values are supported natively, custom values
    need ``dumps``/``loads`` callables. Entries carry a Redis TTL matching
    their ``drop_after`` deadline, so size is bounded by the server's
    eviction policy rather than ``max_entries``.

    Two costs to be aware of:

    - The client is synchronous: every call performs a Redis round-trip,
      including inside :meth:`DocumentCache.get_or_render_async` (which
      would block the event loop) and inside that method's store
      done-callback. Pair it with a small ``socket_timeout`` on the client,
      or use it where blocking is acceptable (the sync adapter paths).
    - ``delete_prefix``/``delete_segment``/``clear`` SCAN the keyspace
      under ``prefix`` — proportional to the number of cached keys, not to
      the match count. Prefer targeted ``delete``/``delete_prefix`` calls.
    """

    DEFAULT_PREFIX = "pubby:doc:"

    def __init__(
        self,
        client: Any,
        *,
        prefix: str = DEFAULT_PREFIX,
        dumps: Optional[Callable[[Any], str]] = None,
        loads: Optional[Callable[[str], Any]] = None,
    ):
        self.client = client
        self.prefix = prefix
        self._dumps = dumps or (lambda v: json.dumps(v, separators=(",", ":")))
        self._loads = loads or json.loads

    # -- key encoding -------------------------------------------------------

    @staticmethod
    def _encode_element(element: Hashable) -> str:
        return quote(str(element), safe="")

    def _encode_key(self, key: CacheKey) -> str:
        return self.prefix + "/".join(self._encode_element(e) for e in key)

    def _decode_key(self, encoded: str) -> CacheKey:
        body = encoded[len(self.prefix) :]
        return tuple(unquote(part) for part in body.split("/"))

    def _match_patterns(self, prefixes: Sequence[CacheKey]) -> list:
        patterns = []
        for prefix in prefixes:
            encoded = self.prefix + "/".join(self._encode_element(e) for e in prefix)
            patterns.append(encoded)
            patterns.append(encoded + "/*")
        return patterns

    def _scan(self, pattern: str) -> list:
        client = self.client
        if hasattr(client, "scan_iter"):
            return list(client.scan_iter(match=pattern))
        return [k for k in client.keys(pattern)]

    # -- DocumentStore API ---------------------------------------------------

    def get(self, key: CacheKey) -> Optional[CacheEntry]:
        raw = self.client.get(self._encode_key(key))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            envelope = self._loads(raw)
        except Exception:
            logger.warning("Unserializable cache entry at %s; dropping", key)
            self.client.delete(self._encode_key(key))
            return None
        return CacheEntry(
            value=_from_jsonable(envelope["v"]),
            fresh_until=envelope["f"],
            drop_after=envelope["d"],
        )

    def set(
        self, key: CacheKey, value: Any, ttl: float, stale_ttl: float = 0.0
    ) -> None:
        now = time.time()
        envelope = {
            "v": _to_jsonable(value),
            "f": now + ttl,
            "d": now + ttl + stale_ttl,
        }
        self.client.set(
            self._encode_key(key),
            self._dumps(envelope),
            pxat=int(envelope["d"] * 1000),
        )

    def delete(self, *keys: CacheKey) -> int:
        if not keys:
            return 0
        return int(self.client.delete(*[self._encode_key(k) for k in keys]))

    def _delete_matching(self, patterns: Sequence[str]) -> int:
        doomed = {k for pattern in patterns for k in self._scan(pattern)}
        if not doomed:
            return 0
        return int(self.client.delete(*doomed))

    def delete_prefix(self, *prefixes: CacheKey) -> int:
        return self._delete_matching(self._match_patterns(prefixes))

    def delete_segment(self, *segments: Hashable) -> int:
        wanted = {str(s) for s in segments}
        doomed = [
            k
            for k in self._scan(self.prefix + "*")
            if wanted
            & set(self._decode_key(k if isinstance(k, str) else k.decode("utf-8")))
        ]
        if not doomed:
            return 0
        return int(self.client.delete(*doomed))

    def clear(self) -> None:
        self._delete_matching([self.prefix + "*"])


# ---------------------------------------------------------------------------
# DocumentCache — TTL + single-flight façade
# ---------------------------------------------------------------------------


class DocumentCache:
    """
    TTL document cache with single-flight request coalescing.

    Concurrent fetches of the same key share one in-flight render: the first
    caller runs it, waiters attach to its result. ``None`` results are
    cached as misses for ``miss_ttl`` at most; exceptions propagate to every
    waiter and are never cached. When a render fails and a slightly expired
    entry exists (``stale_factor``), the stale value is served instead.

    Every invalidation bumps a generation counter; a render that completes
    against a stale generation does not store its result, so an invalidation
    landing mid-render cannot be re-populated by old data.

    :param store: A :class:`DocumentStore` backend. Defaults to
        :class:`InMemoryDocumentStore` bounded to ``max_entries``.
    :param max_entries: Bound for the default in-memory store.
    :param default_ttl: TTL used when a call passes ``ttl=None``.
    :param miss_ttl: Cap on the TTL of cached ``None`` results.
    :param stale_factor: Multiplier on the TTL defining how long an expired
        entry is retained for stale-if-error serving (``0`` disables).
    :param wait_timeout: Seconds a *waiter* thread blocks on a shared
        in-flight render before giving up (``None`` waits forever). Bounds
        how long a hung render can pin the threads folded into it — Flask
        workers or the FastAPI/AnyIO threadpool that runs the sync adapter
        routes. On timeout a waiter falls back to stale data, else raises
        :class:`TimeoutError`. The render itself keeps running.
    """

    def __init__(
        self,
        store: Optional[DocumentStore] = None,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        default_ttl: float = 60.0,
        miss_ttl: float = DEFAULT_MISS_TTL,
        stale_factor: float = DEFAULT_STALE_FACTOR,
        wait_timeout: Optional[float] = 30.0,
    ):
        self.store = (
            store
            if store is not None
            else InMemoryDocumentStore(max_entries=max_entries)
        )
        self.default_ttl = default_ttl
        self.miss_ttl = miss_ttl
        self.stale_factor = stale_factor
        self.wait_timeout = wait_timeout
        self._generation = 0
        self._lock = threading.Lock()
        self._sync_inflight: dict = {}
        self._async_inflight: dict = {}

    # -- entry lookup --------------------------------------------------------

    def _store_get(self, key: CacheKey) -> Optional[CacheEntry]:
        """Read an entry, degrading store failures to misses."""
        try:
            return self.store.get(key)
        except Exception:
            logger.warning("Document store get failed for %r", key, exc_info=True)
            return None

    def _store_set(
        self, key: CacheKey, value: Any, ttl: float, stale_ttl: float
    ) -> None:
        """Write an entry, skipping it when the store fails."""
        try:
            self.store.set(key, value, ttl, stale_ttl=stale_ttl)
        except Exception:
            logger.warning("Document store set failed for %r", key, exc_info=True)

    def _fresh(self, key: CacheKey) -> Any:
        entry = self._store_get(key)
        if entry is not None and entry.fresh:
            return entry.value
        return ABSENT

    def _stale(self, key: CacheKey) -> Any:
        if self.stale_factor <= 0:
            return ABSENT
        entry = self._store_get(key)
        if entry is not None and entry.usable_stale:
            return entry.value
        return ABSENT

    def ttl_remaining(self, key: KeyLike) -> float:
        """
        Seconds of freshness left on the stored entry, ``0`` when absent or
        expired.

        Adapters emit ``max-age=<remaining>`` (via :func:`cache_headers`)
        rather than the full TTL: an entry served 50 s into its 60 s
        lifetime would otherwise get another full ``max-age=60`` at the
        edge cache and at every downstream client, so the layers would add
        their staleness instead of sharing it.
        """
        entry = self._store_get(normalize_key(key))
        if entry is None:
            return 0.0
        return max(0.0, entry.fresh_until - time.time())

    def _effective_ttls(self, ttl: Optional[float], miss_ttl: Optional[float]) -> tuple:
        return (
            self.default_ttl if ttl is None else ttl,
            self.miss_ttl if miss_ttl is None else miss_ttl,
        )

    def _effective_miss_ttl(self, ttl: float, miss_ttl: float) -> float:
        return min(ttl, miss_ttl)

    def _store_result(
        self, key: CacheKey, result: Any, ttl: float, miss_ttl: float
    ) -> None:
        effective_ttl = (
            ttl if result is not None else self._effective_miss_ttl(ttl, miss_ttl)
        )
        if effective_ttl > 0:
            self._store_set(
                key,
                result,
                effective_ttl,
                stale_ttl=effective_ttl * self.stale_factor,
            )

    # -- synchronous single-flight -------------------------------------------

    def get_or_render(
        self,
        key: KeyLike,
        render: Callable[[], T],
        *,
        ttl: Optional[float] = None,
        miss_ttl: Optional[float] = None,
    ) -> T:
        """
        Return the cached value for ``key`` or call ``render()`` once.

        Concurrent callers (threads) share a single in-flight render;
        waiters block on its result. ``render`` must be synchronous — use
        :meth:`get_or_render_async` for coroutine renders.
        """
        key = normalize_key(key)
        ttl, miss_ttl = self._effective_ttls(ttl, miss_ttl)

        if ttl > 0:
            fresh = self._fresh(key)
            if fresh is not ABSENT:
                return fresh

        with self._lock:
            future = self._sync_inflight.get(key)
            if future is None:
                # Re-check under the lock: an owner may have stored its
                # result between the unlocked check above and now — without
                # this, that window renders the key a second time.
                if ttl > 0:
                    fresh = self._fresh(key)
                    if fresh is not ABSENT:
                        return fresh
                future = Future()
                self._sync_inflight[key] = future
                generation = self._generation
                owner = True
            else:
                owner = False

        if not owner:
            try:
                return future.result(timeout=self.wait_timeout)
            except Exception:
                stale = self._stale(key)
                if stale is not ABSENT:
                    return stale
                raise

        try:
            result = render()
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError(
                    "render returned an awaitable — use get_or_render_async "
                    "for coroutine renders"
                )
        except Exception as exc:
            stale = self._stale(key)
            if stale is not ABSENT:
                future.set_result(stale)
                self._pop_sync_inflight(key)
                return stale
            future.set_exception(exc)
            self._pop_sync_inflight(key)
            raise
        except BaseException as exc:
            # KeyboardInterrupt/SystemExit must propagate in the owner —
            # stale data must not swallow an interrupt — but waiters must
            # not hang on a future that never completes.
            future.set_exception(exc)
            self._pop_sync_inflight(key)
            raise

        future.set_result(result)
        with self._lock:
            self._sync_inflight.pop(key, None)
            # A generation bump means an invalidation landed mid-render:
            # the result may be stale and must not be cached.
            if ttl > 0 and self._generation == generation:
                self._store_result(key, result, ttl, miss_ttl)
        return result

    def _pop_sync_inflight(self, key: CacheKey) -> None:
        with self._lock:
            self._sync_inflight.pop(key, None)

    # -- asyncio single-flight -------------------------------------------------

    def _async_inflight_for(self, loop: asyncio.AbstractEventLoop) -> dict:
        inflight = self._async_inflight.get(loop)
        if inflight is None:
            with self._lock:
                for dead in [lp for lp in self._async_inflight if lp.is_closed()]:
                    del self._async_inflight[dead]
                inflight = self._async_inflight.setdefault(loop, {})
        return inflight

    async def _await_render(self, render: Callable[[], Union[T, Awaitable[T]]]) -> T:
        result = render()
        if inspect.isawaitable(result):
            return await result
        return result

    async def get_or_render_async(
        self,
        key: KeyLike,
        render: Callable[[], Union[T, Awaitable[T]]],
        *,
        ttl: Optional[float] = None,
        miss_ttl: Optional[float] = None,
    ) -> T:
        """
        Async variant of :meth:`get_or_render`.

        In-flight renders are tracked per event loop (asyncio tasks are
        loop-bound); the store is shared across loops. ``asyncio.shield``
        keeps a cancelled waiter — e.g. a client that disconnected — from
        cancelling the shared render for everyone else, which is also why
        renders should open their own resources rather than borrow
        request-scoped ones.
        """
        key = normalize_key(key)
        ttl, miss_ttl = self._effective_ttls(ttl, miss_ttl)

        if ttl > 0:
            fresh = self._fresh(key)
            if fresh is not ABSENT:
                return fresh

        inflight = self._async_inflight_for(asyncio.get_running_loop())
        task = inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._await_render(render))
            inflight[key] = task
            generation = self._generation

            def _store(
                fut: asyncio.Future,
                *,
                _key: CacheKey = key,
                _inflight: dict = inflight,
                _generation: int = generation,
                _ttl: float = ttl,
                _miss_ttl: float = miss_ttl,
            ) -> None:
                _inflight.pop(_key, None)
                if fut.cancelled() or fut.exception() is not None:
                    return
                # The generation check and the store write must be atomic
                # against invalidation: this callback runs on the event loop
                # while invalidations may land from any thread. Holding the
                # lock across both mirrors the sync path — without it a
                # threaded invalidation could slip in between the check and
                # the write and the stale result would repopulate the entry.
                # (The store write itself may block on a synchronous backend
                # such as Redis — a documented cost of the sync client.)
                with self._lock:
                    if self._generation != _generation:
                        # An invalidation landed mid-render: the result may
                        # be stale.
                        return
                    self._store_result(_key, fut.result(), _ttl, _miss_ttl)

            task.add_done_callback(_store)

        try:
            return await asyncio.shield(task)
        except Exception:
            # A cancelled waiter (CancelledError, a BaseException) is not
            # served stale data — its cancellation must propagate.
            stale = self._stale(key)
            if stale is not ABSENT:
                return stale
            raise

    # -- invalidation -----------------------------------------------------------

    def bump_generation(self) -> None:
        """
        Discard the results of all in-flight renders.

        Writers mutating state inside a transaction can call this *before*
        commit so renders already under way do not re-populate entries with
        pre-mutation data; the entry deletions themselves should then run
        after commit (e.g. from an ``after_commit`` session hook).
        """
        with self._lock:
            self._generation += 1

    def _delete(self, method: str, *args) -> int:
        """Run a store deletion; a store failure reports 0 drops."""
        try:
            return getattr(self.store, method)(*args)
        except Exception:
            logger.warning("Document store %s failed", method, exc_info=True)
            return 0

    def invalidate(self, *keys: KeyLike) -> int:
        """Drop the given keys exactly."""
        with self._lock:
            self._generation += 1
        return self._delete("delete", *[normalize_key(k) for k in keys])

    def invalidate_prefix(self, *prefixes: KeyLike) -> int:
        """Drop keys whose leading elements equal any of ``prefixes``."""
        with self._lock:
            self._generation += 1
        return self._delete("delete_prefix", *[normalize_key(p) for p in prefixes])

    def invalidate_segment(self, *segments: Optional[Hashable]) -> int:
        """Drop keys containing any of ``segments`` as an element.

        For callers that know a document's id but not which key namespaces
        embed it — e.g. an object id living under both
        ``("obj", user, id)`` and ``("obj", user, id, "followers")``.
        Element-wise matching avoids the false substring hits flat string
        keys produce (``abc`` inside ``abc2``).
        """
        wanted = {s for s in segments if s is not None}
        if not wanted:
            return 0
        with self._lock:
            self._generation += 1
        return self._delete("delete_segment", *wanted)

    def clear(self) -> None:
        """Drop every cached entry and in-flight bookkeeping."""
        with self._lock:
            self._generation += 1
            self._sync_inflight.clear()
            self._async_inflight.clear()
        try:
            self.store.clear()
        except Exception:
            logger.warning("Document store clear failed", exc_info=True)


# ---------------------------------------------------------------------------
# Key builders for the built-in adapter routes
# ---------------------------------------------------------------------------

#: First element of every key the built-in adapter routes and the handler's
#: invalidations use. Applications sharing a :class:`DocumentCache` with
#: pubby (e.g. for their own dereference routes) keep their own namespaces,
#: so pubby's :class:`CachedResponse` entries can never collide with — or be
#: served/invalidated as — an application's own documents.
ROUTE_NAMESPACE = "pubby"


def route_key(*elements: Hashable) -> CacheKey:
    """Build a key (or key prefix) under pubby's adapter-route namespace."""
    return (ROUTE_NAMESPACE,) + tuple(elements)


def actor_document_key() -> CacheKey:
    return route_key("actor")


def normalize_webfinger_resource(resource: str) -> str:
    """
    Normalize a *valid* WebFinger ``resource`` parameter to ``user@domain``.

    ``acct:User@example.com`` and ``acct:@user@example.com`` spell the same
    resource — the lowercased ``user@domain`` form lets every accepted
    spelling share one cache entry.

    Only spellings :meth:`pubby.handlers.ActivityPubHandler.is_valid_webfinger_resource`
    accepts may be normalized here — the adapters validate the resource
    *before* cache lookup, so a malformed form (bare ``user@domain``, a
    leading ``@`` without ``acct:``, surrounding whitespace) is rejected
    uncached instead of poisoning the valid account's entry.
    """
    normalized = resource
    if normalized.lower().startswith("acct:"):
        normalized = normalized[5:]
    return normalized.lstrip("@").lower()


def webfinger_key(resource: str) -> CacheKey:
    """Cache key for a WebFinger ``resource`` parameter."""
    return route_key("webfinger", normalize_webfinger_resource(resource))


def nodeinfo_key() -> CacheKey:
    return route_key("nodeinfo")


def nodeinfo_discovery_key() -> CacheKey:
    return route_key("nodeinfo-discovery")


def outbox_key(limit: int, offset: int) -> CacheKey:
    return route_key("outbox", int(limit), int(offset))


def followers_key() -> CacheKey:
    return route_key("followers")


def following_key() -> CacheKey:
    return route_key("following")


def quote_authorization_key(auth_id: str) -> CacheKey:
    return route_key("quote-authorization", auth_id)
