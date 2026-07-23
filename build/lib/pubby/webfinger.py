"""
WebFinger client utilities for resolving ActivityPub actor URLs.
"""

import logging
import re
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"(?<!\w)@([a-zA-Z0-9_.-]+)@([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})")


@dataclass
class Mention:
    """A resolved ``@user@domain`` mention."""

    username: str
    domain: str
    actor_url: str

    @property
    def acct(self) -> str:
        return f"@{self.username}@{self.domain}"

    def to_tag(self) -> dict:
        """Return an ActivityPub ``Mention`` tag dict."""
        return {
            "type": "Mention",
            "href": self.actor_url,
            "name": self.acct,
        }


def resolve_actor_url(
    username: str,
    domain: str,
    *,
    timeout: int = 10,
) -> str:
    """
    Resolve the ActivityPub actor URL for *@username@domain* via WebFinger.

    Falls back to ``https://{domain}/@{username}`` when the lookup fails or
    no ``self`` link is present.
    """
    fallback = f"https://{domain}/@{username}"
    try:
        resp = requests.get(
            f"https://{domain}/.well-known/webfinger",
            params={"resource": f"acct:{username}@{domain}"},
            headers={"Accept": "application/jrd+json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        for link in data.get("links", []):
            if link.get("rel") == "self" and link.get("type", "").startswith(
                "application/"
            ):
                return link["href"]
    except Exception:
        logger.warning(
            "WebFinger lookup failed for @%s@%s, using fallback",
            username,
            domain,
        )
    return fallback


def extract_mentions(text: str, *, timeout: int = 10) -> list[Mention]:
    """
    Find all ``@user@domain`` patterns in *text* and resolve each via
    WebFinger.  Returns a list of :class:`Mention` objects.
    """
    seen: set[tuple[str, str]] = set()
    mentions: list[Mention] = []
    for username, domain in _MENTION_RE.findall(text):
        key = (username.lower(), domain.lower())
        if key in seen:
            continue
        seen.add(key)
        actor_url = resolve_actor_url(username, domain, timeout=timeout)
        mentions.append(Mention(username=username, domain=domain, actor_url=actor_url))
    return mentions
