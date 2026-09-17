from abc import ABC, abstractmethod
from collections.abc import Collection
from datetime import datetime
from typing import Any

from .._model import (
    Follower,
    FollowRequest,
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

        The caller should set ``Follower.target_actor_id`` to the local
        resource URL being followed — an actor URL for actor follows, or
        a local object id for object follows (thread subscriptions). If
        left empty, the follower is treated as unassigned and may be
        returned for any actor.

        :param follower: The Follower to store.
        """

    @abstractmethod
    def remove_follower(
        self,
        actor_id: str,
        target_actor_id: str = "",
    ) -> Any:
        """
        Remove a follower by their actor ID.

        :param actor_id: The actor ID of the follower to remove.
        :param target_actor_id: Optional local actor URL to scope the
            removal. If provided, only the follower of that actor is removed.
            When omitted, all follow records from this remote actor are
            removed. In multi-actor setups this can be unintentionally
            destructive; callers should always pass ``target_actor_id``
            unless they intend to remove every follow from this actor.
        """

    @abstractmethod
    def get_followers(
        self,
        actor_id: str | None = None,
    ) -> list[Follower]:
        """
        Retrieve stored followers.

        :param actor_id: If provided, only return followers of this actor
            or object (plus any unassigned followers with an empty
            ``target_actor_id``). Implementations should include
            unassigned followers for backward compatibility until the
            application backfills their ``target_actor_id``.
            If None, return all followers.
        :return: A list of Follower records.
        """

    def get_followers_of_targets(
        self,
        target_ids: Collection[str],
    ) -> list[Follower]:
        """
        Retrieve followers of any of the given local targets.

        ``target_ids`` may mix actor URLs and object ids — a ``Follow``
        may legally target any local object (thread subscriptions), and
        the application can use this to fan out thread updates to the
        subscribers of every object in a reply chain. Unassigned
        followers (empty ``target_actor_id``) are not included.

        :param target_ids: Local actor URLs or object ids.
        :return: A list of Follower records.
        """
        wanted = set(target_ids)
        return [f for f in self.get_followers() if f.target_actor_id in wanted]

    # ---------- Follow requests (pending approvals) ----------

    def store_follow_request(self, request: FollowRequest) -> Any:
        """
        Store or update a pending follow request.

        Requests are keyed by ``(actor_id, target_actor_id)``: a repeated
        ``Follow`` from the same actor refreshes the stored record.

        :param request: The FollowRequest to store.
        :raises NotImplementedError: When the backend does not support
            pending requests; callers should fall back to auto-accepting.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support follow requests"
        )

    def get_follow_requests(
        self,
        target_actor_id: str | None = None,
    ) -> list[FollowRequest]:
        """
        Retrieve pending follow requests.

        :param target_actor_id: If provided, only return requests targeting
            this local actor or object. If None, return all requests.
        :return: A list of FollowRequest records.
        """
        return []

    def get_follow_request(
        self,
        actor_id: str,
        target_actor_id: str = "",
    ) -> FollowRequest | None:
        """
        Retrieve the pending request from ``actor_id`` for ``target_actor_id``.

        :param actor_id: The remote actor that sent the ``Follow``.
        :param target_actor_id: The local actor or object URL being followed.
        :return: The FollowRequest, or None if no request is pending.
        """
        for request in self.get_follow_requests(target_actor_id):
            if request.actor_id == actor_id:
                return request
        return None

    def remove_follow_request(
        self,
        actor_id: str,
        target_actor_id: str = "",
    ) -> bool:
        """
        Remove a pending follow request.

        :param actor_id: The remote actor whose request is removed.
        :param target_actor_id: Scope the removal to this target. When
            empty, all requests from this remote actor are removed.
        :return: True if at least one request was removed.
        """
        return False

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
        source_actor_id: str,  # type: ignore
        object_id: str,  # type: ignore
    ) -> bool:
        """
        Delete an interaction by its ``object_id`` (the remote object URL).

        Searches all stored interactions from *source_actor_id* and marks
        matching ones as deleted. Returns ``True`` if at least one was found.

        Subclasses may override for a more efficient implementation.
        """
        return False

    def get_interaction_by_object_id(
        self,
        object_id: str,  # type: ignore
        status: InteractionStatus = InteractionStatus.CONFIRMED,  # type: ignore
    ) -> Interaction | None:
        """
        Retrieve an interaction by its ``object_id`` (the remote object URL).

        This is useful when you need to look up an interaction without knowing
        its target resource, e.g., to find who sent a particular reply.

        :param object_id: The remote object URL (e.g., a Mastodon status URL).
        :param status: Filter by status (default: CONFIRMED).
        :return: The matching Interaction, or None if not found.
        """
        return None

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
