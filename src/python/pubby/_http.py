"""
Outbound HTTP safety for federation fetches — mirrors the Rust SDK's
``http::is_safe_outbound_url``.
"""

import ipaddress
from urllib.parse import urlparse

_MAX_REDIRECTS = 5

_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan")


def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    if not ip.is_global:
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        # Rust additionally rejects 0.0.0.0/8 and CGNAT 100.64.0.0/10
        # (is_global excludes them already) — kept for parity.
        pass
    return True


def is_safe_outbound_url(url: str) -> bool:
    """
    Whether *url* is a safe outbound federation target.

    Requires an ``http``/``https`` scheme and rejects literal hosts that
    are loopback, link-local, private, CGNAT, multicast, unspecified or
    well-known internal hostnames (``localhost``, ``*.local``,
    ``*.internal``, ``*.localhost``, the cloud metadata address).
    DNS-level rebinding is out of scope.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        return _ip_is_public(ipaddress.ip_address(host))
    except ValueError:
        pass
    d = host.lower()
    return not (
        d == "localhost"
        or d == "metadata.google.internal"
        or d.endswith(_BLOCKED_SUFFIXES)
    )


def guarded_get(
    url, *, headers, timeout, allow_private_targets=False, session=None, **kwargs
):
    """
    GET *url*, following redirects manually with per-hop
    :func:`is_safe_outbound_url` validation (mirrors the Rust
    ``HttpClient::get``). ``allow_private_targets`` clears only the
    *initial* check when the target is private — a public target still
    gets per-hop validation.

    Extra keyword arguments are forwarded to ``session.get`` (e.g.
    ``params=``).

    The returned response's ``url`` attribute is the *effective* URL —
    the last redirect hop, or the request URL when no redirect was
    followed. Fetchers that verify an object's ``id`` against the
    origin that served it should compare against ``resp.url``: a
    server may legitimately redirect a web permalink to its canonical
    ActivityPub domain.
    """
    import requests  # local import keeps module cheap

    sess = session or requests
    safe = is_safe_outbound_url(url)
    if not safe and not allow_private_targets:
        from ._exceptions import ActivityPubError

        raise ActivityPubError(f"refusing unsafe outbound URL: {url}")
    check_hops = not allow_private_targets or safe
    for _hop in range(_MAX_REDIRECTS + 1):
        resp = sess.get(
            url,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
            **kwargs,
        )
        # Status-range check (Rust parity) — `is_redirect` misses
        # 307/308 and fails on non-requests duck-typed responses.
        code = resp.status_code if isinstance(resp.status_code, int) else 0
        if 300 <= code < 400:
            loc = resp.headers.get("location")
            if not loc:
                return resp
            from urllib.parse import urljoin

            nxt = urljoin(url, loc)
            if check_hops and not is_safe_outbound_url(nxt):
                import logging

                logging.getLogger(__name__).warning(
                    "Refusing redirect to unsafe URL: %s", nxt
                )
                return resp
            url = nxt
            continue
        return resp
    return resp


def guarded_post(
    url, *, headers, body, timeout, allow_private_targets=False, session=None
):
    """POST *url* — redirects are not followed (mirrors the Rust client)."""
    import requests

    if not is_safe_outbound_url(url) and not allow_private_targets:
        from ._exceptions import ActivityPubError

        raise ActivityPubError(f"refusing unsafe outbound URL: {url}")
    sess = session or requests
    return sess.post(url, headers=headers, data=body, timeout=timeout)
