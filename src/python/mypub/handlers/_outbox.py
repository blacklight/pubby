"""
Outbox processing — build activities, fan-out delivery to follower inboxes.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timezone

import requests

from .._model import (
    Activity,
    AP_CONTEXT,
    Follower,
    Object,
)
from .._exceptions import DeliveryError
from ..crypto import sign_request
from ..storage import ActivityPubStorage

logger = logging.getLogger(__name__)

# Public addressing
AS_PUBLIC = "https://www.w3.org/ns/activitystreams#Public"


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
    :param user_agent: User-Agent for outgoing HTTP requests.
    :param http_timeout: Timeout for outgoing HTTP requests.
    """

    def __init__(
        self,
        storage: ActivityPubStorage,
        actor_id: str,
        private_key: object,
        key_id: str,
        followers_collection_url: str = "",
        *,
        max_retries: int = 3,
        retry_base_delay: float = 10.0,
        user_agent: str = "mypub/0.0.1",
        http_timeout: float = 15.0,
    ):
        self.storage = storage
        self.actor_id = actor_id
        self.private_key = private_key
        self.key_id = key_id
        self.followers_collection_url = followers_collection_url
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.user_agent = user_agent
        self.http_timeout = http_timeout

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
            "cc": obj.cc or [self.followers_collection_url] if self.followers_collection_url else obj.cc,
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
            "cc": obj.cc or [self.followers_collection_url] if self.followers_collection_url else obj.cc,
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
            "cc": [self.followers_collection_url] if self.followers_collection_url else [],
            "object": {
                "id": object_id,
                "type": "Tombstone",
            },
        }

        return activity

    def publish(self, activity: dict) -> dict:
        """
        Publish an activity: store in outbox and fan-out to followers.

        :param activity: The activity JSON-LD dictionary.
        :return: The stored activity.
        """
        activity_id = activity.get("id", self._new_activity_id())
        self.storage.store_activity(activity_id, activity)

        # Fan-out delivery
        followers = self.storage.get_followers()
        inboxes = self._collect_inboxes(followers)

        for inbox_url in inboxes:
            self._deliver_with_retry(inbox_url, activity)

        return activity

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
            except Exception:
                logger.warning(
                    "Delivery attempt %d/%d to %s failed",
                    attempt + 1,
                    self.max_retries,
                    inbox_url,
                    exc_info=True,
                )

            if attempt < self.max_retries - 1:
                delay = self.retry_base_delay * (2**attempt)
                logger.info(
                    "Retrying delivery to %s in %.1fs", inbox_url, delay
                )
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
