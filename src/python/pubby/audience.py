"""
Audience helpers — parse ActivityPub addressing fields.

Stdlib-only helpers for reading the audience (``to``/``cc``/``bto``/``bcc``)
and ``Mention`` tags of an ActivityStreams object or activity. The helpers
are pure: they inspect only the mapping supplied by the caller, never
descend into an activity's embedded ``object``, and never raise on
malformed values.
"""

from typing import Any, Dict, List, Set

from ._model import AS_PUBLIC

#: Recognized identifiers for the ActivityStreams public audience. The
#: canonical URI is defined in the spec; ``Public`` and ``as:Public`` are
#: common shorthand aliases emitted by some implementations.
PUBLIC_URIS = frozenset({AS_PUBLIC, "Public", "as:Public"})

#: Audience fields inspected by :func:`addressees`.
AUDIENCE_FIELDS = ("to", "cc", "bto", "bcc")


def addressees(obj_or_activity: Dict[str, Any]) -> Set[str]:
    """
    Return the union of string values in ``to``, ``cc``, ``bto`` and ``bcc``.

    Each field may be a string, a list of strings, or absent; non-string
    collection members and non-string/non-list field values are ignored.

    Only the supplied mapping is inspected — the function does not look
    inside an activity's embedded ``object``. Call it once per container
    (activity and object) and combine the results if both scopes matter.

    :param obj_or_activity: An ActivityStreams object or activity mapping.
    :return: The set of addressee URLs (unordered).
    """
    urls: Set[str] = set()
    if not isinstance(obj_or_activity, dict):
        return urls
    for key in AUDIENCE_FIELDS:
        value = obj_or_activity.get(key)
        if isinstance(value, str):
            urls.add(value)
        elif isinstance(value, list):
            urls.update(v for v in value if isinstance(v, str))
    return urls


def is_public(obj_or_activity: Dict[str, Any]) -> bool:
    """
    Return whether the mapping addresses the public collection.

    ``True`` when any of :data:`PUBLIC_URIS` appears in the mapping's
    audience fields. Only the supplied mapping is inspected.

    :param obj_or_activity: An ActivityStreams object or activity mapping.
    :return: ``True`` if publicly addressed.
    """
    return bool(PUBLIC_URIS.intersection(addressees(obj_or_activity)))


def mentioned_actors(obj_data: Dict[str, Any]) -> List[str]:
    """
    Return actor URLs from the mapping's ``Mention`` tags.

    Entries of the form ``{"type": "Mention", "href": "<url>"}`` in the
    ``tag`` collection contribute their ``href``; malformed entries,
    non-``Mention`` tags and empty ``href`` values are skipped. URLs are
    returned in first-seen order with duplicates removed.

    :param obj_data: An ActivityStreams object mapping.
    :return: Ordered, deduplicated list of mentioned actor URLs.
    """
    mentioned: List[str] = []
    seen: Set[str] = set()
    tags = obj_data.get("tag") if isinstance(obj_data, dict) else None
    if isinstance(tags, list):
        for tag in tags:
            if not isinstance(tag, dict) or tag.get("type") != "Mention":
                continue
            href = tag.get("href")
            if isinstance(href, str) and href and href not in seen:
                seen.add(href)
                mentioned.append(href)
    return mentioned
