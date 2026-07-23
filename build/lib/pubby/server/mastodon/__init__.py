"""
Mastodon-compatible API layer.

Provides framework-agnostic mappers and route handlers that expose
a read-only subset of the Mastodon REST API backed by Pubby's
ActivityPub handler and storage.
"""

from ._mappers import (
    actor_to_account,
    activity_to_status,
    follower_to_account,
    stable_id,
    tag_to_mastodon_tag,
)
from ._routes import MastodonAPI

__all__ = [
    "MastodonAPI",
    "actor_to_account",
    "activity_to_status",
    "follower_to_account",
    "stable_id",
    "tag_to_mastodon_tag",
]
