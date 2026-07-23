"""
FastAPI server adapter for the Mastodon-compatible API.

Registers read-only Mastodon REST API routes on a FastAPI application.
"""

from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ...handlers import ActivityPubHandler
from ..mastodon._routes import MastodonAPI


def bind_mastodon_api(
    app: FastAPI,
    handler: ActivityPubHandler,
    *,
    title: str | None = None,
    description: str | None = None,
    contact_email: str = "",
    software_name: str | None = None,
    software_version: str | None = None,
):
    """
    Bind Mastodon-compatible API routes to a FastAPI application.

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

    :param app: The FastAPI application.
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

    @app.get("/api/v1/instance")
    def mastodon_instance_v1():
        body, status = api.instance_v1()
        return JSONResponse(content=body, status_code=status)

    @app.get("/api/v2/instance")
    def mastodon_instance_v2():
        body, status = api.instance_v2()
        return JSONResponse(content=body, status_code=status)

    @app.get("/api/v1/instance/peers")
    def mastodon_instance_peers():
        body, status = api.instance_peers()
        return JSONResponse(content=body, status_code=status)

    # -- NodeInfo aliases --

    @app.get("/nodeinfo/2.0")
    @app.get("/nodeinfo/2.0.json")
    def nodeinfo_20():
        return JSONResponse(content=handler.get_nodeinfo_document())

    @app.get("/nodeinfo/2.1.json")
    def nodeinfo_21_json():
        return JSONResponse(content=handler.get_nodeinfo_document())

    # -- Accounts --

    @app.get("/api/v1/accounts/lookup")
    def mastodon_accounts_lookup(acct: Optional[str] = None):
        body, status = api.accounts_lookup(acct)
        return JSONResponse(content=body, status_code=status)

    @app.get("/api/v1/accounts/{account_id}/statuses")
    def mastodon_accounts_statuses(
        account_id: str,
        limit: int = 20,
        max_id: Optional[str] = None,
        since_id: Optional[str] = None,
        only_media: bool = False,
        exclude_replies: bool = False,
        exclude_reblogs: bool = False,
        tagged: Optional[str] = None,
    ):
        body, status = api.accounts_statuses(
            account_id,
            limit=limit,
            max_id=max_id,
            since_id=since_id,
            only_media=only_media,
            exclude_replies=exclude_replies,
            exclude_reblogs=exclude_reblogs,
            tagged=tagged,
        )
        return JSONResponse(content=body, status_code=status)

    @app.get("/api/v1/accounts/{account_id}/followers")
    def mastodon_accounts_followers(
        account_id: str,
        limit: int = 40,
        max_id: Optional[str] = None,
        since_id: Optional[str] = None,
    ):
        body, status = api.accounts_followers(
            account_id,
            limit=limit,
            max_id=max_id,
            since_id=since_id,
        )
        return JSONResponse(content=body, status_code=status)

    @app.get("/api/v1/accounts/{account_id}")
    def mastodon_accounts_get(account_id: str):
        body, status = api.accounts_get(account_id)
        return JSONResponse(content=body, status_code=status)

    # -- Statuses --

    @app.get("/api/v1/statuses/{status_id}")
    def mastodon_statuses_get(status_id: str):
        body, status = api.statuses_get(status_id)
        return JSONResponse(content=body, status_code=status)
