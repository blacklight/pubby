"""
FastAPI server adapter for ActivityPub.

Registers all required routes on a FastAPI application.
"""

import json
from typing import Optional

from fastapi import FastAPI, APIRouter, Request, Depends
from fastapi.responses import JSONResponse

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


def _wants_activity_json(request: Request) -> bool:
    """Check if the client prefers ActivityPub JSON."""
    accept = request.headers.get("Accept", "")
    return "application/activity+json" in accept or "application/ld+json" in accept


async def get_raw_body(request: Request) -> bytes:
    """Dependency to get raw request body."""
    return await request.body()


def bind_activitypub(
    app: FastAPI,
    handler: ActivityPubHandler,
    prefix: str = "/ap",
    rate_limiter: Optional[RateLimiter] = None,
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
    """
    prefix = prefix.rstrip("/")
    router = APIRouter(prefix=prefix)

    # -- WebFinger (well-known route, goes directly on app) --
    @app.get("/.well-known/webfinger")
    def webfinger(resource: Optional[str] = None):
        if not resource:
            return JSONResponse(
                content={"error": "resource parameter is required"}, status_code=400
            )

        result = handler.get_webfinger_response(resource)
        if result is None:
            return JSONResponse(content={"error": "not found"}, status_code=404)

        return JSONResponse(content=result, media_type=JRD_JSON)

    # -- NodeInfo discovery (well-known route, goes directly on app) --
    @app.get("/.well-known/nodeinfo")
    def nodeinfo_discovery():
        return JSONResponse(content=handler.get_nodeinfo_discovery())

    # -- NodeInfo document (goes directly on app) --
    @app.get("/nodeinfo/2.1")
    def nodeinfo():
        return JSONResponse(content=handler.get_nodeinfo_document())

    # -- Actor --
    @router.get("/actor")
    def actor(request: Request):
        doc = handler.get_actor_document()
        return JSONResponse(content=doc, media_type=ACTIVITY_JSON)

    # -- Inbox --
    @router.post("/inbox")
    def inbox(request: Request, body: bytes = Depends(get_raw_body)):
        # Rate limiting
        if rate_limiter is not None:
            client_ip = (
                getattr(request.client, "host", "unknown")
                if request.client
                else "unknown"
            )
            try:
                rate_limiter.check(client_ip)
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
    def outbox(limit: int = 20, offset: int = 0):
        doc = handler.get_outbox(limit=limit, offset=offset)
        return JSONResponse(content=doc, media_type=ACTIVITY_JSON)

    # -- Followers --
    @router.get("/followers")
    def followers():
        doc = handler.get_followers_collection()
        return JSONResponse(content=doc, media_type=ACTIVITY_JSON)

    # -- Following --
    @router.get("/following")
    def following():
        doc = handler.get_following_collection()
        return JSONResponse(content=doc, media_type=ACTIVITY_JSON)

    # Include the router with the prefix
    app.include_router(router)
