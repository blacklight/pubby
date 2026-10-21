"""
Multi-actor support — mirrors ``handlers::multi`` in the Rust SDK.

A registry of :class:`ActivityPubHandler` instances keyed by local
username, for Mastodon-style servers hosting many actors. Pairs an
instance-level handler with an ``resolver`` callback that maps a
username to its handler.
"""

import logging
from typing import Callable, Optional

from ..cache import normalize_webfinger_resource
from ._discovery import build_webfinger_response
from ._handler import ActivityPubHandler

logger = logging.getLogger(__name__)

ActorResolver = Callable[[str], Optional[ActivityPubHandler]]


class MultiActorHandler:
    """
    Registry of local actors for multi-actor deployments.

    :param instance: Instance-level handler — serves ``{prefix}/actor``,
        signs instance-level fetches, and acts as the shared-inbox
        fallback.
    :param resolver: ``username -> Optional[ActivityPubHandler]``.
    :param actor_path_template: Actor path template containing
        ``{username}``, e.g. ``"/ap/users/{username}"``.
    :param webfinger_domain: Domain for WebFinger ``acct:`` resolution —
        defaults to the instance handler's domain.
    """

    def __init__(
        self,
        instance: ActivityPubHandler,
        resolver: ActorResolver,
        actor_path_template: str,
        webfinger_domain: Optional[str] = None,
    ):
        self.instance = instance
        self.resolver = resolver
        self.actor_path_template = actor_path_template
        self.webfinger_domain = webfinger_domain or instance.webfinger_domain

    def _template_parts(self):
        pre, _, suf = self.actor_path_template.partition("{username}")
        return pre, suf

    def actor_path(self, username: str) -> str:
        """The actor path for *username* (template substituted)."""
        pre, suf = self._template_parts()
        return f"{pre}{username}{suf}"

    def actor_url(self, username: str) -> str:
        """The full actor URL for *username* under the instance base URL."""
        return f"{self.instance.base_url.rstrip('/')}{self.actor_path(username)}"

    def username_from_path(self, path: str):
        """
        Extract ``(username, subpath)`` from a request path matching the
        template — ``None`` when it doesn't match or the segment is empty.
        """
        pre, suf = self._template_parts()
        if not path.startswith(pre):
            return None
        rest = path[len(pre) :]
        if suf:
            i = rest.find(suf)
            if i < 0:
                return None
            username, sub = rest[:i], rest[i + len(suf) :]
        else:
            i = rest.find("/")
            username, sub = (rest, "") if i < 0 else (rest[:i], rest[i:])
        if not username or "/" in username:
            return None
        return username, sub

    def for_username(self, username: str) -> Optional[ActivityPubHandler]:
        """Resolve the handler for *username* (``None`` when unknown)."""
        return self.resolver(username)

    def for_addressees(self, activity_data: dict) -> ActivityPubHandler:
        """
        Resolve the local handler an inbound activity addresses: scans
        ``to``/``cc``/``audience`` and ``object.attributedTo`` for URLs
        matching the actor template; falls back to the instance actor.
        """
        candidates = []

        def _collect(v):
            if isinstance(v, str):
                candidates.append(v)
            elif isinstance(v, list):
                candidates.extend(x for x in v if isinstance(x, str))

        for f in ("to", "cc", "audience"):
            _collect(activity_data.get(f))
        obj = activity_data.get("object")
        if isinstance(obj, dict):
            _collect(obj.get("attributedTo"))
            _collect(obj.get("to"))
            _collect(obj.get("cc"))

        base = self.instance.base_url.rstrip("/")
        for url in candidates:
            if not url.startswith(base):
                continue
            match = self.username_from_path(url[len(base) :])
            if not match:
                continue
            username, sub = match
            if sub and not sub.startswith("/"):
                continue
            handler = self.for_username(username)
            if handler is not None:
                return handler
        return self.instance

    def get_webfinger_response(self, resource: str) -> Optional[dict]:
        """WebFinger for any local actor (instance actor included)."""
        normalized = normalize_webfinger_resource(resource)
        if "@" not in normalized:
            return None
        username, domain = normalized.split("@", 1)
        if domain.lower() != self.webfinger_domain.lower():
            return None
        if username.lower() == self.instance.username.lower():
            handler = self.instance
        else:
            handler = self.for_username(username)
            if handler is None:
                return None
        return build_webfinger_response(
            username=handler.username,
            domain=self.webfinger_domain,
            actor_url=handler.actor_id,
        )
