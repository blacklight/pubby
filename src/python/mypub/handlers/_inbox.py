"""
Inbox processing — dispatch incoming activities by type.
"""

import logging
from datetime import datetime, timezone
from typing import Callable

import requests

from .._model import (
    Activity,
    ActivityType,
    Actor,
    Follower,
    Interaction,
    InteractionStatus,
    InteractionType,
    Object,
    AP_CONTEXT,
)
from .._exceptions import SignatureVerificationError
from ..crypto import sign_request, verify_request
from ..crypto._keys import load_public_key
from ..storage import ActivityPubStorage

logger = logging.getLogger(__name__)


class InboxProcessor:
    """
    Processes incoming ActivityPub activities.

    Dispatches by activity type and performs HTTP signature verification.

    :param storage: Storage backend.
    :param actor_id: This server's actor ID (URL).
    :param private_key: RSA private key for signing outgoing Accept responses.
    :param key_id: Key ID for HTTP signatures (e.g. ``actor_id#main-key``).
    :param on_interaction_received: Optional callback when an interaction is stored.
    :param user_agent: User-Agent for outgoing HTTP requests.
    :param http_timeout: Timeout for outgoing HTTP requests.
    """

    def __init__(
        self,
        storage: ActivityPubStorage,
        actor_id: str,
        private_key: object,
        key_id: str,
        *,
        on_interaction_received: Callable[[Interaction], None] | None = None,
        user_agent: str = "mypub/0.0.1",
        http_timeout: float = 15.0,
    ):
        self.storage = storage
        self.actor_id = actor_id
        self.private_key = private_key
        self.key_id = key_id
        self.on_interaction_received = on_interaction_received
        self.user_agent = user_agent
        self.http_timeout = http_timeout

    def _fetch_actor(self, actor_id: str) -> dict | None:
        """Fetch a remote actor document, using cache if available."""
        cached = self.storage.get_cached_actor(actor_id)
        if cached is not None:
            return cached

        try:
            resp = requests.get(
                actor_id,
                headers={
                    "Accept": "application/activity+json, application/ld+json",
                    "User-Agent": self.user_agent,
                },
                timeout=self.http_timeout,
            )
            resp.raise_for_status()
            actor_data = resp.json()
            self.storage.cache_remote_actor(actor_id, actor_data)
            return actor_data
        except Exception:
            logger.warning("Failed to fetch actor %s", actor_id, exc_info=True)
            return None

    def verify_signature(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> str:
        """
        Verify the HTTP signature on an incoming request.

        :param method: HTTP method.
        :param path: Request path.
        :param headers: Request headers.
        :param body: Request body.
        :return: The actor ID (key owner) from the signature.
        :raises SignatureVerificationError: If the signature is invalid.
        """
        # Parse the Signature header to find keyId
        lower_headers = {k.lower(): v for k, v in headers.items()}
        sig_header = lower_headers.get("signature", "")
        if not sig_header:
            raise SignatureVerificationError("Missing Signature header")

        # Extract keyId
        key_id = ""
        for part in sig_header.split(","):
            part = part.strip()
            if part.startswith("keyId="):
                key_id = part.split("=", 1)[1].strip('"')
                break

        if not key_id:
            raise SignatureVerificationError("No keyId in Signature header")

        # Derive actor URL from keyId (strip fragment)
        actor_url = key_id.split("#")[0]

        # Fetch the actor to get their public key
        actor_data = self._fetch_actor(actor_url)
        if actor_data is None:
            raise SignatureVerificationError(
                f"Cannot fetch actor for key verification: {actor_url}"
            )

        public_key_pem = ""
        pk = actor_data.get("publicKey")
        if isinstance(pk, dict):
            public_key_pem = pk.get("publicKeyPem", "")

        if not public_key_pem:
            raise SignatureVerificationError(
                f"No public key found for actor: {actor_url}"
            )

        public_key = load_public_key(public_key_pem)
        verify_request(public_key, method, path, headers, body)
        return actor_url

    def process(
        self,
        activity_data: dict,
        method: str = "POST",
        path: str = "/ap/inbox",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        skip_verification: bool = False,
    ) -> dict | None:
        """
        Process an incoming activity.

        :param activity_data: The parsed activity JSON-LD document.
        :param method: HTTP method of the incoming request.
        :param path: Request path of the incoming request.
        :param headers: Request headers (for signature verification).
        :param body: Raw request body (for signature verification).
        :param skip_verification: Skip HTTP signature verification
            (for testing only).
        :return: Response data or None.
        """
        if not skip_verification and headers:
            self.verify_signature(method, path, headers, body)

        activity = Activity.build(activity_data)
        activity_type_str = activity.type.strip()

        try:
            activity_type = ActivityType.from_raw(activity_type_str)
        except ValueError:
            logger.info("Ignoring unknown activity type: %s", activity_type_str)
            return None

        handler_map = {
            ActivityType.FOLLOW: self._handle_follow,
            ActivityType.UNDO: self._handle_undo,
            ActivityType.CREATE: self._handle_create,
            ActivityType.LIKE: self._handle_like,
            ActivityType.ANNOUNCE: self._handle_announce,
            ActivityType.DELETE: self._handle_delete,
            ActivityType.UPDATE: self._handle_update,
        }

        handler = handler_map.get(activity_type)
        if handler is None:
            logger.info("No handler for activity type: %s", activity_type_str)
            return None

        return handler(activity, activity_data)

    def _handle_follow(self, activity: Activity, raw: dict) -> dict | None:
        """Handle an incoming Follow activity."""
        actor_id = activity.actor
        logger.info("Processing Follow from %s", actor_id)

        # Fetch actor info to get their inbox
        actor_data = self._fetch_actor(actor_id)
        if actor_data is None:
            logger.warning("Cannot fetch actor for Follow: %s", actor_id)
            return None

        actor = Actor.build(actor_data)
        inbox = actor.inbox
        shared_inbox = actor.endpoints.get("sharedInbox", "")

        follower = Follower(
            actor_id=actor_id,
            inbox=inbox,
            shared_inbox=shared_inbox,
            followed_at=datetime.now(timezone.utc),
            actor_data=actor_data,
        )
        self.storage.store_follower(follower)

        # Send Accept back
        accept_activity = {
            "@context": AP_CONTEXT,
            "id": f"{self.actor_id}#accept-{activity.id}",
            "type": "Accept",
            "actor": self.actor_id,
            "object": raw,
        }

        self._deliver_to_inbox(inbox, accept_activity)
        return accept_activity

    def _handle_undo(self, activity: Activity, raw: dict) -> dict | None:
        """Handle an incoming Undo activity."""
        inner = activity.object
        if isinstance(inner, dict):
            inner_type = inner.get("type", "")
        elif isinstance(inner, str):
            inner_type = ""
        else:
            return None

        if inner_type == "Follow":
            actor_id = activity.actor
            logger.info("Processing Undo Follow from %s", actor_id)
            self.storage.remove_follower(actor_id)
        elif inner_type in ("Like", "Announce"):
            self._handle_undo_interaction(activity, inner)
        else:
            logger.info("Ignoring Undo of type: %s", inner_type)

        return None

    def _handle_undo_interaction(
        self, activity: Activity, inner: dict
    ) -> None:
        """Handle Undo of a Like or Announce."""
        actor_id = activity.actor
        inner_type = inner.get("type", "")

        # Determine interaction type
        if inner_type == "Like":
            interaction_type = InteractionType.LIKE
        elif inner_type == "Announce":
            interaction_type = InteractionType.BOOST
        else:
            return

        # Find the target resource
        obj = inner.get("object", "")
        target = obj if isinstance(obj, str) else obj.get("id", "") if isinstance(obj, dict) else ""

        if target:
            self.storage.delete_interaction(actor_id, target, interaction_type)
            logger.info(
                "Removed %s from %s on %s", interaction_type.value, actor_id, target
            )

    def _handle_create(self, activity: Activity, raw: dict) -> dict | None:
        """Handle an incoming Create activity (reply/comment)."""
        obj_data = activity.object
        if not isinstance(obj_data, dict):
            return None

        obj = Object.build(obj_data)
        target = obj.in_reply_to
        if not target:
            logger.info("Create without inReplyTo — ignoring")
            return None

        # Only process if the target is our resource
        actor_data = self._fetch_actor(activity.actor)
        author_name = ""
        author_url = ""
        author_photo = ""
        if actor_data:
            author_name = actor_data.get("name", "") or actor_data.get(
                "preferredUsername", ""
            )
            author_url = actor_data.get("url", "") or actor_data.get("id", "")
            icon = actor_data.get("icon")
            if isinstance(icon, dict):
                author_photo = icon.get("url", "")
            elif isinstance(icon, str):
                author_photo = icon

        interaction = Interaction(
            source_actor_id=activity.actor,
            target_resource=target,
            interaction_type=InteractionType.REPLY,
            activity_id=activity.id,
            object_id=obj.id,
            content=obj.content,
            author_name=author_name,
            author_url=author_url,
            author_photo=author_photo,
            published=obj.published or datetime.now(timezone.utc),
            status=InteractionStatus.CONFIRMED,
            metadata={"raw_object": obj_data},
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        self.storage.store_interaction(interaction)
        if self.on_interaction_received:
            self.on_interaction_received(interaction)

        logger.info("Stored reply from %s on %s", activity.actor, target)
        return None

    def _handle_like(self, activity: Activity, raw: dict) -> dict | None:
        """Handle an incoming Like activity."""
        obj = activity.object
        target = obj if isinstance(obj, str) else obj.get("id", "") if isinstance(obj, dict) else ""

        if not target:
            return None

        actor_data = self._fetch_actor(activity.actor)
        author_name = ""
        author_url = ""
        author_photo = ""
        if actor_data:
            author_name = actor_data.get("name", "") or actor_data.get(
                "preferredUsername", ""
            )
            author_url = actor_data.get("url", "") or actor_data.get("id", "")
            icon = actor_data.get("icon")
            if isinstance(icon, dict):
                author_photo = icon.get("url", "")
            elif isinstance(icon, str):
                author_photo = icon

        interaction = Interaction(
            source_actor_id=activity.actor,
            target_resource=target,
            interaction_type=InteractionType.LIKE,
            activity_id=activity.id,
            author_name=author_name,
            author_url=author_url,
            author_photo=author_photo,
            published=activity.published or datetime.now(timezone.utc),
            status=InteractionStatus.CONFIRMED,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        self.storage.store_interaction(interaction)
        if self.on_interaction_received:
            self.on_interaction_received(interaction)

        logger.info("Stored like from %s on %s", activity.actor, target)
        return None

    def _handle_announce(self, activity: Activity, raw: dict) -> dict | None:
        """Handle an incoming Announce (boost) activity."""
        obj = activity.object
        target = obj if isinstance(obj, str) else obj.get("id", "") if isinstance(obj, dict) else ""

        if not target:
            return None

        actor_data = self._fetch_actor(activity.actor)
        author_name = ""
        author_url = ""
        author_photo = ""
        if actor_data:
            author_name = actor_data.get("name", "") or actor_data.get(
                "preferredUsername", ""
            )
            author_url = actor_data.get("url", "") or actor_data.get("id", "")
            icon = actor_data.get("icon")
            if isinstance(icon, dict):
                author_photo = icon.get("url", "")
            elif isinstance(icon, str):
                author_photo = icon

        interaction = Interaction(
            source_actor_id=activity.actor,
            target_resource=target,
            interaction_type=InteractionType.BOOST,
            activity_id=activity.id,
            author_name=author_name,
            author_url=author_url,
            author_photo=author_photo,
            published=activity.published or datetime.now(timezone.utc),
            status=InteractionStatus.CONFIRMED,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        self.storage.store_interaction(interaction)
        if self.on_interaction_received:
            self.on_interaction_received(interaction)

        logger.info("Stored boost from %s on %s", activity.actor, target)
        return None

    def _handle_delete(self, activity: Activity, raw: dict) -> dict | None:
        """Handle an incoming Delete activity."""
        obj = activity.object
        if isinstance(obj, dict):
            target = obj.get("id", "")
        elif isinstance(obj, str):
            target = obj
        else:
            return None

        # Try deleting interactions where object_id matches
        # We don't know the exact interaction type, so try all
        for itype in InteractionType:
            self.storage.delete_interaction(activity.actor, target, itype)

        logger.info("Processed Delete from %s for %s", activity.actor, target)
        return None

    def _handle_update(self, activity: Activity, raw: dict) -> dict | None:
        """Handle an incoming Update activity."""
        obj_data = activity.object
        if not isinstance(obj_data, dict):
            return None

        obj = Object.build(obj_data)
        target = obj.in_reply_to
        if not target:
            return None

        # Update the stored reply
        actor_data = self._fetch_actor(activity.actor)
        author_name = ""
        author_url = ""
        author_photo = ""
        if actor_data:
            author_name = actor_data.get("name", "") or actor_data.get(
                "preferredUsername", ""
            )
            author_url = actor_data.get("url", "") or actor_data.get("id", "")
            icon = actor_data.get("icon")
            if isinstance(icon, dict):
                author_photo = icon.get("url", "")
            elif isinstance(icon, str):
                author_photo = icon

        interaction = Interaction(
            source_actor_id=activity.actor,
            target_resource=target,
            interaction_type=InteractionType.REPLY,
            activity_id=activity.id,
            object_id=obj.id,
            content=obj.content,
            author_name=author_name,
            author_url=author_url,
            author_photo=author_photo,
            published=obj.published or datetime.now(timezone.utc),
            status=InteractionStatus.CONFIRMED,
            metadata={"raw_object": obj_data},
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        self.storage.store_interaction(interaction)
        logger.info("Updated reply from %s on %s", activity.actor, target)
        return None

    def _deliver_to_inbox(self, inbox_url: str, activity: dict) -> bool:
        """Deliver an activity to a remote inbox."""
        import json

        body = json.dumps(activity).encode("utf-8")

        try:
            signed_headers = sign_request(
                private_key=self.private_key,  # type: ignore
                key_id=self.key_id,
                method="POST",
                url=inbox_url,
                body=body,
                headers={"Content-Type": "application/activity+json"},
            )

            resp = requests.post(
                inbox_url,
                data=body,
                headers={
                    **signed_headers,
                    "Content-Type": "application/activity+json",
                    "User-Agent": self.user_agent,
                },
                timeout=self.http_timeout,
            )

            if resp.status_code < 200 or resp.status_code >= 300:
                logger.warning(
                    "Delivery to %s returned status %d", inbox_url, resp.status_code
                )
                return False

            return True
        except Exception:
            logger.warning(
                "Failed to deliver to %s", inbox_url, exc_info=True
            )
            return False
