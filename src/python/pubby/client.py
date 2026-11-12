from __future__ import annotations

import logging
from typing import Collection
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives.asymmetric import rsa
from requests import ConnectionError, HTTPError

from .crypto import (
    Ed25519Identity,
    SignatureScheme,
    load_private_key,
    sign_request,
    sign_request_rfc9421,
    signing_identity,
)
from .handlers._client import get_default_user_agent
from ._http import guarded_get, is_safe_outbound_url
from .moderation import is_domain_blocked
from .storage import ActivityPubStorage

logger = logging.getLogger(__name__)


def extract_actor_inbox(actor_data: dict) -> str | None:
    """
    Return the preferred inbox URL from an actor document.

    Prefers ``endpoints.sharedInbox`` over ``inbox``.

    :param actor_data: The actor's JSON-LD document.
    :return: The inbox URL, or ``None`` if no inbox is present.
    """
    if not isinstance(actor_data, dict):
        return None

    endpoints = actor_data.get("endpoints") or {}
    if not isinstance(endpoints, dict):
        endpoints = {}

    shared_inbox = endpoints.get("sharedInbox")
    if shared_inbox:
        return shared_inbox

    return actor_data.get("inbox") or None


def _load_private_key(
    private_key: rsa.RSAPrivateKey | str | bytes | None,
) -> rsa.RSAPrivateKey | None:
    """Normalize a PEM string/bytes or key object into an RSAPrivateKey."""
    if private_key is None:
        return None
    if isinstance(private_key, rsa.RSAPrivateKey):
        return private_key
    if isinstance(private_key, (str, bytes)):
        return load_private_key(private_key)
    return None


def resolve_actor_inbox(
    actor_url: str,
    storage: ActivityPubStorage,
    *,
    private_key: rsa.RSAPrivateKey | str | bytes | None = None,
    key_id: str | None = None,
    allowed_instances: Collection[str] | None = None,
    blocked_instances: Collection[str] | None = None,
    user_agent: str | None = None,
    timeout: float = 10.0,
    allow_private_targets: bool = False,
    signature_scheme: SignatureScheme | str = SignatureScheme.DRAFT_CAVAGE,
    ed25519: Ed25519Identity | None = None,
) -> str | None:
    """
    Resolve a remote actor's inbox, using the cache and an optional signed fetch.

    - Returns ``None`` for non-HTTP(S) actor IDs.
    - Applies allow/block domain filtering before any network call.
    - Consults ``storage.get_cached_actor(actor_url)`` first.
    - Fetches the actor document with ``Accept: application/activity+json,
      application/ld+json`` on cache miss.
    - Signs the GET when both ``private_key`` and ``key_id`` are provided.
    - Caches successful fetches via ``storage.cache_remote_actor``.
    - Returns ``endpoints.sharedInbox`` when available, otherwise ``inbox``.

    :param actor_url: The remote actor URL/ID.
    :param storage: An ``ActivityPubStorage`` backend (used for actor cache).
    :param private_key: RSA private key, PEM string/bytes, or ``None`` for an
        unsigned fetch.
    :param key_id: Key ID for the HTTP signature (typically
        ``<actor_id>#main-key``).
    :param allowed_instances: Optional allow-list of remote instance domains.
    :param blocked_instances: Optional block-list of remote instance domains.
    :param user_agent: Optional ``User-Agent`` header; defaults to
        ``pubby/{version} (+{actor_url})``.
    :param timeout: HTTP request timeout in seconds.
    :param signature_scheme: Wire format of the signed actor fetch —
        ``draft-cavage`` (default) or ``rfc9421``.
    :param ed25519: Optional Ed25519 identity used for the signed fetch
        under ``rfc9421``.
    :return: The resolved inbox URL, or ``None`` if the actor is blocked,
        unreachable, unparseable, or has no inbox.
    """
    parsed = urlparse(actor_url)
    if parsed.scheme not in ("http", "https"):
        logger.debug("Refusing to resolve non-HTTP(S) actor ID: %s", actor_url)
        return None
    if not is_safe_outbound_url(actor_url) and not allow_private_targets:
        logger.debug("Refusing unsafe outbound URL: %s", actor_url)
        return None

    if is_domain_blocked(
        actor_url,
        allowed=allowed_instances,
        blocked=blocked_instances,
    ):
        logger.debug(
            "Skipping actor on blocked/non-allowed instance: %s",
            actor_url,
        )
        return None

    cached = storage.get_cached_actor(actor_url)
    if cached is not None:
        return extract_actor_inbox(cached)

    try:
        headers = {
            "Accept": "application/activity+json, application/ld+json",
            "User-Agent": user_agent or get_default_user_agent(actor_url),
        }

        if private_key is not None and key_id is not None:
            key = _load_private_key(private_key)
            if key is not None:
                signer = (
                    sign_request_rfc9421
                    if SignatureScheme(signature_scheme) is SignatureScheme.RFC9421
                    else sign_request
                )
                signing_key, signing_key_id = signing_identity(
                    signature_scheme, key, key_id, ed25519
                )
                signed = signer(
                    private_key=signing_key,
                    key_id=signing_key_id,
                    method="GET",
                    url=actor_url,
                    headers=headers,
                )
                headers = signed

        resp = guarded_get(
            actor_url,
            headers=headers,
            timeout=timeout,
            allow_private_targets=allow_private_targets,
            session=requests,
        )
        resp.raise_for_status()
        actor_data = resp.json()
        storage.cache_remote_actor(actor_url, actor_data)
        return extract_actor_inbox(actor_data)
    except HTTPError as e:
        if e.response is not None and e.response.status_code == 410:
            logger.debug("Actor gone (deleted): %s", actor_url)
        else:
            logger.warning("Failed to fetch actor %s: %s", actor_url, e)
        return None
    except ConnectionError as e:
        logger.warning("Failed to connect to actor %s: %s", actor_url, e)
        return None
    except Exception as e:
        logger.warning("Failed to resolve actor inbox %s: %s", actor_url, e)
        return None
