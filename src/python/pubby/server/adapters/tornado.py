"""
Tornado server adapter for ActivityPub.

Registers all required routes on a Tornado application.
"""

import json
from typing import Optional, List, Tuple, Any

import tornado.web

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
        self, handler: ActivityPubHandler, rate_limiter: Optional[RateLimiter] = None
    ):
        self.ap_handler = handler
        self.rate_limiter = rate_limiter

    def write_json(self, data, content_type="application/json"):
        """Write JSON with explicit content type (avoids Tornado auto-setting)."""
        self.set_header("Content-Type", content_type)
        self.write(json.dumps(data))


class WebFingerHandler(BaseActivityPubHandler):
    """Handle WebFinger requests."""

    def get(self):
        resource = self.get_argument("resource", None)
        if not resource:
            self.set_status(400)
            self.write_json({"error": "resource parameter is required"})
            return

        result = self.ap_handler.get_webfinger_response(resource)
        if result is None:
            self.set_status(404)
            self.write_json({"error": "not found"})
            return

        self.write_json(result, JRD_JSON)


class NodeInfoDiscoveryHandler(BaseActivityPubHandler):
    """Handle NodeInfo discovery requests."""

    def get(self):
        self.write_json(self.ap_handler.get_nodeinfo_discovery())


class NodeInfoHandler(BaseActivityPubHandler):
    """Handle NodeInfo document requests."""

    def get(self):
        self.write_json(self.ap_handler.get_nodeinfo_document())


class ActorHandler(BaseActivityPubHandler):
    """Handle actor profile requests."""

    def get(self):
        doc = self.ap_handler.get_actor_document()
        self.write_json(doc, ACTIVITY_JSON)


class InboxHandler(BaseActivityPubHandler):
    """Handle inbox POST requests."""

    def post(self):
        # Rate limiting
        if self.rate_limiter is not None:
            client_ip = self.request.remote_ip or "unknown"
            try:
                self.rate_limiter.check(client_ip)
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
        doc = self.ap_handler.get_outbox(limit=limit, offset=offset)
        self.write_json(doc, ACTIVITY_JSON)


class FollowersHandler(BaseActivityPubHandler):
    """Handle followers collection requests."""

    def get(self):
        doc = self.ap_handler.get_followers_collection()
        self.write_json(doc, ACTIVITY_JSON)


class FollowingHandler(BaseActivityPubHandler):
    """Handle following collection requests."""

    def get(self):
        doc = self.ap_handler.get_following_collection()
        self.write_json(doc, ACTIVITY_JSON)


def bind_activitypub(
    app: tornado.web.Application,
    handler: ActivityPubHandler,
    prefix: str = "/ap",
    rate_limiter: Optional[RateLimiter] = None,
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
    :return: List of URL spec tuples (also adds them to the app).
    """
    prefix = prefix.rstrip("/")

    # Handler initialization kwargs
    init_kwargs = {
        "handler": handler,
        "rate_limiter": rate_limiter,
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
    ]

    # Add handlers to the application
    app.add_handlers(".*", url_patterns)

    return url_patterns
