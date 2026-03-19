"""
Inbox processing — dispatch incoming activities by type.
"""

import logging
import uuid
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
from ._client import get_default_user_agent

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
    :param user_agent: User-Agent for outgoing HTTP requests. Default:
        ``pubby/{version} (+{actor_id})``
    :param http_timeout: Timeout for outgoing HTTP requests.
    :param store_local_only: If ``True``, only store interactions whose
        ``target_resource`` starts with a configured base URL, or that
        mention the local actor. The ``on_interaction_received`` callback
        is still invoked for all interactions.
    :param local_base_urls: List of base URLs considered "local". If empty,
        defaults to the actor's base URL (derived from ``actor_id``).
    """

    def __init__(
        self,
        storage: ActivityPubStorage,
        actor_id: str,
        private_key: object,
        key_id: str,
        *,
        on_interaction_received: Callable[[Interaction], None] | None = None,
        user_agent: str | None = None,
        http_timeout: float = 15.0,
        auto_approve_quotes: bool = True,
        store_local_only: bool = False,
        local_base_urls: list[str] | None = None,
    ):
        self.storage = storage
        self.actor_id = actor_id
        self.private_key = private_key
        self.key_id = key_id
        self.on_interaction_received = on_interaction_received
        self.http_timeout = http_timeout
        self.user_agent = user_agent or get_default_user_agent(actor_id)
        self.auto_approve_quotes = auto_approve_quotes
        self.store_local_only = store_local_only
        self.local_base_urls = local_base_urls or []

    def _is_local_target(self, target_resource: str) -> bool:
        """Check if target_resource is considered local."""
        base_urls = self.local_base_urls or [self.actor_id.rsplit("/", 1)[0]]
        return any(target_resource.startswith(base) for base in base_urls)

    def _should_store_interaction(
        self,
        target_resource: str,
        mentions_actor: bool = False,
    ) -> bool:
        """Determine if an interaction should be stored based on store_local_only."""
        if not self.store_local_only:
            return True
        return self._is_local_target(target_resource) or mentions_actor

    def _fetch_actor(self, actor_id: str) -> dict | None:
        """Fetch a remote actor document, using cache if available."""
        cached = self.storage.get_cached_actor(actor_id)
        if cached is not None:
            return cached

        try:
            base_headers = {
                "Accept": "application/activity+json, application/ld+json",
            }
            signed_headers = sign_request(
                private_key=self.private_key,  # type: ignore
                key_id=self.key_id,
                method="GET",
                url=actor_id,
                headers=base_headers,
            )
            resp = requests.get(
                actor_id,
                headers={
                    **signed_headers,
                    "Accept": "application/activity+json, application/ld+json",
                    "User-Agent": self.user_agent,
                },
                timeout=self.http_timeout,
            )
            resp.raise_for_status()
            actor_data = resp.json()
            self.storage.cache_remote_actor(actor_id, actor_data)
            return actor_data
        except requests.HTTPError as e:
            # 410 Gone is expected when an actor has been deleted
            if e.response is not None and e.response.status_code == 410:
                logger.debug("Actor gone (deleted): %s", actor_id)
            else:
                logger.warning("Failed to fetch actor %s", actor_id, exc_info=True)
            return None
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
            ActivityType.QUOTE_REQUEST: self._handle_quote_request,
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

    def _handle_undo(self, activity: Activity, _: dict) -> dict | None:
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
        elif inner_type in ("Like", "Announce") and isinstance(inner, dict):
            self._handle_undo_interaction(activity, inner)
        else:
            logger.info("Ignoring Undo of type: %s", inner_type)

        return None

    def _handle_undo_interaction(self, activity: Activity, inner: dict) -> None:
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
        target = (
            obj
            if isinstance(obj, str)
            else obj.get("id", "") if isinstance(obj, dict) else ""
        )

        if target:
            self.storage.delete_interaction(actor_id, target, interaction_type)
            logger.info(
                "Removed %s from %s on %s", interaction_type.value, actor_id, target
            )

    @staticmethod
    def _extract_quote_target(obj_data: dict) -> str | None:
        """Extract the quoted object URL from a Create object, if present.

        Checks the FEP-0449 ``quote`` field, Mastodon's ``quoteUrl``, and
        Misskey's ``_misskey_quote``.
        """
        for key in ("quote", "quoteUrl", "_misskey_quote"):
            value = obj_data.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _is_mention_of_actor(self, activity: Activity, obj_data: dict) -> bool:
        """Check if this Create activity is a direct mention of our actor."""
        # Check if actor is in to/cc fields
        to_list = activity.to or []
        cc_list = activity.cc or []
        all_recipients = to_list + cc_list

        if self.actor_id in all_recipients:
            return True

        # Check tag field for Mention objects targeting our actor
        tags = obj_data.get("tag", [])
        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, dict):
                    if (
                        tag.get("type") == "Mention"
                        and tag.get("href") == self.actor_id
                    ):
                        return True

        return False

    @staticmethod
    def _extract_mentioned_actors(obj_data: dict) -> list[str]:
        """Extract actor URLs from Mention tags in the object data."""
        mentioned = []
        tags = obj_data.get("tag", [])
        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, dict) and tag.get("type") == "Mention":
                    href = tag.get("href")
                    if isinstance(href, str) and href:
                        mentioned.append(href)
        return mentioned

    def _handle_create(self, activity: Activity, _: dict) -> dict | None:
        """Handle an incoming Create activity (reply/comment, quote, or mention)."""
        obj_data = activity.object
        if not isinstance(obj_data, dict):
            return None

        obj = Object.build(obj_data)
        quote_target = self._extract_quote_target(obj_data)
        target = obj.in_reply_to

        # Determine interaction type: quote > reply > mention
        if quote_target:
            interaction_type = InteractionType.QUOTE
            effective_target = quote_target
        elif target:
            interaction_type = InteractionType.REPLY
            effective_target = target
        elif self._is_mention_of_actor(activity, obj_data):
            # Direct mention to the actor (guestbook entry)
            interaction_type = InteractionType.MENTION
            effective_target = self.actor_id
        else:
            logger.info("Create without inReplyTo, quote, or mention — ignoring")
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

        mentioned_actors = self._extract_mentioned_actors(obj_data)

        interaction = Interaction(
            source_actor_id=activity.actor,
            target_resource=effective_target or "",
            interaction_type=interaction_type,
            activity_id=activity.id,
            object_id=obj.id,
            content=obj.content,
            author_name=author_name,
            author_url=author_url,
            author_photo=author_photo,
            published=obj.published or datetime.now(timezone.utc),
            status=InteractionStatus.CONFIRMED,
            metadata={"raw_object": obj_data},
            mentioned_actors=mentioned_actors,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        if self.on_interaction_received:
            self.on_interaction_received(interaction)

        mentions_local_actor = self.actor_id in mentioned_actors
        if self._should_store_interaction(effective_target or "", mentions_local_actor):
            self.storage.store_interaction(interaction)
            logger.info(
                "Stored %s from %s on %s",
                interaction_type.value,
                activity.actor,
                effective_target,
            )
        else:
            logger.debug(
                "Skipped storing %s from %s on %s (non-local)",
                interaction_type.value,
                activity.actor,
                effective_target,
            )

        return None

    def _handle_like(self, activity: Activity, _: dict) -> dict | None:
        """Handle an incoming Like activity."""
        obj = activity.object
        target = (
            obj
            if isinstance(obj, str)
            else obj.get("id", "") if isinstance(obj, dict) else ""
        )

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

        if self.on_interaction_received:
            self.on_interaction_received(interaction)

        if self._should_store_interaction(target):
            self.storage.store_interaction(interaction)
            logger.info("Stored like from %s on %s", activity.actor, target)
        else:
            logger.debug(
                "Skipped storing like from %s on %s (non-local)", activity.actor, target
            )

        return None

    def _handle_announce(self, activity: Activity, _: dict) -> dict | None:
        """Handle an incoming Announce (boost) activity."""
        obj = activity.object
        target = (
            obj
            if isinstance(obj, str)
            else obj.get("id", "") if isinstance(obj, dict) else ""
        )

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

        if self.on_interaction_received:
            self.on_interaction_received(interaction)

        if self._should_store_interaction(target):
            self.storage.store_interaction(interaction)
            logger.info("Stored boost from %s on %s", activity.actor, target)
        else:
            logger.debug(
                "Skipped storing boost from %s on %s (non-local)",
                activity.actor,
                target,
            )

        return None

    def _handle_delete(self, activity: Activity, _: dict) -> dict | None:
        """Handle an incoming Delete activity."""
        obj = activity.object
        if isinstance(obj, dict):
            target = obj.get("id", "")
        elif isinstance(obj, str):
            target = obj
        else:
            return None

        # Try to delete by object_id first (the common case: we know the
        # deleted object's URL but not which article it targeted).
        found = self.storage.delete_interaction_by_object_id(activity.actor, target)

        if not found:
            # Fallback: try interpreting target as a target_resource
            for itype in InteractionType:
                self.storage.delete_interaction(activity.actor, target, itype)

        logger.info("Processed Delete from %s for %s", activity.actor, target)
        return None

    def _handle_update(self, activity: Activity, _: dict) -> dict | None:
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

    def _handle_quote_request(self, activity: Activity, raw: dict) -> dict | None:
        """Handle an incoming QuoteRequest activity (FEP-044f).

        If ``auto_approve_quotes`` is enabled, builds a
        ``QuoteAuthorization`` object, stores it so it can be served via
        HTTP GET, and sends an ``Accept`` activity (with the authorization
        URL as ``result``) back to the quoting actor's inbox.
        """
        if not self.auto_approve_quotes:
            logger.info(
                "QuoteRequest from %s ignored (auto_approve disabled)", activity.actor
            )
            return None

        # Per FEP-044f: object = the quoted post, instrument = the quoting post
        quoted_object = activity.object
        quoted_uri = (
            quoted_object
            if isinstance(quoted_object, str)
            else quoted_object.get("id", "") if isinstance(quoted_object, dict) else ""
        )
        instrument = raw.get("instrument")
        quoting_uri = (
            instrument
            if isinstance(instrument, str)
            else instrument.get("id", "") if isinstance(instrument, dict) else ""
        )

        if not quoted_uri or not quoting_uri:
            logger.info("QuoteRequest missing object or instrument — ignoring")
            return None

        actor_data = self._fetch_actor(activity.actor)
        if not actor_data:
            logger.warning("Cannot fetch actor for QuoteRequest: %s", activity.actor)
            return None

        actor = Actor.build(actor_data)
        inbox_url = actor.inbox
        if not inbox_url:
            logger.warning("Cannot send Accept: no inbox for %s", activity.actor)
            return None

        # Build a dereferenceable QuoteAuthorization
        auth_id = f"{self.actor_id}/quote_authorizations/{uuid.uuid4()}"

        qa_context = [
            "https://www.w3.org/ns/activitystreams",
            {
                "QuoteAuthorization": "https://w3id.org/fep/044f#QuoteAuthorization",
                "gts": "https://gotosocial.org/ns#",
                "interactingObject": {
                    "@id": "gts:interactingObject",
                    "@type": "@id",
                },
                "interactionTarget": {
                    "@id": "gts:interactionTarget",
                    "@type": "@id",
                },
            },
        ]

        authorization = {
            "@context": qa_context,
            "type": "QuoteAuthorization",
            "id": auth_id,
            "attributedTo": self.actor_id,
            "interactionTarget": quoted_uri,
            "interactingObject": quoting_uri,
        }

        # Store so it can be served via HTTP GET
        self.storage.store_quote_authorization(auth_id, authorization)

        # Wrap in an Accept and deliver to the quoting actor
        accept_context = [
            "https://www.w3.org/ns/activitystreams",
            {"QuoteRequest": "https://w3id.org/fep/044f#QuoteRequest"},
        ]

        accept_activity = {
            "@context": accept_context,
            "type": "Accept",
            "id": f"{self.actor_id}/activities/{uuid.uuid4()}",
            "actor": self.actor_id,
            "to": activity.actor,
            "object": raw,
            "result": auth_id,
        }

        logger.info(
            "Accepting QuoteRequest %s with authorization %s → %s",
            activity.id,
            auth_id,
            inbox_url,
        )

        self._deliver_to_inbox(inbox_url, accept_activity)
        return accept_activity

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
            logger.warning("Failed to deliver to %s", inbox_url, exc_info=True)
            return False
