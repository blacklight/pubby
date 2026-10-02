"""
Tornado server adapter for ActivityPub.

Registers all required routes on a Tornado application.
"""

import json
from typing import Callable, Optional, List, Tuple, Any

import tornado.web

from ... import cache as _cache
from ...handlers import ActivityPubHandler
from ..._exceptions import (
    ActivityPubError,
    RateLimitError,
    SignatureVerificationError,
)
from ..._rate_limit import RateLimiter

# Content types
ACTIVITY_JSON = "application/activity+json"
JRD_JSON = "application/jrd+json"


class BaseActivityPubHandler(tornado.web.RequestHandler):
    """Base handler with shared functionality."""

    def initialize(
        self,
        handler: ActivityPubHandler,
        rate_limiter: Optional[RateLimiter] = None,
        document_cache: Optional[_cache.DocumentCache] = None,
        rate_limit_key: Optional[Callable] = None,
    ):
        self.ap_handler = handler
        self.rate_limiter = rate_limiter
        self.document_cache = (
            document_cache if document_cache is not None else handler.document_cache
        )
        self.rate_limit_key = rate_limit_key

    def write_json(self, data, content_type="application/json"):
        """Write JSON with explicit content type (avoids Tornado auto-setting)."""
        self.set_header("Content-Type", content_type)
        self.write(json.dumps(data))

    def serve_document(self, key, render, media_type=ACTIVITY_JSON):
        """Serve a dereference document through the shared document cache."""
        cache = self.document_cache
        if cache is None:
            doc = render()
            if doc is None:
                self.set_status(404)
                self.write_json({"error": "not found"})
                return
            self.write_json(doc, media_type)
            return

        def _render():
            doc = render()
            if doc is None:
                return None
            return _cache.CachedResponse.document(doc, media_type)

        cached = cache.get_or_render(key, _render)
        if cached is None:
            self.set_status(404)
            self.write_json({"error": "not found"})
            return

        # Emit the entry's remaining freshness rather than the full TTL, so
        # edge caches and clients don't add their own max-age on top of ours.
        # With no freshness left (stale-if-error, invalidated mid-render, a
        # failed store write) the response must not be treated as fresh:
        # ``stale`` asks for an explicit zero-freshness policy instead of
        # dropping Cache-Control — unless caching itself is disabled, which
        # maps to ``no-store``.
        remaining = cache.ttl_remaining(key)
        headers = _cache.cache_headers(
            remaining,
            etag=cached.etag,
            stale=cache.default_ttl > 0,
        )
        for name, value in headers.items():
            self.set_header(name, value)
        for name, value in cached.headers.items():
            self.set_header(name, value)
        if cached.etag and _cache.etag_matches(
            self.request.headers.get("If-None-Match"), cached.etag
        ):
            self.set_status(304)
            return
        self.set_status(cached.status)
        self.set_header("Content-Type", cached.media_type)
        self.write(cached.body)

    def rate_limit_bucket(self) -> str:
        if self.rate_limit_key is not None:
            return self.rate_limit_key(self.request)
        return self.request.remote_ip or "unknown"


class WebFingerHandler(BaseActivityPubHandler):
    """Handle WebFinger requests."""

    def get(self):
        resource = self.get_argument("resource", None)
        if not resource:
            self.set_status(400)
            self.write_json({"error": "resource parameter is required"})
            return
        # Validate before cache lookup: a malformed resource must be
        # rejected uncached, or its 404 would poison the valid account's
        # cache entry (they normalize to the same key).
        if not self.ap_handler.is_valid_webfinger_resource(resource):
            self.set_status(404)
            self.write_json({"error": "not found"})
            return

        self.serve_document(
            _cache.webfinger_key(resource),
            lambda: self.ap_handler.get_webfinger_response(resource),
            JRD_JSON,
        )


class NodeInfoDiscoveryHandler(BaseActivityPubHandler):
    """Handle NodeInfo discovery requests."""

    def get(self):
        self.serve_document(
            _cache.nodeinfo_discovery_key(),
            self.ap_handler.get_nodeinfo_discovery,
            "application/json",
        )


class NodeInfoHandler(BaseActivityPubHandler):
    """Handle NodeInfo document requests."""

    def get(self):
        self.serve_document(
            _cache.nodeinfo_key(),
            self.ap_handler.get_nodeinfo_document,
            "application/json",
        )


class ActorHandler(BaseActivityPubHandler):
    """Handle actor profile requests."""

    def get(self):
        self.serve_document(
            _cache.actor_document_key(), self.ap_handler.get_actor_document
        )


class InboxHandler(BaseActivityPubHandler):
    """Handle inbox POST requests."""

    def post(self):
        # Rate limiting
        if self.rate_limiter is not None:
            try:
                self.rate_limiter.check(self.rate_limit_bucket())
            except RateLimitError:
                self.set_status(429)
                self.write_json({"error": "rate limit exceeded"})
                return

        body = self.request.body
        try:
            activity_data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self.set_status(400)
            self.write_json({"error": "invalid JSON"})
            return

        # Collect headers
        headers = dict(self.request.headers)

        try:
            self.ap_handler.process_inbox_activity(
                activity_data,
                method="POST",
                path=self.request.path,
                headers=headers,
                body=body,
            )
        except SignatureVerificationError as e:
            self.set_status(401)
            self.write_json({"error": str(e)})
            return
        except ActivityPubError as e:
            self.set_status(400)
            self.write_json({"error": str(e)})
            return

        self.set_status(202)
        self.write_json({"status": "ok"})


class OutboxHandler(BaseActivityPubHandler):
    """Handle outbox GET requests."""

    def get(self):
        limit = int(self.get_argument("limit", 20))
        offset = int(self.get_argument("offset", 0))
        self.serve_document(
            _cache.outbox_key(limit, offset),
            lambda: self.ap_handler.get_outbox(limit=limit, offset=offset),
        )


class FollowersHandler(BaseActivityPubHandler):
    """Handle followers collection requests."""

    def get(self):
        self.serve_document(
            _cache.followers_key(), self.ap_handler.get_followers_collection
        )


class FollowingHandler(BaseActivityPubHandler):
    """Handle following collection requests."""

    def get(self):
        self.serve_document(
            _cache.following_key(), self.ap_handler.get_following_collection
        )


class QuoteAuthorizationHandler(BaseActivityPubHandler):
    """Handle GET requests for QuoteAuthorization objects."""

    def get(self, auth_id):
        full_id = f"{self.ap_handler.actor_id}/quote_authorizations/{auth_id}"
        self.serve_document(
            _cache.quote_authorization_key(full_id),
            lambda: self.ap_handler.get_quote_authorization(full_id),
        )


def bind_activitypub(
    app: tornado.web.Application,
    handler: ActivityPubHandler,
    prefix: str = "/ap",
    rate_limiter: Optional[RateLimiter] = None,
    document_cache: Optional[_cache.DocumentCache] = None,
    rate_limit_key: Optional[Callable] = None,
) -> List[Tuple[str, Any, dict]]:
    """
    Bind ActivityPub routes to a Tornado application.

    Registers the following endpoints:

    - ``GET /.well-known/webfinger`` — WebFinger discovery
    - ``GET /.well-known/nodeinfo`` — NodeInfo discovery
    - ``GET /nodeinfo/2.1`` — NodeInfo document
    - ``GET <prefix>/actor`` — Actor profile
    - ``POST <prefix>/inbox`` — Inbox (receive activities)
    - ``GET <prefix>/outbox`` — Outbox collection
    - ``GET <prefix>/followers`` — Followers collection
    - ``GET <prefix>/following`` — Following collection

    :param app: The Tornado application.
    :param handler: The ActivityPubHandler instance.
    :param prefix: URL prefix for AP routes (default ``/ap``).
    :param rate_limiter: Optional rate limiter for the inbox endpoint.
    :param document_cache: Optional :class:`pubby.cache.DocumentCache`
        shared by the dereference GET endpoints: concurrent fetches of the
        same document share one render (single-flight), hits cost nothing,
        and responses carry ``Cache-Control``/``Vary``/``ETag`` headers with
        ``If-None-Match`` support. Falls back to ``handler.document_cache``;
        when a cache is provided here and the handler has none, it is also
        set on the handler so mutations invalidate the right entries.
    :param rate_limit_key: Optional callable mapping the incoming request to
        the rate-limit bucket key (default: ``request.remote_ip``). Pass a
        trusted-proxy-aware resolver (or the signing ``keyId`` domain) when
        the app sits behind a reverse proxy — otherwise all remote servers
        share the proxy's bucket.
    :return: List of URL spec tuples (also adds them to the app).
    """
    prefix = prefix.rstrip("/")

    if document_cache is not None and handler.document_cache is None:
        handler.document_cache = document_cache

    # Handler initialization kwargs
    init_kwargs = {
        "handler": handler,
        "rate_limiter": rate_limiter,
        "document_cache": document_cache,
        "rate_limit_key": rate_limit_key,
    }

    # Define URL patterns
    url_patterns = [
        # Well-known routes
        (r"/.well-known/webfinger", WebFingerHandler, init_kwargs),
        (r"/.well-known/nodeinfo", NodeInfoDiscoveryHandler, init_kwargs),
        # NodeInfo document
        (r"/nodeinfo/2.1", NodeInfoHandler, init_kwargs),
        # ActivityPub routes with prefix
        (rf"{prefix}/actor", ActorHandler, init_kwargs),
        (rf"{prefix}/inbox", InboxHandler, init_kwargs),
        (rf"{prefix}/outbox", OutboxHandler, init_kwargs),
        (rf"{prefix}/followers", FollowersHandler, init_kwargs),
        (rf"{prefix}/following", FollowingHandler, init_kwargs),
        (
            rf"{handler.actor_path}/quote_authorizations/(.*)",
            QuoteAuthorizationHandler,
            init_kwargs,
        ),
    ]

    # Add handlers to the application
    app.add_handlers(".*", url_patterns)

    return url_patterns
