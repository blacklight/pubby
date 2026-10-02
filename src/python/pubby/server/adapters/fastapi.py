"""
FastAPI server adapter for ActivityPub.

Registers all required routes on a FastAPI application.
"""

import json
from typing import Callable, Optional

from fastapi import FastAPI, APIRouter, Request, Depends
from fastapi.responses import JSONResponse, Response

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


async def get_raw_body(request: Request) -> bytes:
    """Dependency to get raw request body."""
    return await request.body()


def bind_activitypub(
    app: FastAPI,
    handler: ActivityPubHandler,
    prefix: str = "/ap",
    rate_limiter: Optional[RateLimiter] = None,
    document_cache: Optional[_cache.DocumentCache] = None,
    rate_limit_key: Optional[Callable[[Request], str]] = None,
):
    """
    Bind ActivityPub routes to a FastAPI application.

    Registers the following endpoints:

    - ``GET /.well-known/webfinger`` — WebFinger discovery
    - ``GET /.well-known/nodeinfo`` — NodeInfo discovery
    - ``GET /nodeinfo/2.1`` — NodeInfo document
    - ``GET <prefix>/actor`` — Actor profile
    - ``POST <prefix>/inbox`` — Inbox (receive activities)
    - ``GET <prefix>/outbox`` — Outbox collection
    - ``GET <prefix>/followers`` — Followers collection
    - ``GET <prefix>/following`` — Following collection

    :param app: The FastAPI application.
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
        the rate-limit bucket key (default: the client IP). Pass a
        trusted-proxy-aware resolver (or the signing ``keyId`` domain) when
        the app sits behind a reverse proxy — otherwise all remote servers
        share the proxy's bucket.
    """
    prefix = prefix.rstrip("/")
    router = APIRouter(prefix=prefix)
    if document_cache is None:
        document_cache = handler.document_cache
    elif handler.document_cache is None:
        handler.document_cache = document_cache

    def _serve_document(
        request: Request, key, render, media_type: str = ACTIVITY_JSON
    ) -> Response:
        """Serve a dereference document through the shared document cache."""
        if document_cache is None:
            doc = render()
            if doc is None:
                return JSONResponse(content={"error": "not found"}, status_code=404)
            return JSONResponse(content=doc, media_type=media_type)

        def _render() -> Optional[_cache.CachedResponse]:
            doc = render()
            if doc is None:
                return None
            return _cache.CachedResponse.document(doc, media_type)

        cached = document_cache.get_or_render(key, _render)
        if cached is None:
            return JSONResponse(content={"error": "not found"}, status_code=404)

        # Emit the entry's remaining freshness rather than the full TTL, so
        # edge caches and clients don't add their own max-age on top of ours.
        # With no freshness left (stale-if-error, invalidated mid-render, a
        # failed store write) the response must not be treated as fresh:
        # ``stale`` asks for an explicit zero-freshness policy instead of
        # dropping Cache-Control — unless caching itself is disabled, which
        # maps to ``no-store``.
        remaining = document_cache.ttl_remaining(key)
        headers = _cache.cache_headers(
            remaining,
            etag=cached.etag,
            stale=document_cache.default_ttl > 0,
        )
        if cached.etag and _cache.etag_matches(
            request.headers.get("if-none-match"), cached.etag
        ):
            return Response(status_code=304, headers=headers)
        return Response(
            content=cached.body,
            status_code=cached.status,
            media_type=cached.media_type,
            headers={**cached.headers, **headers},
        )

    def _rate_limit_bucket(request: Request) -> str:
        if rate_limit_key is not None:
            return rate_limit_key(request)
        return (
            getattr(request.client, "host", "unknown") if request.client else "unknown"
        )

    # -- WebFinger (well-known route, goes directly on app) --
    @app.get("/.well-known/webfinger")
    def webfinger(request: Request, resource: Optional[str] = None):
        if not resource:
            return JSONResponse(
                content={"error": "resource parameter is required"}, status_code=400
            )
        # Validate before cache lookup: a malformed resource must be
        # rejected uncached, or its 404 would poison the valid account's
        # cache entry (they normalize to the same key).
        if not handler.is_valid_webfinger_resource(resource):
            return JSONResponse(content={"error": "not found"}, status_code=404)

        return _serve_document(
            request,
            _cache.webfinger_key(resource),
            lambda: handler.get_webfinger_response(resource),
            media_type=JRD_JSON,
        )

    # -- NodeInfo discovery (well-known route, goes directly on app) --
    @app.get("/.well-known/nodeinfo")
    def nodeinfo_discovery(request: Request):
        return _serve_document(
            request,
            _cache.nodeinfo_discovery_key(),
            handler.get_nodeinfo_discovery,
            media_type="application/json",
        )

    # -- NodeInfo document (goes directly on app) --
    @app.get("/nodeinfo/2.1")
    def nodeinfo(request: Request):
        return _serve_document(
            request,
            _cache.nodeinfo_key(),
            handler.get_nodeinfo_document,
            media_type="application/json",
        )

    # -- Actor --
    @router.get("/actor")
    def actor(request: Request):
        return _serve_document(
            request, _cache.actor_document_key(), handler.get_actor_document
        )

    # -- Inbox --
    @router.post("/inbox")
    def inbox(request: Request, body: bytes = Depends(get_raw_body)):
        # Rate limiting
        if rate_limiter is not None:
            try:
                rate_limiter.check(_rate_limit_bucket(request))
            except RateLimitError:
                return JSONResponse(
                    content={"error": "rate limit exceeded"}, status_code=429
                )

        try:
            activity_data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(content={"error": "invalid JSON"}, status_code=400)

        # Collect headers
        headers = dict(request.headers)

        try:
            handler.process_inbox_activity(
                activity_data,
                method="POST",
                path=request.url.path,
                headers=headers,
                body=body,
            )
        except SignatureVerificationError as e:
            return JSONResponse(content={"error": str(e)}, status_code=401)
        except ActivityPubError as e:
            return JSONResponse(content={"error": str(e)}, status_code=400)

        return JSONResponse(content={"status": "ok"}, status_code=202)

    # -- Outbox --
    @router.get("/outbox")
    def outbox(request: Request, limit: int = 20, offset: int = 0):
        return _serve_document(
            request,
            _cache.outbox_key(limit, offset),
            lambda: handler.get_outbox(limit=limit, offset=offset),
        )

    # -- Followers --
    @router.get("/followers")
    def followers(request: Request):
        return _serve_document(
            request, _cache.followers_key(), handler.get_followers_collection
        )

    # -- Following --
    @router.get("/following")
    def following(request: Request):
        return _serve_document(
            request, _cache.following_key(), handler.get_following_collection
        )

    # Include the router with the prefix
    app.include_router(router)

    # -- Quote authorizations --
    # Registered directly on the app because auth IDs are derived from
    # actor_id (e.g. /ap/actor/quote_authorizations/{uuid}), which
    # includes the actor path, not just the prefix.
    @app.get(f"{handler.actor_path}/quote_authorizations/{{auth_id:path}}")
    def quote_authorization(request: Request, auth_id: str):
        full_id = f"{handler.actor_id}/quote_authorizations/{auth_id}"
        return _serve_document(
            request,
            _cache.quote_authorization_key(full_id),
            lambda: handler.get_quote_authorization(full_id),
        )
