"""
Framework-agnostic Mastodon API route handlers.

Each method returns a ``(body_dict, status_code)`` tuple that the
framework adapter turns into an HTTP response.
"""

from typing import Any
from urllib.parse import urlparse

from ...handlers import ActivityPubHandler
from ._mappers import (
    _ACCOUNT_LOCAL_ID,
    activity_to_status,
    actor_to_account,
    follower_to_account,
    id_to_url,
)


class MastodonAPI:
    """
    Stateless request handler for the Mastodon-compatible API surface.

    :param handler: The Pubby :class:`ActivityPubHandler`.
    :param title: Instance title (defaults to the actor name).
    :param description: Instance description.
    :param contact_email: Contact e-mail shown in instance info.
    :param software_name: Software name for the ``version`` field.
    :param software_version: Software version string.
    """

    def __init__(
        self,
        handler: ActivityPubHandler,
        *,
        title: str | None = None,
        description: str | None = None,
        contact_email: str = "",
        software_name: str | None = None,
        software_version: str | None = None,
    ):
        self.handler = handler
        self.title = title or handler.actor_name or handler.username
        self.description = description or handler.actor_summary or ""
        self.contact_email = contact_email
        self.software_name = software_name or handler.software_name
        self.software_version = software_version or handler.software_version

    # -- helpers --------------------------------------------------------------

    def _account(self) -> dict[str, Any]:
        return actor_to_account(self.handler)

    def _peer_domains(self) -> list[str]:
        followers = self.handler.storage.get_followers()
        domains: set[str] = set()
        for f in followers:
            parsed = urlparse(f.actor_id)
            if parsed.hostname:
                domains.add(parsed.hostname)
        return sorted(domains)

    # -- /api/v1/instance -----------------------------------------------------

    def instance_v1(self) -> tuple[dict[str, Any], int]:
        """``GET /api/v1/instance``"""
        account = self._account()
        activities = self.handler.storage.get_activities(limit=10000, offset=0)
        peers = self._peer_domains()

        version_str = (
            f"{self.software_name} {self.software_version} " f"(Mastodon-compatible)"
        )

        body: dict[str, Any] = {
            "uri": self.handler.webfinger_domain,
            "title": self.title,
            "description": self.description,
            "short_description": self.description,
            "email": self.contact_email,
            "version": version_str,
            "urls": {
                "streaming_api": "",
            },
            "stats": {
                "user_count": 1,
                "status_count": len(activities),
                "domain_count": len(peers),
            },
            "thumbnail": self.handler.icon_url or None,
            "languages": ["en"],
            "registrations": False,
            "approval_required": False,
            "invites_enabled": False,
            "configuration": {
                "statuses": {
                    "max_characters": 500,
                    "max_media_attachments": 4,
                },
                "media_attachments": {
                    "supported_mime_types": [
                        "image/jpeg",
                        "image/png",
                        "image/gif",
                        "image/webp",
                        "video/mp4",
                        "audio/mpeg",
                    ],
                    "image_size_limit": 10485760,
                    "video_size_limit": 41943040,
                },
                "polls": {
                    "max_options": 4,
                    "max_characters_per_option": 50,
                    "min_expiration": 300,
                    "max_expiration": 2629746,
                },
            },
            "contact_account": account,
            "rules": [],
        }

        return body, 200

    # -- /api/v2/instance -----------------------------------------------------

    def instance_v2(self) -> tuple[dict[str, Any], int]:
        """``GET /api/v2/instance``"""
        account = self._account()

        body: dict[str, Any] = {
            "domain": self.handler.webfinger_domain,
            "title": self.title,
            "version": (
                f"{self.software_name} {self.software_version} "
                f"(Mastodon-compatible)"
            ),
            "source_url": "",
            "description": self.description,
            "usage": {
                "users": {
                    "active_month": 1,
                },
            },
            "thumbnail": {
                "url": self.handler.icon_url or "",
            },
            "languages": ["en"],
            "configuration": {
                "urls": {
                    "streaming": "",
                },
                "accounts": {
                    "max_featured_tags": 0,
                },
                "statuses": {
                    "max_characters": 500,
                    "max_media_attachments": 4,
                    "characters_reserved_per_url": 23,
                },
                "media_attachments": {
                    "supported_mime_types": [
                        "image/jpeg",
                        "image/png",
                        "image/gif",
                        "image/webp",
                        "video/mp4",
                        "audio/mpeg",
                    ],
                    "image_size_limit": 10485760,
                    "video_size_limit": 41943040,
                },
                "polls": {
                    "max_options": 4,
                    "max_characters_per_option": 50,
                    "min_expiration": 300,
                    "max_expiration": 2629746,
                },
                "translation": {
                    "enabled": False,
                },
            },
            "registrations": {
                "enabled": False,
                "approval_required": False,
                "message": None,
            },
            "contact": {
                "email": self.contact_email,
                "account": account,
            },
            "rules": [],
        }

        return body, 200

    # -- /api/v1/instance/peers -----------------------------------------------

    def instance_peers(self) -> tuple[list[str], int]:
        """``GET /api/v1/instance/peers``"""
        return self._peer_domains(), 200

    # -- /api/v1/accounts/lookup ----------------------------------------------

    def accounts_lookup(self, acct: str | None) -> tuple[dict[str, Any], int]:
        """``GET /api/v1/accounts/lookup?acct=...``"""
        if not acct:
            return {"error": "Missing acct parameter"}, 400

        # Normalise: strip leading @
        normalized = acct.lstrip("@")
        expected = f"{self.handler.username}@{self.handler.webfinger_domain}"

        if normalized.lower() != expected.lower():
            # Also accept bare username
            if normalized.lower() != self.handler.username.lower():
                return {"error": "Record not found"}, 404

        return self._account(), 200

    # -- /api/v1/accounts/:id -------------------------------------------------

    def accounts_get(self, account_id: str) -> tuple[dict[str, Any], int]:
        """``GET /api/v1/accounts/:id``"""
        if account_id != _ACCOUNT_LOCAL_ID:
            return {"error": "Record not found"}, 404

        return self._account(), 200

    # -- /api/v1/accounts/:id/statuses ----------------------------------------

    def accounts_statuses(
        self,
        account_id: str,
        *,
        limit: int = 20,
        max_id: str | None = None,
        since_id: str | None = None,
        only_media: bool = False,
        tagged: str | None = None,
        **_: Any,
    ) -> tuple[list[dict[str, Any]] | dict[str, Any], int]:
        """``GET /api/v1/accounts/:id/statuses``"""
        if account_id != _ACCOUNT_LOCAL_ID:
            return {"error": "Record not found"}, 404

        limit = min(max(limit, 1), 40)

        # Fetch a generous batch; we filter/cursor in-memory.
        all_activities = self.handler.storage.get_activities(limit=10000, offset=0)

        account = self._account()
        statuses = [
            activity_to_status(a, self.handler, account=account) for a in all_activities
        ]

        # Cursor-based pagination
        if max_id:
            idx = next(
                (i for i, s in enumerate(statuses) if s["id"] == max_id),
                None,
            )
            if idx is not None:
                statuses = statuses[idx + 1 :]

        if since_id:
            idx = next(
                (i for i, s in enumerate(statuses) if s["id"] == since_id),
                None,
            )
            if idx is not None:
                statuses = statuses[:idx]

        # Filtering
        if only_media:
            statuses = [s for s in statuses if s["media_attachments"]]

        if tagged:
            tag_lower = tagged.lower()
            statuses = [
                s
                for s in statuses
                if any(t.get("name", "").lower() == tag_lower for t in s["tags"])
            ]

        return statuses[:limit], 200

    # -- /api/v1/accounts/:id/followers ---------------------------------------

    def accounts_followers(
        self,
        account_id: str,
        *,
        limit: int = 40,
        max_id: str | None = None,
        since_id: str | None = None,
    ) -> tuple[list[dict[str, Any]] | dict[str, Any], int]:
        """``GET /api/v1/accounts/:id/followers``"""
        if account_id != _ACCOUNT_LOCAL_ID:
            return {"error": "Record not found"}, 404

        limit = min(max(limit, 1), 80)
        followers = self.handler.storage.get_followers()
        accounts = [follower_to_account(f) for f in followers]

        # Cursor-based pagination
        if max_id:
            idx = next(
                (i for i, a in enumerate(accounts) if a["id"] == max_id),
                None,
            )
            if idx is not None:
                accounts = accounts[idx + 1 :]

        if since_id:
            idx = next(
                (i for i, a in enumerate(accounts) if a["id"] == since_id),
                None,
            )
            if idx is not None:
                accounts = accounts[:idx]

        return accounts[:limit], 200

    # -- /api/v1/statuses/:id -------------------------------------------------

    def statuses_get(self, status_id: str) -> tuple[dict[str, Any], int]:
        """``GET /api/v1/statuses/:id``"""
        # Decode the status_id back to the AP object URL
        try:
            object_url = id_to_url(status_id)
        except Exception:
            return {"error": "Record not found"}, 404

        # Search outbox for a matching activity
        activities = self.handler.storage.get_activities(limit=10000, offset=0)

        for act in activities:
            obj = act.get("object", {})
            if isinstance(obj, dict):
                obj_id = obj.get("id", "")
            elif isinstance(obj, str):
                obj_id = obj
            else:
                continue

            if obj_id == object_url:
                return activity_to_status(act, self.handler), 200

        return {"error": "Record not found"}, 404
