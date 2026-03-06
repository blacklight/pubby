"""
Flask server adapter for ActivityPub.

Registers all required routes on a Flask application.
"""

import json

import flask as flask_upstream  # pylint: disable=W0406

if getattr(flask_upstream, "__file__", None) == __file__:
    raise RuntimeError(
        "Local module name 'flask.py' is shadowing the upstream 'flask' dependency. "
        "Do not run this file directly; import it as 'pubby.server.adapters.flask'."
    )

Flask = flask_upstream.Flask
jsonify = flask_upstream.jsonify
request = flask_upstream.request

from ...handlers import ActivityPubHandler  # noqa: E402
from ..._exceptions import (  # noqa: E402
    ActivityPubError,
    RateLimitError,
    SignatureVerificationError,
)  # noqa: E402
from ..._rate_limit import RateLimiter  # noqa: E402

# Content types
ACTIVITY_JSON = "application/activity+json"
JRD_JSON = "application/jrd+json"


def _wants_activity_json() -> bool:
    """Check if the client prefers ActivityPub JSON."""
    accept = request.headers.get("Accept", "")
    return "application/activity+json" in accept or "application/ld+json" in accept


def bind_activitypub(
    app: Flask,
    handler: ActivityPubHandler,
    prefix: str = "/ap",
    rate_limiter: RateLimiter | None = None,
):
    """
    Bind ActivityPub routes to a Flask application.

    Registers the following endpoints:

    - ``GET /.well-known/webfinger`` — WebFinger discovery
    - ``GET /.well-known/nodeinfo`` — NodeInfo discovery
    - ``GET /nodeinfo/2.1`` — NodeInfo document
    - ``GET <prefix>/actor`` — Actor profile
    - ``POST <prefix>/inbox`` — Inbox (receive activities)
    - ``GET <prefix>/outbox`` — Outbox collection
    - ``GET <prefix>/followers`` — Followers collection
    - ``GET <prefix>/following`` — Following collection

    :param app: The Flask application.
    :param handler: The ActivityPubHandler instance.
    :param prefix: URL prefix for AP routes (default ``/ap``).
    :param rate_limiter: Optional rate limiter for the inbox endpoint.
    """
    prefix = prefix.rstrip("/")

    # -- WebFinger --
    @app.route("/.well-known/webfinger", methods=["GET"])
    def _webfinger():
        resource = request.args.get("resource")
        if not resource:
            return jsonify({"error": "resource parameter is required"}), 400

        result = handler.get_webfinger_response(resource)
        if result is None:
            return jsonify({"error": "not found"}), 404

        response = jsonify(result)
        response.headers["Content-Type"] = JRD_JSON
        return response

    # -- NodeInfo discovery --
    @app.route("/.well-known/nodeinfo", methods=["GET"])
    def _nodeinfo_discovery():
        return jsonify(handler.get_nodeinfo_discovery())

    # -- NodeInfo document --
    @app.route("/nodeinfo/2.1", methods=["GET"])
    def _nodeinfo():
        return jsonify(handler.get_nodeinfo_document())

    # -- Actor --
    @app.route(f"{prefix}/actor", methods=["GET"])
    def _actor():
        doc = handler.get_actor_document()
        if _wants_activity_json():
            response = jsonify(doc)
            response.headers["Content-Type"] = ACTIVITY_JSON
            return response
        # For HTML clients, return a simple redirect or the JSON anyway
        response = jsonify(doc)
        response.headers["Content-Type"] = ACTIVITY_JSON
        return response

    # -- Inbox --
    @app.route(f"{prefix}/inbox", methods=["POST"])
    def _inbox():
        # Rate limiting
        if rate_limiter is not None:
            client_ip = request.remote_addr or "unknown"
            try:
                rate_limiter.check(client_ip)
            except RateLimitError:
                return jsonify({"error": "rate limit exceeded"}), 429

        body = request.get_data()
        try:
            activity_data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return jsonify({"error": "invalid JSON"}), 400

        # Collect headers
        headers = dict(request.headers)

        try:
            handler.process_inbox_activity(
                activity_data,
                method="POST",
                path=request.path,
                headers=headers,
                body=body,
            )
        except SignatureVerificationError as e:
            return jsonify({"error": str(e)}), 401
        except ActivityPubError as e:
            return jsonify({"error": str(e)}), 400

        return jsonify({"status": "ok"}), 202

    # -- Outbox --
    @app.route(f"{prefix}/outbox", methods=["GET"])
    def _outbox():
        limit = request.args.get("limit", 20, type=int)
        offset = request.args.get("offset", 0, type=int)
        doc = handler.get_outbox(limit=limit, offset=offset)
        response = jsonify(doc)
        response.headers["Content-Type"] = ACTIVITY_JSON
        return response

    # -- Followers --
    @app.route(f"{prefix}/followers", methods=["GET"])
    def _followers():
        doc = handler.get_followers_collection()
        response = jsonify(doc)
        response.headers["Content-Type"] = ACTIVITY_JSON
        return response

    # -- Following --
    @app.route(f"{prefix}/following", methods=["GET"])
    def _following():
        doc = handler.get_following_collection()
        response = jsonify(doc)
        response.headers["Content-Type"] = ACTIVITY_JSON
        return response
