from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from .._model import (
    Follower,
    Interaction,
    InteractionStatus,
    InteractionType,
)


class ActivityPubStorage(ABC):
    """
    Abstract base class for ActivityPub storage backends.
    """

    # ---------- Followers ----------

    @abstractmethod
    def store_follower(self, follower: Follower) -> Any:
        """
        Store or update a follower record.

        :param follower: The Follower to store.
        """

    @abstractmethod
    def remove_follower(self, actor_id: str) -> Any:
        """
        Remove a follower by their actor ID.

        :param actor_id: The actor ID of the follower to remove.
        """

    @abstractmethod
    def get_followers(self) -> list[Follower]:
        """
        Retrieve all stored followers.

        :return: A list of Follower records.
        """

    # ---------- Interactions ----------

    @abstractmethod
    def store_interaction(self, interaction: Interaction) -> Any:
        """
        Store or update an interaction.

        :param interaction: The Interaction to store.
        """

    @abstractmethod
    def delete_interaction(
        self,
        source_actor_id: str,
        target_resource: str,
        interaction_type: InteractionType,
    ) -> Any:
        """
        Mark an interaction as deleted.

        :param source_actor_id: The actor ID of the interaction source.
        :param target_resource: The target resource URL.
        :param interaction_type: The type of interaction.
        """

    def delete_interaction_by_object_id(
        self,
        source_actor_id: str,
        object_id: str,
    ) -> bool:
        """
        Delete an interaction by its ``object_id`` (the remote object URL).

        Searches all stored interactions from *source_actor_id* and marks
        matching ones as deleted. Returns ``True`` if at least one was found.

        Subclasses may override for a more efficient implementation.
        """
        return False

    @abstractmethod
    def get_interactions(
        self,
        target_resource: str,
        interaction_type: InteractionType | None = None,
        status: InteractionStatus = InteractionStatus.CONFIRMED,
    ) -> list[Interaction]:
        """
        Retrieve interactions for a resource.

        :param target_resource: The target resource URL.
        :param interaction_type: Optional filter by interaction type.
        :param status: Filter by status (default: CONFIRMED).
        :return: A list of Interaction records.
        """

    def get_interactions_mentioning(
        self,
        actor_url: str,
        interaction_type: InteractionType | None = None,
        status: InteractionStatus = InteractionStatus.CONFIRMED,
    ) -> list[Interaction]:
        """
        Retrieve interactions that mention a given actor URL.

        This uses the ``mentioned_actors`` field populated at write time
        from the ActivityPub object's ``tag`` array (Mention tags).

        :param actor_url: The actor URL to search for in mentions.
        :param interaction_type: Optional filter by interaction type.
        :param status: Filter by status (default: CONFIRMED).
        :return: A list of Interaction records mentioning the actor.
        """
        return []

    # ---------- Activities (outbox records) ----------

    @abstractmethod
    def store_activity(self, activity_id: str, activity_data: dict) -> Any:
        """
        Store an outbound activity record.

        :param activity_id: The activity's unique ID.
        :param activity_data: The full activity JSON-LD dictionary.
        """

    @abstractmethod
    def get_activities(
        self,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        """
        Retrieve outbound activities (for the outbox collection).

        :param limit: Maximum number of activities to return.
        :param offset: Offset for pagination.
        :return: A list of activity dictionaries, newest first.
        """

    # ---------- Actor cache ----------

    @abstractmethod
    def cache_remote_actor(
        self,
        actor_id: str,
        actor_data: dict,
        fetched_at: datetime | None = None,
    ) -> Any:
        """
        Cache a fetched remote actor's data.

        :param actor_id: The actor's ID (URL).
        :param actor_data: The actor's JSON-LD document.
        :param fetched_at: When the actor was fetched (defaults to now).
        """

    @abstractmethod
    def get_cached_actor(
        self,
        actor_id: str,
        max_age_seconds: float = 86400.0,
    ) -> dict | None:
        """
        Retrieve a cached remote actor if still fresh.

        :param actor_id: The actor's ID (URL).
        :param max_age_seconds: Maximum age in seconds before the cache
            entry is considered stale (default 24h).
        :return: The actor's JSON-LD document, or None if not cached or stale.
        """

    # ---------- Quote authorizations ----------

    def store_quote_authorization(
        self,
        authorization_id: str,
        authorization_data: dict,
    ) -> Any:
        """
        Store a QuoteAuthorization object so it can be served via HTTP GET.

        :param authorization_id: The full URL / ID of the authorization.
        :param authorization_data: The JSON-LD document.
        """

    def get_quote_authorization(self, authorization_id: str) -> dict | None:
        """
        Retrieve a stored QuoteAuthorization by its ID.

        :param authorization_id: The full URL / ID of the authorization.
        :return: The JSON-LD document, or None if not found.
        """
        return None
