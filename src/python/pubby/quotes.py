"""
Quote helpers — FEP-0449 quote fields and FEP-044f interaction policies.

Stdlib-only helpers for reading and stamping the quote fields of an
ActivityStreams object.  ``quote`` is the FEP-0449 field; ``quoteUri``
and ``quoteUrl`` are emitted by Fedibird, Akkoma and Mastodon;
``_misskey_quote`` is Misskey's variant.  Readers should accept all
four spellings (:func:`extract_quote_target`); writers should emit all
of them (:func:`set_quote_target`) so every compatible server
recognizes the quote.
"""

import copy
from typing import Any, Dict, List, Optional

from ._model import AS_PUBLIC

#: Quote field spellings recognized on inbound objects, in precedence
#: order — the FEP-0449 ``quote`` field wins when several are present.
QUOTE_FIELD_KEYS = ("quote", "quoteUri", "quoteUrl", "_misskey_quote")

#: FEP-044f extension terms for ``QuoteRequest`` and
#: ``QuoteAuthorization`` documents.
FEP_044F_TERMS: Dict[str, Any] = {
    "QuoteAuthorization": "https://w3id.org/fep/044f#QuoteAuthorization",
    "QuoteRequest": "https://w3id.org/fep/044f#QuoteRequest",
    "gts": "https://gotosocial.org/ns#",
    "interactingObject": {
        "@id": "gts:interactingObject",
        "@type": "@id",
    },
    "interactionTarget": {
        "@id": "gts:interactionTarget",
        "@type": "@id",
    },
}

#: JSON-LD ``@context`` for FEP-044f payloads (``QuoteRequest``
#: activities, ``QuoteAuthorization`` documents and their ``Accept``).
FEP_044F_CONTEXT: List[Any] = [
    "https://www.w3.org/ns/activitystreams",
    FEP_044F_TERMS,
]

#: FEP-044f ``interactionPolicy`` value allowing anyone to quote without
#: manual approval.  Mastodon reads ``canQuote`` to decide whether its
#: Quote action is enabled and whether an incoming quote needs a
#: ``QuoteAuthorization`` stamp to leave its pending state.
PUBLIC_QUOTE_POLICY: Dict[str, Any] = {
    "canQuote": {
        "automaticApproval": [AS_PUBLIC],
        "manualApproval": [],
    }
}


def extract_quote_target(obj_data: Dict[str, Any]) -> Optional[str]:
    """
    Extract the quoted object URL from an object, if present.

    Checks every spelling in :data:`QUOTE_FIELD_KEYS` — the FEP-0449
    ``quote`` field, the ``quoteUri``/``quoteUrl`` forms Fedibird,
    Akkoma and Mastodon emit, and Misskey's ``_misskey_quote``.

    :param obj_data: An ActivityStreams object mapping.
    :return: The quoted object URL, or ``None`` when absent.
    """
    for key in QUOTE_FIELD_KEYS:
        value = obj_data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def set_quote_target(obj: Dict[str, Any], quoted_uri: str) -> Dict[str, Any]:
    """
    Stamp ``quoted_uri`` on ``obj`` under every recognized quote field.

    Emits all spellings in :data:`QUOTE_FIELD_KEYS` so FEP-0449,
    Mastodon, Fedibird, Akkoma and Misskey readers all recognize the
    quote.  Mutates and returns ``obj`` for chaining.

    :param obj: The object document to mark as a quote.
    :param quoted_uri: The URL of the object being quoted.
    :return: ``obj``.
    """
    for key in QUOTE_FIELD_KEYS:
        obj[key] = quoted_uri
    return obj


def allow_public_quotes(obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Stamp the public-quote ``interactionPolicy`` on an object document.

    Sets ``interactionPolicy`` to a deep copy of
    :data:`PUBLIC_QUOTE_POLICY` — quoting allowed for everyone, without
    manual approval — so callers may keep mutating ``obj`` without
    aliasing the shared constant.  Returns ``obj`` for chaining.

    :param obj: The object document to stamp.
    :return: ``obj``.
    """
    obj["interactionPolicy"] = copy.deepcopy(PUBLIC_QUOTE_POLICY)
    return obj


__all__ = [
    "FEP_044F_CONTEXT",
    "FEP_044F_TERMS",
    "PUBLIC_QUOTE_POLICY",
    "QUOTE_FIELD_KEYS",
    "allow_public_quotes",
    "extract_quote_target",
    "set_quote_target",
]
