"""
AP-to-Mastodon entity converters.

Maps ActivityPub actors, objects, and activities to the Mastodon API
JSON shapes (Account, Status, Tag, etc.).
"""

import base64
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from ...handlers import ActivityPubHandler
from ..._model import Follower, InteractionType


# -- Stable ID helpers -------------------------------------------------------


def stable_id(url: str) -> str:
    """
    Derive a URL-safe, deterministic, reversible identifier from a URL.

    Uses URL-safe base64 encoding (without padding) so that the original
    URL can be recovered via :func:`id_to_url`.
    """
    return base64.urlsafe_b64encode(url.encode()).rstrip(b"=").decode()


def id_to_url(encoded: str) -> str:
    """Reverse a :func:`stable_id` encoding back to the original URL."""
    padded = encoded + "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(padded.encode()).decode()


# -- Account mapper -----------------------------------------------------------

_ACCOUNT_LOCAL_ID = "1"


def actor_to_account(handler: ActivityPubHandler) -> dict[str, Any]:
    """
    Map the local actor to a Mastodon Account entity.

    :param handler: The ActivityPubHandler instance.
    :return: A Mastodon-compatible Account dictionary.
    """
    followers = handler.storage.get_followers()
    activities = handler.storage.get_activities(limit=10000, offset=0)

    fields: list[dict[str, Any]] = []
    for att in handler.actor_attachment or []:
        if att.get("type") == "PropertyValue":
            fields.append(
                {
                    "name": att.get("name", ""),
                    "value": att.get("value", ""),
                    "verified_at": None,
                }
            )

    avatar = handler.icon_url or ""

    return {
        "id": _ACCOUNT_LOCAL_ID,
        "username": handler.username,
        "acct": f"{handler.username}@{handler.webfinger_domain}",
        "display_name": handler.actor_name or handler.username,
        "note": handler.actor_summary or "",
        "url": handler.actor_url or handler.base_url,
        "uri": handler.actor_id,
        "avatar": avatar,
        "avatar_static": avatar,
        "header": "",
        "header_static": "",
        "locked": handler.manually_approves,
        "created_at": "1970-01-01T00:00:00.000Z",
        "last_status_at": None,
        "followers_count": len(followers),
        "following_count": 0,
        "statuses_count": len(activities),
        "fields": fields,
        "emojis": [],
        "bot": False,
        "group": False,
        "discoverable": True,
        "noindex": False,
        "source": None,
    }


# -- Status mapper ------------------------------------------------------------


def _parse_published(value: object) -> str:
    """Return an ISO-8601 string from various input formats."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value.strip():
        return value
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _extract_object(activity: dict) -> dict:
    """Extract the inner object from a Create/Update activity."""
    obj = activity.get("object", {})
    if isinstance(obj, str):
        return {"id": obj}
    return obj if isinstance(obj, dict) else {}


def _map_media_attachments(attachments: list[dict]) -> list[dict[str, Any]]:
    """Map AP attachments to Mastodon MediaAttachment entities."""
    result: list[dict[str, Any]] = []
    for att in attachments or []:
        media_type = att.get("mediaType", "")
        att_type = "unknown"
        if media_type.startswith("image/"):
            att_type = "image"
        elif media_type.startswith("video/"):
            att_type = "video"
        elif media_type.startswith("audio/"):
            att_type = "audio"

        result.append(
            {
                "id": stable_id(att.get("url", att.get("href", ""))),
                "type": att_type,
                "url": att.get("url", att.get("href", "")),
                "preview_url": att.get("url", att.get("href", "")),
                "remote_url": None,
                "meta": None,
                "description": att.get("name", None),
                "blurhash": None,
            }
        )
    return result


def _map_tags(tags: list[dict], base_url: str) -> list[dict[str, Any]]:
    """Map AP tag objects to Mastodon Tag entities."""
    result: list[dict[str, Any]] = []
    for tag in tags or []:
        tag_type = tag.get("type", "")
        if tag_type == "Hashtag":
            name = tag.get("name", "").lstrip("#")
            result.append(
                {
                    "name": name,
                    "url": tag.get("href", f"{base_url}/tags/{name}"),
                }
            )
        elif tag_type == "Mention":
            result.append(
                {
                    "id": tag.get("href", ""),
                    "username": tag.get("name", "").lstrip("@"),
                    "url": tag.get("href", ""),
                    "acct": tag.get("name", "").lstrip("@"),
                }
            )
    return result


def _count_interactions(
    handler: ActivityPubHandler,
    object_url: str,
    interaction_type: InteractionType,
) -> int:
    """Count stored interactions of a given type for an object."""
    try:
        interactions = handler.storage.get_interactions(
            target_resource=object_url,
            interaction_type=interaction_type,
        )
        return len(interactions)
    except Exception:
        return 0


def activity_to_status(
    activity: dict,
    handler: ActivityPubHandler,
    account: dict | None = None,
) -> dict[str, Any]:
    """
    Map an outbox activity (Create wrapping a Note/Article) to a Mastodon
    Status entity.

    :param activity: The activity dictionary from storage.
    :param handler: The ActivityPubHandler instance.
    :param account: Pre-built Account dict for the local actor (avoids
        repeated DB queries). If *None*, one is built on the fly.
    :return: A Mastodon-compatible Status dictionary.
    """
    if account is None:
        account = actor_to_account(handler)

    obj = _extract_object(activity)
    object_id = obj.get("id", activity.get("id", ""))
    object_url = obj.get("url", object_id)

    content = obj.get("content", "")
    language = None
    content_map = obj.get("contentMap")
    if isinstance(content_map, dict) and content_map:
        language = next(iter(content_map))

    tags = _map_tags(obj.get("tag", []), handler.base_url)
    media = _map_media_attachments(obj.get("attachment", []))

    return {
        "id": stable_id(object_id),
        "created_at": _parse_published(obj.get("published", activity.get("published"))),
        "in_reply_to_id": None,
        "in_reply_to_account_id": None,
        "sensitive": obj.get("sensitive", False),
        "spoiler_text": obj.get("summary") or "",
        "visibility": "public",
        "language": language,
        "uri": object_id,
        "url": object_url,
        "replies_count": _count_interactions(
            handler, object_url, InteractionType.REPLY
        ),
        "reblogs_count": _count_interactions(
            handler, object_url, InteractionType.BOOST
        ),
        "favourites_count": _count_interactions(
            handler, object_url, InteractionType.LIKE
        ),
        "favourited": False,
        "reblogged": False,
        "muted": False,
        "bookmarked": False,
        "pinned": False,
        "content": content,
        "reblog": None,
        "application": {
            "name": handler.software_name,
            "website": handler.base_url,
        },
        "account": account,
        "media_attachments": media,
        "mentions": [],
        "tags": [t for t in tags if "name" in t and "url" in t and "id" not in t],
        "emojis": [],
        "card": None,
        "poll": None,
    }


# -- Follower → Account mapper -----------------------------------------------


def follower_to_account(follower: Follower) -> dict[str, Any]:
    """
    Map a stored Follower to a minimal Mastodon Account entity.

    Uses cached ``actor_data`` when available, otherwise falls back to
    the follower's ``actor_id``.

    :param follower: A Follower record.
    :return: A Mastodon-compatible Account dictionary.
    """
    data = follower.actor_data or {}
    actor_url = data.get("url", follower.actor_id)
    username = data.get("preferredUsername", "")
    name = data.get("name", username)
    icon = data.get("icon", {})
    avatar = ""
    if isinstance(icon, dict):
        avatar = icon.get("url", "")
    elif isinstance(icon, str):
        avatar = icon

    parsed = urlparse(follower.actor_id)
    domain = parsed.hostname or ""

    return {
        "id": stable_id(follower.actor_id),
        "username": username,
        "acct": f"{username}@{domain}" if username else follower.actor_id,
        "display_name": name,
        "note": data.get("summary", ""),
        "url": actor_url,
        "uri": follower.actor_id,
        "avatar": avatar,
        "avatar_static": avatar,
        "header": "",
        "header_static": "",
        "locked": data.get("manuallyApprovesFollowers", False),
        "created_at": "1970-01-01T00:00:00.000Z",
        "last_status_at": None,
        "followers_count": 0,
        "following_count": 0,
        "statuses_count": 0,
        "fields": [],
        "emojis": [],
        "bot": False,
        "group": False,
        "discoverable": data.get("discoverable", True),
        "noindex": False,
    }


# -- Tag mapper ---------------------------------------------------------------


def tag_to_mastodon_tag(
    name: str,
    base_url: str,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Build a Mastodon Tag entity.

    :param name: The tag name (without ``#``).
    :param base_url: The base URL of the instance.
    :param history: Optional usage history list.
    :return: A Mastodon-compatible Tag dictionary.
    """
    return {
        "name": name.lower(),
        "url": f"{base_url}/tags/{name.lower()}",
        "history": history or [],
    }
