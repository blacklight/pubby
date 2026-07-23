"""
Flask server adapter for the Mastodon-compatible API.

Registers read-only Mastodon REST API routes on a Flask application.
"""

import flask as flask_upstream  # pylint: disable=W0406

if getattr(flask_upstream, "__file__", None) == __file__:
    raise RuntimeError(
        "Local module name 'flask_mastodon.py' is shadowing the upstream "
        "'flask' dependency."
    )

Flask = flask_upstream.Flask
jsonify = flask_upstream.jsonify
request = flask_upstream.request

from ...handlers import ActivityPubHandler  # noqa: E402
from ..mastodon._routes import MastodonAPI  # noqa: E402


def bind_mastodon_api(
    app: Flask,
    handler: ActivityPubHandler,
    *,
    title: str | None = None,
    description: str | None = None,
    contact_email: str = "",
    software_name: str | None = None,
    software_version: str | None = None,
):
    """
    Bind Mastodon-compatible API routes to a Flask application.

    Registers the following endpoints:

    - ``GET /api/v1/instance``
    - ``GET /api/v2/instance``
    - ``GET /api/v1/instance/peers``
    - ``GET /api/v1/accounts/lookup``
    - ``GET /api/v1/accounts/:id``
    - ``GET /api/v1/accounts/:id/statuses``
    - ``GET /api/v1/accounts/:id/followers``
    - ``GET /api/v1/statuses/:id``
    - ``GET /nodeinfo/2.0`` (alias)
    - ``GET /nodeinfo/2.0.json`` (alias)
    - ``GET /nodeinfo/2.1.json`` (alias)

    :param app: The Flask application.
    :param handler: The ActivityPubHandler instance.
    :param title: Instance title (defaults to the actor name).
    :param description: Instance description.
    :param contact_email: Contact e-mail shown in instance info.
    :param software_name: Software name override.
    :param software_version: Software version override.
    """
    api = MastodonAPI(
        handler,
        title=title,
        description=description,
        contact_email=contact_email,
        software_name=software_name,
        software_version=software_version,
    )

    # -- Instance info --

    @app.route("/api/v1/instance", methods=["GET"])
    def _mastodon_instance_v1():
        body, status = api.instance_v1()
        return jsonify(body), status

    @app.route("/api/v2/instance", methods=["GET"])
    def _mastodon_instance_v2():
        body, status = api.instance_v2()
        return jsonify(body), status

    @app.route("/api/v1/instance/peers", methods=["GET"])
    def _mastodon_instance_peers():
        body, status = api.instance_peers()
        return jsonify(body), status

    # -- NodeInfo aliases --

    @app.route("/nodeinfo/2.0", methods=["GET"])
    @app.route("/nodeinfo/2.0.json", methods=["GET"])
    def _nodeinfo_20():
        return jsonify(handler.get_nodeinfo_document())

    @app.route("/nodeinfo/2.1.json", methods=["GET"])
    def _nodeinfo_21_json():
        return jsonify(handler.get_nodeinfo_document())

    # -- Accounts --

    @app.route("/api/v1/accounts/lookup", methods=["GET"])
    def _mastodon_accounts_lookup():
        acct = request.args.get("acct")
        body, status = api.accounts_lookup(acct)
        return jsonify(body), status

    @app.route("/api/v1/accounts/<account_id>", methods=["GET"])
    def _mastodon_accounts_get(account_id):
        body, status = api.accounts_get(account_id)
        return jsonify(body), status

    @app.route("/api/v1/accounts/<account_id>/statuses", methods=["GET"])
    def _mastodon_accounts_statuses(account_id):
        body, status = api.accounts_statuses(
            account_id,
            limit=request.args.get("limit", 20, type=int),
            max_id=request.args.get("max_id"),
            since_id=request.args.get("since_id"),
            only_media=request.args.get("only_media", "false").lower() == "true",
            exclude_replies=request.args.get("exclude_replies", "false").lower()
            == "true",
            exclude_reblogs=request.args.get("exclude_reblogs", "false").lower()
            == "true",
            tagged=request.args.get("tagged"),
        )
        return jsonify(body), status

    @app.route("/api/v1/accounts/<account_id>/followers", methods=["GET"])
    def _mastodon_accounts_followers(account_id):
        body, status = api.accounts_followers(
            account_id,
            limit=request.args.get("limit", 40, type=int),
            max_id=request.args.get("max_id"),
            since_id=request.args.get("since_id"),
        )
        return jsonify(body), status

    # -- Statuses --

    @app.route("/api/v1/statuses/<status_id>", methods=["GET"])
    def _mastodon_statuses_get(status_id):
        body, status = api.statuses_get(status_id)
        return jsonify(body), status
