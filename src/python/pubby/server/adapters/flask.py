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
Response = flask_upstream.Response
jsonify = flask_upstream.jsonify
request = flask_upstream.request

from typing import Callable  # noqa: E402

from ... import cache as _cache  # noqa: E402
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


def bind_activitypub(
    app: Flask,
    handler: ActivityPubHandler,
    prefix: str = "/ap",
    rate_limiter: RateLimiter | None = None,
    document_cache: "_cache.DocumentCache | None" = None,
    rate_limit_key: Callable | None = None,
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
    :param document_cache: Optional :class:`pubby.cache.DocumentCache`
        shared by the dereference GET endpoints: concurrent fetches of the
        same document share one render (single-flight), hits cost nothing,
        and responses carry ``Cache-Control``/``Vary``/``ETag`` headers with
        ``If-None-Match`` support. Falls back to ``handler.document_cache``;
        when a cache is provided here and the handler has none, it is also
        set on the handler so mutations invalidate the right entries.
    :param rate_limit_key: Optional callable mapping the incoming request to
        the rate-limit bucket key (default: ``request.remote_addr``). Pass a
        trusted-proxy-aware resolver (or the signing ``keyId`` domain) when
        the app sits behind a reverse proxy — otherwise all remote servers
        share the proxy's bucket.
    """
    prefix = prefix.rstrip("/")
    if document_cache is None:
        document_cache = handler.document_cache
    elif handler.document_cache is None:
        handler.document_cache = document_cache

    def _serve_document(key, render, media_type: str = ACTIVITY_JSON):
        """Serve a dereference document through the shared document cache."""
        if document_cache is None:
            doc = render()
            if doc is None:
                return jsonify({"error": "not found"}), 404
            response = jsonify(doc)
            response.headers["Content-Type"] = media_type
            return response

        def _render():
            doc = render()
            if doc is None:
                return None
            return _cache.CachedResponse.document(doc, media_type)

        cached = document_cache.get_or_render(key, _render)
        if cached is None:
            return jsonify({"error": "not found"}), 404

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
            request.headers.get("If-None-Match"), cached.etag
        ):
            return Response(status=304, headers=headers)
        return Response(
            cached.body,
            status=cached.status,
            content_type=cached.media_type,
            headers={**cached.headers, **headers},
        )

    def _rate_limit_bucket() -> str:
        if rate_limit_key is not None:
            return rate_limit_key(request)
        return request.remote_addr or "unknown"

    # -- WebFinger --
    @app.route("/.well-known/webfinger", methods=["GET"])
    def _webfinger():
        resource = request.args.get("resource")
        if not resource:
            return jsonify({"error": "resource parameter is required"}), 400
        # Validate before cache lookup: a malformed resource must be
        # rejected uncached, or its 404 would poison the valid account's
        # cache entry (they normalize to the same key).
        if not handler.is_valid_webfinger_resource(resource):
            return jsonify({"error": "not found"}), 404

        return _serve_document(
            _cache.webfinger_key(resource),
            lambda: handler.get_webfinger_response(resource),
            media_type=JRD_JSON,
        )

    # -- NodeInfo discovery --
    @app.route("/.well-known/nodeinfo", methods=["GET"])
    def _nodeinfo_discovery():
        return _serve_document(
            _cache.nodeinfo_discovery_key(),
            handler.get_nodeinfo_discovery,
            media_type="application/json",
        )

    # -- NodeInfo document --
    @app.route("/nodeinfo/2.1", methods=["GET"])
    def _nodeinfo():
        return _serve_document(
            _cache.nodeinfo_key(),
            handler.get_nodeinfo_document,
            media_type="application/json",
        )

    # -- Actor --
    @app.route(f"{prefix}/actor", methods=["GET"])
    def _actor():
        return _serve_document(_cache.actor_document_key(), handler.get_actor_document)

    # -- Inbox --
    @app.route(f"{prefix}/inbox", methods=["POST"])
    def _inbox():
        # Rate limiting
        if rate_limiter is not None:
            try:
                rate_limiter.check(_rate_limit_bucket())
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
        return _serve_document(
            _cache.outbox_key(limit, offset),
            lambda: handler.get_outbox(limit=limit, offset=offset),
        )

    # -- Followers --
    @app.route(f"{prefix}/followers", methods=["GET"])
    def _followers():
        return _serve_document(_cache.followers_key(), handler.get_followers_collection)

    # -- Following --
    @app.route(f"{prefix}/following", methods=["GET"])
    def _following():
        return _serve_document(_cache.following_key(), handler.get_following_collection)

    # -- Quote authorizations --
    @app.route(
        f"{handler.actor_path}/quote_authorizations/<path:auth_id>", methods=["GET"]
    )
    def _quote_authorization(auth_id):
        full_id = f"{handler.actor_id}/quote_authorizations/{auth_id}"
        return _serve_document(
            _cache.quote_authorization_key(full_id),
            lambda: handler.get_quote_authorization(full_id),
        )
