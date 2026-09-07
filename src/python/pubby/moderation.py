"""
Instance-level moderation helpers — domain allow/block lists.

Stdlib-only helpers for normalizing and comparing remote instance domains.
The application owns the policy (which instances are allowed or blocked);
Pubby provides the normalization, the matching, and the enforcement seams
in the inbox and outbox processors.
"""

from typing import Collection, Optional
from urllib.parse import urlparse


def normalize_domain(value: str) -> str:
    """
    Normalize a domain or URL for allow/block comparisons.

    Strips the scheme, path, port and credentials, then lower-cases the
    hostname.  Returns ``""`` for empty or unparseable input.

    :param value: A bare domain (``example.com``), a host with port
        (``example.com:8080``), or a full URL
        (``https://example.com/users/alice``).
    :return: The normalized hostname, or ``""``.
    """
    if not value:
        return ""

    candidate = value.strip().lower()
    if not candidate:
        return ""

    # ``urlparse`` only treats the leading component as a netloc (host) when
    # a scheme or a ``//`` prefix is present, so prefix bare hosts.
    if "://" not in candidate:
        candidate = "//" + candidate

    try:
        host = urlparse(candidate).hostname or ""
    except ValueError:
        return ""

    return host.strip()


def extract_domain(url_or_actor: str) -> str:
    """
    Extract the normalized domain from an actor URL, inbox URL, or bare domain.

    :param url_or_actor: An actor URL (``https://example.com/users/alice``),
        an inbox URL, or a bare domain.
    :return: The normalized hostname, or ``""``.
    """
    return normalize_domain(url_or_actor)


def is_domain_blocked(
    domain: str,
    allowed: Optional[Collection[str]] = None,
    blocked: Optional[Collection[str]] = None,
) -> bool:
    """
    Return ``True`` when ``domain`` is blocked or excluded by an allow-list.

    - An empty or absent allow-list means "allow all" (except explicit
      blocks).
    - Blocked domains take precedence over allowed domains.
    - Comparisons are case-insensitive and ignore URL schemes, paths and
      ports on both sides.

    :param domain: Domain, actor URL or inbox URL to check.
    :param allowed: Optional allow-list of domains.  When non-empty, only
        these domains are permitted.
    :param blocked: Optional block-list of domains.
    :return: ``True`` if the domain must be rejected.
    """
    if not allowed and not blocked:
        return False

    normalized = normalize_domain(domain)
    if not normalized:
        return False

    if normalized in {normalize_domain(d) for d in blocked or ()}:
        return True

    allowed_set = {normalize_domain(d) for d in allowed or ()}
    return bool(allowed_set) and normalized not in allowed_set
