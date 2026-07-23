"""
Tornado server adapter for the Mastodon-compatible API.

Registers read-only Mastodon REST API routes on a Tornado application.
"""

import json
from typing import Any, List, Tuple

import tornado.web

from ...handlers import ActivityPubHandler
from ..mastodon._routes import MastodonAPI


class BaseMastodonHandler(tornado.web.RequestHandler):
    """Base handler with shared functionality."""

    def initialize(self, api: MastodonAPI, ap_handler: ActivityPubHandler):
        self.api = api
        self.ap_handler = ap_handler

    def write_json(self, data, status_code: int = 200):
        """Write JSON response."""
        self.set_status(status_code)
        self.set_header("Content-Type", "application/json")
        self.write(json.dumps(data))


class InstanceV1Handler(BaseMastodonHandler):
    def get(self):
        body, status = self.api.instance_v1()
        self.write_json(body, status)


class InstanceV2Handler(BaseMastodonHandler):
    def get(self):
        body, status = self.api.instance_v2()
        self.write_json(body, status)


class InstancePeersHandler(BaseMastodonHandler):
    def get(self):
        body, status = self.api.instance_peers()
        self.write_json(body, status)


class NodeInfo20Handler(BaseMastodonHandler):
    def get(self):
        self.write_json(self.ap_handler.get_nodeinfo_document())


class NodeInfo21JsonHandler(BaseMastodonHandler):
    def get(self):
        self.write_json(self.ap_handler.get_nodeinfo_document())


class AccountsLookupHandler(BaseMastodonHandler):
    def get(self):
        acct = self.get_argument("acct", None)
        body, status = self.api.accounts_lookup(acct)
        self.write_json(body, status)


class AccountsGetHandler(BaseMastodonHandler):
    def get(self, account_id):
        body, status = self.api.accounts_get(account_id)
        self.write_json(body, status)


class AccountsStatusesHandler(BaseMastodonHandler):
    def get(self, account_id):
        body, status = self.api.accounts_statuses(
            account_id,
            limit=int(self.get_argument("limit", "20")),
            max_id=self.get_argument("max_id", None),
            since_id=self.get_argument("since_id", None),
            only_media=self.get_argument("only_media", "false").lower() == "true",
            exclude_replies=self.get_argument("exclude_replies", "false").lower()
            == "true",
            exclude_reblogs=self.get_argument("exclude_reblogs", "false").lower()
            == "true",
            tagged=self.get_argument("tagged", None),
        )
        self.write_json(body, status)


class AccountsFollowersHandler(BaseMastodonHandler):
    def get(self, account_id):
        body, status = self.api.accounts_followers(
            account_id,
            limit=int(self.get_argument("limit", "40")),
            max_id=self.get_argument("max_id", None),
            since_id=self.get_argument("since_id", None),
        )
        self.write_json(body, status)


class StatusesGetHandler(BaseMastodonHandler):
    def get(self, status_id):
        body, status = self.api.statuses_get(status_id)
        self.write_json(body, status)


def bind_mastodon_api(
    app: tornado.web.Application,
    handler: ActivityPubHandler,
    *,
    title: str | None = None,
    description: str | None = None,
    contact_email: str = "",
    software_name: str | None = None,
    software_version: str | None = None,
) -> List[Tuple[str, Any, dict]]:
    """
    Bind Mastodon-compatible API routes to a Tornado application.

    :param app: The Tornado application.
    :param handler: The ActivityPubHandler instance.
    :param title: Instance title (defaults to the actor name).
    :param description: Instance description.
    :param contact_email: Contact e-mail shown in instance info.
    :param software_name: Software name override.
    :param software_version: Software version override.
    :return: List of URL spec tuples (also adds them to the app).
    """
    api = MastodonAPI(
        handler,
        title=title,
        description=description,
        contact_email=contact_email,
        software_name=software_name,
        software_version=software_version,
    )

    init_kwargs: dict[str, Any] = {"api": api, "ap_handler": handler}

    url_patterns: List[Tuple[str, Any, dict]] = [
        (r"/api/v1/instance/peers", InstancePeersHandler, init_kwargs),
        (r"/api/v1/instance", InstanceV1Handler, init_kwargs),
        (r"/api/v2/instance", InstanceV2Handler, init_kwargs),
        (r"/nodeinfo/2\.0", NodeInfo20Handler, init_kwargs),
        (r"/nodeinfo/2\.0\.json", NodeInfo20Handler, init_kwargs),
        (r"/nodeinfo/2\.1\.json", NodeInfo21JsonHandler, init_kwargs),
        (r"/api/v1/accounts/lookup", AccountsLookupHandler, init_kwargs),
        (
            r"/api/v1/accounts/([^/]+)/statuses",
            AccountsStatusesHandler,
            init_kwargs,
        ),
        (
            r"/api/v1/accounts/([^/]+)/followers",
            AccountsFollowersHandler,
            init_kwargs,
        ),
        (r"/api/v1/accounts/([^/]+)", AccountsGetHandler, init_kwargs),
        (r"/api/v1/statuses/([^/]+)", StatusesGetHandler, init_kwargs),
    ]

    app.add_handlers(".*", url_patterns)
    return url_patterns
