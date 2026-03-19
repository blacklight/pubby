"""
Outbox processing — build activities, fan-out delivery to follower inboxes.
"""

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

from .._model import (
    AP_CONTEXT,
    Actor,
    Follower,
    Object,
)
from ..crypto import sign_request
from ..storage import ActivityPubStorage
from ._client import get_default_user_agent

logger = logging.getLogger(__name__)

# Public addressing
AS_PUBLIC = "https://www.w3.org/ns/activitystreams#Public"

# URL patterns that are not actor URLs (collections, public addressing)
_NON_ACTOR_URL_PATTERNS = (
    "/followers",
    "/following",
    "/outbox",
    "/inbox",
)


class OutboxProcessor:
    """
    Handles outbound activity creation and delivery.

    :param storage: Storage backend.
    :param actor_id: This server's actor ID (URL).
    :param private_key: RSA private key for signing outgoing requests.
    :param key_id: Key ID for HTTP signatures.
    :param followers_collection_url: URL of the followers collection.
    :param max_retries: Maximum delivery retry attempts.
    :param retry_base_delay: Base delay (seconds) for exponential backoff.
    :param max_delivery_workers: Maximum threads for concurrent fan-out delivery.
    :param user_agent: User-Agent for outgoing HTTP requests.
    :param http_timeout: Timeout for outgoing HTTP requests.
    :param async_delivery: If ``True``, delivery fan-out runs in a background
        thread and ``publish()`` returns immediately after storing the
        activity. This prevents slow or unreachable inboxes from blocking
        the caller.
    """

    def __init__(
        self,
        storage: ActivityPubStorage,
        actor_id: str,
        private_key: object,
        key_id: str,
        *,
        followers_collection_url: str = "",
        max_retries: int = 3,
        retry_base_delay: float = 10.0,
        max_delivery_workers: int = 10,
        user_agent: str | None = None,
        http_timeout: float = 15.0,
        async_delivery: bool = True,
        **_,
    ):
        self.storage = storage
        self.actor_id = actor_id
        self.private_key = private_key
        self.key_id = key_id
        self.followers_collection_url = followers_collection_url
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.max_delivery_workers = max_delivery_workers
        self.user_agent = user_agent or get_default_user_agent(actor_id)
        self.http_timeout = http_timeout
        self.async_delivery = async_delivery

    def _new_activity_id(self) -> str:
        """Generate a unique activity ID."""
        return f"{self.actor_id}/activities/{uuid.uuid4()}"

    def build_create_activity(self, obj: Object) -> dict:
        """
        Build a Create activity wrapping an Object.

        :param obj: The object to wrap.
        :return: The activity as a JSON-LD dictionary.
        """
        activity_id = self._new_activity_id()
        now = datetime.now(timezone.utc)

        activity = {
            "@context": AP_CONTEXT,
            "id": activity_id,
            "type": "Create",
            "actor": self.actor_id,
            "published": now.isoformat(),
            "to": obj.to or [AS_PUBLIC],
            "cc": (
                obj.cc or [self.followers_collection_url]
                if self.followers_collection_url
                else obj.cc
            ),
            "object": obj.to_dict(),
        }

        return activity

    def build_update_activity(self, obj: Object) -> dict:
        """
        Build an Update activity wrapping an Object.

        :param obj: The updated object.
        :return: The activity as a JSON-LD dictionary.
        """
        activity_id = self._new_activity_id()
        now = datetime.now(timezone.utc)

        activity = {
            "@context": AP_CONTEXT,
            "id": activity_id,
            "type": "Update",
            "actor": self.actor_id,
            "published": now.isoformat(),
            "to": obj.to or [AS_PUBLIC],
            "cc": (
                obj.cc or [self.followers_collection_url]
                if self.followers_collection_url
                else obj.cc
            ),
            "object": obj.to_dict(),
        }

        return activity

    def build_delete_activity(self, object_id: str) -> dict:
        """
        Build a Delete activity for an object.

        :param object_id: The ID of the object to delete.
        :return: The activity as a JSON-LD dictionary.
        """
        activity_id = self._new_activity_id()
        now = datetime.now(timezone.utc)

        activity = {
            "@context": AP_CONTEXT,
            "id": activity_id,
            "type": "Delete",
            "actor": self.actor_id,
            "published": now.isoformat(),
            "to": [AS_PUBLIC],
            "cc": (
                [self.followers_collection_url] if self.followers_collection_url else []
            ),
            "object": {
                "id": object_id,
                "type": "Tombstone",
            },
        }

        return activity

    def build_like_activity(
        self,
        object_url: str,
        *,
        activity_id: str | None = None,
        published: datetime | None = None,
    ) -> dict:
        """
        Build a Like activity targeting a remote object.

        :param object_url: The URL of the object being liked.
        :param activity_id: Optional explicit activity ID. If not provided,
            a new unique ID is generated.
        :param published: Optional publication timestamp. Defaults to now.
        :return: The activity as a JSON-LD dictionary.
        """
        activity_id = activity_id or self._new_activity_id()
        now = (published or datetime.now(timezone.utc)).isoformat()

        activity = {
            "@context": AP_CONTEXT,
            "id": activity_id,
            "type": "Like",
            "actor": self.actor_id,
            "published": now,
            "object": object_url,
            "to": [AS_PUBLIC],
            "cc": (
                [self.followers_collection_url] if self.followers_collection_url else []
            ),
        }

        return activity

    def build_announce_activity(
        self,
        object_url: str,
        *,
        activity_id: str | None = None,
        published: datetime | None = None,
    ) -> dict:
        """
        Build an Announce (boost) activity targeting a remote object.

        :param object_url: The URL of the object being boosted.
        :param activity_id: Optional explicit activity ID. If not provided,
            a new unique ID is generated.
        :param published: Optional publication timestamp. Defaults to now.
        :return: The activity as a JSON-LD dictionary.
        """
        activity_id = activity_id or self._new_activity_id()
        now = (published or datetime.now(timezone.utc)).isoformat()
        activity = {
            "@context": AP_CONTEXT,
            "id": activity_id,
            "type": "Announce",
            "actor": self.actor_id,
            "published": now,
            "object": object_url,
            "to": [AS_PUBLIC],
            "cc": (
                [self.followers_collection_url] if self.followers_collection_url else []
            ),
        }

        return activity

    def build_undo_activity(self, inner_activity: dict) -> dict:
        """
        Build an Undo activity wrapping another activity.

        This is intentionally generic: it works for ``Undo Like``,
        ``Undo Announce``, ``Undo Follow``, etc.

        :param inner_activity: The activity to undo (must contain at least
            ``id``, ``type``, ``actor``, and ``object``).
        :return: The Undo activity as a JSON-LD dictionary.
        """
        activity_id = self._new_activity_id()
        now = datetime.now(timezone.utc).isoformat()
        activity = {
            "@context": AP_CONTEXT,
            "id": activity_id,
            "type": "Undo",
            "actor": self.actor_id,
            "published": now,
            "object": inner_activity,
            "to": inner_activity.get("to", [AS_PUBLIC]),
            "cc": inner_activity.get("cc", []),
        }

        return activity

    def publish(self, activity: dict) -> dict:
        """
        Publish an activity: store in outbox and fan-out to followers and
        mentioned actors.

        Delivery targets include:
        - All follower inboxes (via stored follower list)
        - All actor inboxes from the activity's ``to`` and ``cc`` fields
          (e.g., mentioned users)

        :param activity: The activity JSON-LD dictionary.
        :return: The stored activity.
        """
        activity_id = activity.get("id", self._new_activity_id())
        self.storage.store_activity(activity_id, activity)

        # Collect follower inboxes
        followers = self.storage.get_followers()
        follower_inboxes = self._collect_inboxes(followers)
        logger.debug(
            "Collected %d follower inboxes for activity %s",
            len(follower_inboxes),
            activity_id,
        )

        # Collect recipient inboxes (mentioned actors, etc.)
        recipient_inboxes = self._collect_recipient_inboxes(activity)
        logger.debug(
            "Collected %d recipient inboxes for activity %s: %s",
            len(recipient_inboxes),
            activity_id,
            recipient_inboxes,
        )

        # Merge and deduplicate
        seen: set[str] = set(follower_inboxes)
        inboxes = list(follower_inboxes)
        for inbox in recipient_inboxes:
            if inbox not in seen:
                seen.add(inbox)
                inboxes.append(inbox)

        logger.info(
            "Delivering activity %s to %d inboxes",
            activity_id,
            len(inboxes),
        )

        if self.async_delivery:
            # Fire-and-forget: spawn a daemon thread for delivery
            threading.Thread(
                target=self._fan_out_delivery,
                args=(inboxes, activity),
                daemon=True,
                name=f"ap-deliver-{activity_id.split('/')[-1][:8]}",
            ).start()
        else:
            # Blocking: wait for all deliveries to complete
            self._fan_out_delivery(inboxes, activity)

        return activity

    def _fan_out_delivery(self, inboxes: list[str], activity: dict) -> None:
        """
        Deliver an activity to multiple inboxes concurrently.

        :param inboxes: List of inbox URLs.
        :param activity: The activity to deliver.
        """
        if not inboxes:
            return

        with ThreadPoolExecutor(
            max_workers=min(self.max_delivery_workers, len(inboxes))
        ) as pool:
            futures = {
                pool.submit(self._deliver_with_retry, url, activity): url
                for url in inboxes
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    future.result()
                except Exception:
                    logger.error(
                        "Delivery to %s raised an unexpected exception",
                        url,
                        exc_info=True,
                    )

    def _collect_inboxes(self, followers: list[Follower]) -> list[str]:
        """
        Collect unique inbox URLs from followers, preferring shared inboxes.

        :param followers: List of followers.
        :return: Deduplicated list of inbox URLs.
        """
        seen: set[str] = set()
        inboxes: list[str] = []

        for follower in followers:
            # Prefer shared inbox to reduce delivery requests
            inbox = follower.shared_inbox or follower.inbox
            if inbox and inbox not in seen:
                seen.add(inbox)
                inboxes.append(inbox)

        return inboxes

    def _is_actor_url(self, url: str) -> bool:
        """
        Check if a URL is likely an actor URL (not a collection or special URL).

        :param url: The URL to check.
        :return: True if it appears to be an actor URL.
        """
        if not url or not url.startswith("http"):
            return False

        # Exclude public addressing
        if url == AS_PUBLIC:
            return False

        # Exclude our own collections
        if self.followers_collection_url and url == self.followers_collection_url:
            return False

        # Exclude common collection URL patterns
        for pattern in _NON_ACTOR_URL_PATTERNS:
            if url.endswith(pattern):
                return False

        return True

    def _extract_recipient_actors(self, activity: dict) -> list[str]:
        """
        Extract actor URLs from the activity's to and cc fields.

        Filters out special URLs like AS_PUBLIC and collection URLs.

        :param activity: The activity dictionary.
        :return: List of actor URLs.
        """
        actors: list[str] = []
        seen: set[str] = set()

        for field in ("to", "cc"):
            recipients = activity.get(field, [])
            if isinstance(recipients, str):
                recipients = [recipients]

            for url in recipients:
                if url not in seen and self._is_actor_url(url):
                    seen.add(url)
                    actors.append(url)

        return actors

    def _fetch_actor(self, actor_url: str) -> dict | None:
        """
        Fetch a remote actor document.

        :param actor_url: The actor's URL/ID.
        :return: The actor document or None if fetch failed.
        """
        # Check cache first
        cached = self.storage.get_cached_actor(actor_url)
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
                url=actor_url,
                headers=base_headers,
            )
            resp = requests.get(
                actor_url,
                headers={
                    **signed_headers,
                    "Accept": "application/activity+json, application/ld+json",
                    "User-Agent": self.user_agent,
                },
                timeout=self.http_timeout,
            )
            resp.raise_for_status()
            actor_data = resp.json()
            self.storage.cache_remote_actor(actor_url, actor_data)
            return actor_data
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 410:
                logger.debug("Actor gone (deleted): %s", actor_url)
            else:
                logger.warning("Failed to fetch actor %s: %s", actor_url, e)
            return None
        except Exception as e:
            logger.warning("Failed to fetch actor %s: %s", actor_url, e)
            return None

    def _collect_recipient_inboxes(self, activity: dict) -> list[str]:
        """
        Collect inbox URLs for actors in the activity's to/cc fields.

        Fetches each actor document to get their inbox URL.

        :param activity: The activity dictionary.
        :return: List of inbox URLs.
        """
        actor_urls = self._extract_recipient_actors(activity)
        logger.debug("Extracted recipient actor URLs: %s", actor_urls)
        inboxes: list[str] = []
        seen: set[str] = set()

        for actor_url in actor_urls:
            actor_data = self._fetch_actor(actor_url)
            if not actor_data:
                logger.debug("Failed to fetch actor data for %s", actor_url)
                continue

            actor = Actor.build(actor_data)
            # Prefer shared inbox to reduce delivery requests
            inbox = actor.endpoints.get("sharedInbox") or actor.inbox
            logger.debug(
                "Actor %s resolved to inbox %s",
                actor_url,
                inbox,
            )
            if inbox and inbox not in seen:
                seen.add(inbox)
                inboxes.append(inbox)

        return inboxes

    def _deliver_with_retry(self, inbox_url: str, activity: dict) -> bool:
        """
        Deliver an activity to a remote inbox with exponential backoff retry.

        :param inbox_url: The inbox URL.
        :param activity: The activity to deliver.
        :return: True if delivery succeeded.
        """
        for attempt in range(self.max_retries):
            try:
                success = self._deliver(inbox_url, activity)
                if success:
                    return True
            except Exception as e:
                logger.warning(
                    "Delivery attempt %d/%d to %s failed: %s: %s",
                    attempt + 1,
                    self.max_retries,
                    inbox_url,
                    type(e).__name__,
                    e,
                )

            if attempt < self.max_retries - 1:
                delay = self.retry_base_delay * (2**attempt)
                logger.info("Retrying delivery to %s in %.1fs", inbox_url, delay)
                time.sleep(delay)

        logger.error(
            "Delivery to %s failed after %d attempts",
            inbox_url,
            self.max_retries,
        )
        return False

    def _deliver(self, inbox_url: str, activity: dict) -> bool:
        """
        Deliver an activity to a single inbox.

        :param inbox_url: The inbox URL.
        :param activity: The activity to deliver.
        :return: True if the server accepted the delivery (2xx).
        """
        body = json.dumps(activity).encode("utf-8")
        content_type = "application/activity+json"
        content_length = str(len(body))

        signed_headers = sign_request(
            private_key=self.private_key,  # type: ignore
            key_id=self.key_id,
            method="POST",
            url=inbox_url,
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": content_length,
            },
            signed_headers=[
                "(request-target)",
                "host",
                "date",
                "digest",
                "content-type",
                "content-length",
            ],
        )

        resp = requests.post(
            inbox_url,
            data=body,
            headers={
                **signed_headers,
                "Content-Type": content_type,
                "Content-Length": content_length,
                "User-Agent": self.user_agent,
            },
            timeout=self.http_timeout,
        )

        if 200 <= resp.status_code < 300:
            logger.info("Delivered to %s (status %d)", inbox_url, resp.status_code)
            return True

        logger.warning(
            "Delivery to %s returned status %d: %s",
            inbox_url,
            resp.status_code,
            resp.text[:200],
        )

        # Retry on 5xx and connection errors
        if resp.status_code >= 500:
            return False

        # 4xx errors are not retryable
        return False

    def get_outbox_collection(
        self,
        collection_url: str,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        """
        Build the outbox OrderedCollection response.

        :param collection_url: The outbox collection URL.
        :param limit: Maximum items per page.
        :param offset: Pagination offset.
        :return: An OrderedCollection JSON-LD dictionary.
        """
        activities = self.storage.get_activities(limit=limit, offset=offset)

        return {
            "@context": AP_CONTEXT,
            "id": collection_url,
            "type": "OrderedCollection",
            "totalItems": len(activities),
            "orderedItems": activities,
        }
