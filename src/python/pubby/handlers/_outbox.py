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
from typing import Any, Callable, Collection

import requests
from cryptography.hazmat.primitives.asymmetric import rsa

from .._model import (
    AP_CONTEXT,
    Actor,
    Follower,
    Object,
)
from ..crypto import sign_request
from ..moderation import is_domain_blocked
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


def collect_inboxes(followers: list[Follower]) -> list[str]:
    """
    Collect unique inbox URLs from followers, preferring shared inboxes.

    Shared inboxes are preferred over per-actor inboxes to reduce the number
    of delivery requests; followers without any inbox are skipped.  This is
    the same collection logic :class:`OutboxProcessor` uses for its fan-out,
    exposed for applications that build their own delivery pipeline.

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


def deliver_activity(
    activity: dict,
    inbox_url: str,
    *,
    key_id: str,
    private_key: rsa.RSAPrivateKey,
    user_agent: str | None = None,
    timeout: float = 15.0,
) -> int:
    """
    Deliver an activity to a single inbox with one signed POST request.

    Unlike the built-in fan-out, this performs no retries and does not
    raise on HTTP error statuses: it returns the response status code so
    callers (e.g. a task-queue worker) can decide their own retry policy.
    Network-level failures (connection errors, timeouts) still raise the
    underlying ``requests`` exception.

    :param activity: The activity JSON-LD dictionary.
    :param inbox_url: The remote inbox URL.
    :param key_id: Key ID for the HTTP signature (``<actor_id>#main-key``).
    :param private_key: RSA private key used to sign the request.
    :param user_agent: Optional ``User-Agent`` header; defaults to
        ``pubby/{version}``.
    :param timeout: HTTP request timeout in seconds.
    :return: The HTTP response status code.
    """
    body = json.dumps(activity).encode("utf-8")
    content_type = "application/activity+json"
    content_length = str(len(body))

    signed_headers = sign_request(
        private_key=private_key,
        key_id=key_id,
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

    if user_agent is None:
        from pubby import __version__

        user_agent = f"pubby/{__version__}"

    resp = requests.post(
        inbox_url,
        data=body,
        headers={
            **signed_headers,
            "Content-Type": content_type,
            "Content-Length": content_length,
            "User-Agent": user_agent,
        },
        timeout=timeout,
    )

    if 200 <= resp.status_code < 300:
        logger.info("Delivered to %s (status %d)", inbox_url, resp.status_code)
    else:
        logger.warning(
            "Delivery to %s returned status %d: %s",
            inbox_url,
            resp.status_code,
            resp.text[:200],
        )

    return resp.status_code


def _new_activity_id(actor_id: str) -> str:
    """Generate a unique activity ID under an actor."""
    return f"{actor_id}/activities/{uuid.uuid4()}"


def _format_published(published: datetime | str | None) -> str:
    """Normalize a published timestamp to an ISO-8601 string."""
    if published is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(published, datetime):
        return published.isoformat()
    return published


def build_like_activity(
    actor_id: str,
    object_id: str,
    *,
    to: list[str] | None = None,
    cc: list[str] | None = None,
    activity_id: str | None = None,
    published: datetime | str | None = None,
    context: Any = AP_CONTEXT,
) -> dict:
    """
    Build a Like activity targeting a remote object.

    Audience is caller-provided; when ``to``/``cc`` are omitted, the
    activity defaults to ``to=[AS_PUBLIC]`` and ``cc=[]``.

    :param actor_id: The actor ID (URL) performing the Like.
    :param object_id: The URL of the object being liked.
    :param to: Optional explicit ``to`` recipients. Defaults to public.
    :param cc: Optional explicit ``cc`` recipients. Defaults to empty.
    :param activity_id: Optional explicit activity ID. If not provided, a
        new unique ID is generated under ``actor_id``.
    :param published: Optional publication timestamp. A ``datetime`` is
        converted with ``.isoformat()``; a ``str`` is used as-is;
        ``None`` defaults to the current UTC time.
    :param context: Optional JSON-LD ``@context`` value. Defaults to
        ``AP_CONTEXT``.
    :return: The activity as a JSON-LD dictionary.
    """
    if to is None:
        to = [AS_PUBLIC]
    if cc is None:
        cc = []

    return {
        "@context": context,
        "id": activity_id or _new_activity_id(actor_id),
        "type": "Like",
        "actor": actor_id,
        "published": _format_published(published),
        "object": object_id,
        "to": to,
        "cc": cc,
    }


def build_announce_activity(
    actor_id: str,
    object_id: str,
    *,
    to: list[str] | None = None,
    cc: list[str] | None = None,
    activity_id: str | None = None,
    published: datetime | str | None = None,
    context: Any = AP_CONTEXT,
) -> dict:
    """
    Build an Announce (boost) activity targeting a remote object.

    Audience is caller-provided; when ``to``/``cc`` are omitted, the
    activity defaults to ``to=[AS_PUBLIC]`` and ``cc=[]``.

    :param actor_id: The actor ID (URL) performing the Announce.
    :param object_id: The URL of the object being boosted.
    :param to: Optional explicit ``to`` recipients. Defaults to public.
    :param cc: Optional explicit ``cc`` recipients. Defaults to empty.
    :param activity_id: Optional explicit activity ID. If not provided, a
        new unique ID is generated under ``actor_id``.
    :param published: Optional publication timestamp. A ``datetime`` is
        converted with ``.isoformat()``; a ``str`` is used as-is;
        ``None`` defaults to the current UTC time.
    :param context: Optional JSON-LD ``@context`` value. Defaults to
        ``AP_CONTEXT``.
    :return: The activity as a JSON-LD dictionary.
    """
    if to is None:
        to = [AS_PUBLIC]
    if cc is None:
        cc = []

    return {
        "@context": context,
        "id": activity_id or _new_activity_id(actor_id),
        "type": "Announce",
        "actor": actor_id,
        "published": _format_published(published),
        "object": object_id,
        "to": to,
        "cc": cc,
    }


def build_undo_activity(
    inner_activity: dict,
    actor_id: str,
    *,
    activity_id: str | None = None,
    published: datetime | str | None = None,
    to: list[str] | None = None,
    cc: list[str] | None = None,
    context: Any = AP_CONTEXT,
) -> dict:
    """
    Build an Undo activity wrapping another activity.

    This is intentionally generic: it works for ``Undo Like``,
    ``Undo Announce``, ``Undo Follow``, etc.

    When ``to``/``cc`` are omitted, the addressing is inherited from
    ``inner_activity``; if the inner activity has no addressing, it
    defaults to ``to=[AS_PUBLIC]`` and ``cc=[]``.

    :param inner_activity: The activity to undo (must contain at least
        ``id``, ``type``, ``actor``, and ``object``).
    :param actor_id: The actor ID (URL) performing the Undo.
    :param activity_id: Optional explicit activity ID. If not provided, a
        new unique ID is generated under ``actor_id``.
    :param published: Optional publication timestamp. A ``datetime`` is
        converted with ``.isoformat()``; a ``str`` is used as-is;
        ``None`` defaults to the current UTC time.
    :param to: Optional explicit ``to`` recipients. Defaults to the inner
        activity's ``to`` field (or public).
    :param cc: Optional explicit ``cc`` recipients. Defaults to the inner
        activity's ``cc`` field (or empty).
    :param context: Optional JSON-LD ``@context`` value. Defaults to
        ``AP_CONTEXT``.
    :return: The Undo activity as a JSON-LD dictionary.
    """
    if to is None:
        to = inner_activity.get("to", [AS_PUBLIC])
    if cc is None:
        cc = inner_activity.get("cc", [])

    return {
        "@context": context,
        "id": activity_id or _new_activity_id(actor_id),
        "type": "Undo",
        "actor": actor_id,
        "published": _format_published(published),
        "object": inner_activity,
        "to": to,
        "cc": cc,
    }


def build_delete_activity(
    actor_id: str,
    object_id: str,
    *,
    to: list[str] | None = None,
    cc: list[str] | None = None,
    activity_id: str | None = None,
    published: datetime | str | None = None,
    context: Any = AP_CONTEXT,
) -> dict:
    """
    Build a Delete activity wrapping a Tombstone for ``object_id``.

    When ``to`` is omitted the activity is addressed to the public
    ActivityStreams collection. When ``cc`` is omitted it defaults to the
    actor's followers collection via ``{actor_id}/followers``.

    :param actor_id: The actor ID (URL) performing the Delete.
    :param object_id: The URL of the object being deleted.
    :param to: Optional explicit ``to`` recipients. Defaults to public.
    :param cc: Optional explicit ``cc`` recipients. Defaults to
        ``[f"{actor_id}/followers"]``.
    :param activity_id: Optional explicit activity ID. If not provided, a
        new unique ID is generated under ``actor_id``.
    :param published: Optional publication timestamp. A ``datetime`` is
        converted with ``.isoformat()``; a ``str`` is used as-is;
        ``None`` defaults to the current UTC time.
    :param context: Optional JSON-LD ``@context`` value. Defaults to
        ``AP_CONTEXT``.
    :return: The Delete activity as a JSON-LD dictionary.
    """
    if to is None:
        to = [AS_PUBLIC]
    if cc is None:
        cc = [f"{actor_id}/followers"]

    return {
        "@context": context,
        "id": activity_id or _new_activity_id(actor_id),
        "type": "Delete",
        "actor": actor_id,
        "published": _format_published(published),
        "to": to,
        "cc": cc,
        "object": {
            "id": object_id,
            "type": "Tombstone",
        },
    }


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
    :param allowed_instances: Optional allow-list of remote instance domains.
        When non-empty, deliveries are only sent to inboxes on these domains.
    :param blocked_instances: Optional block-list of remote instance domains.
        Inboxes on these domains are skipped during delivery fan-out.
    :param deliver: Optional custom delivery callable invoked once per
        collected inbox as ``deliver(inbox_url, activity)`` instead of the
        built-in ``ThreadPoolExecutor`` fan-out.  Use it to route deliveries
        through a task queue (e.g. Celery or RQ) while keeping Pubby's inbox
        collection, shared-inbox deduplication, and instance allow/block
        filtering.  The callable is invoked synchronously by ``publish()``;
        ``async_delivery`` does not apply to it.
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
        allowed_instances: Collection[str] | None = None,
        blocked_instances: Collection[str] | None = None,
        deliver: Callable[[str, dict], None] | None = None,
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
        self.allowed_instances = allowed_instances
        self.blocked_instances = blocked_instances
        self.deliver = deliver

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

        # If addressing is explicitly provided (either to or cc has values),
        # use it as-is. Otherwise, apply defaults. This preserves empty cc
        # for direct messages while still defaulting unaddressed posts.
        has_explicit_addressing = bool(obj.to) or bool(obj.cc)
        to_field = obj.to if has_explicit_addressing else [AS_PUBLIC]
        cc_field = (
            obj.cc
            if has_explicit_addressing
            else (
                [self.followers_collection_url] if self.followers_collection_url else []
            )
        )

        activity = {
            "@context": AP_CONTEXT,
            "id": activity_id,
            "type": "Create",
            "actor": self.actor_id,
            "published": now.isoformat(),
            "to": to_field,
            "cc": cc_field,
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

        # If addressing is explicitly provided (either to or cc has values),
        # use it as-is. Otherwise, apply defaults.
        has_explicit_addressing = bool(obj.to) or bool(obj.cc)
        to_field = obj.to if has_explicit_addressing else [AS_PUBLIC]
        cc_field = (
            obj.cc
            if has_explicit_addressing
            else (
                [self.followers_collection_url] if self.followers_collection_url else []
            )
        )

        activity = {
            "@context": AP_CONTEXT,
            "id": activity_id,
            "type": "Update",
            "actor": self.actor_id,
            "published": now.isoformat(),
            "to": to_field,
            "cc": cc_field,
            "object": obj.to_dict(),
        }

        return activity

    def build_delete_activity(self, object_id: str) -> dict:
        """
        Build a Delete activity for an object.

        :param object_id: The ID of the object to delete.
        :return: The activity as a JSON-LD dictionary.
        """
        return build_delete_activity(
            actor_id=self.actor_id,
            object_id=object_id,
            cc=[self.followers_collection_url] if self.followers_collection_url else [],
            context=AP_CONTEXT,
        )

    def build_like_activity(
        self,
        object_url: str,
        *,
        activity_id: str | None = None,
        published: datetime | str | None = None,
    ) -> dict:
        """
        Build a Like activity targeting a remote object.

        :param object_url: The URL of the object being liked.
        :param activity_id: Optional explicit activity ID. If not provided,
            a new unique ID is generated.
        :param published: Optional publication timestamp. Defaults to now.
        :return: The activity as a JSON-LD dictionary.
        """
        return build_like_activity(
            actor_id=self.actor_id,
            object_id=object_url,
            cc=[self.followers_collection_url] if self.followers_collection_url else [],
            activity_id=activity_id,
            published=published,
        )

    def build_announce_activity(
        self,
        object_url: str,
        *,
        activity_id: str | None = None,
        published: datetime | str | None = None,
    ) -> dict:
        """
        Build an Announce (boost) activity targeting a remote object.

        :param object_url: The URL of the object being boosted.
        :param activity_id: Optional explicit activity ID. If not provided,
            a new unique ID is generated.
        :param published: Optional publication timestamp. Defaults to now.
        :return: The activity as a JSON-LD dictionary.
        """
        return build_announce_activity(
            actor_id=self.actor_id,
            object_id=object_url,
            cc=[self.followers_collection_url] if self.followers_collection_url else [],
            activity_id=activity_id,
            published=published,
        )

    def build_undo_activity(self, inner_activity: dict) -> dict:
        """
        Build an Undo activity wrapping another activity.

        This is intentionally generic: it works for ``Undo Like``,
        ``Undo Announce``, ``Undo Follow``, etc.

        :param inner_activity: The activity to undo (must contain at least
            ``id``, ``type``, ``actor``, and ``object``).
        :return: The Undo activity as a JSON-LD dictionary.
        """
        return build_undo_activity(
            inner_activity=inner_activity,
            actor_id=self.actor_id,
        )

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

        # Check if this activity is addressed to followers
        # (i.e., followers URL appears in to or cc)
        addressed_to_followers = self._is_addressed_to_followers(activity)

        # Collect follower inboxes only if addressed to followers
        if addressed_to_followers:
            followers = self.storage.get_followers(actor_id=self.actor_id)
            follower_inboxes = collect_inboxes(followers)
            logger.debug(
                "Collected %d follower inboxes for activity %s",
                len(follower_inboxes),
                activity_id,
            )
        else:
            follower_inboxes = []
            logger.debug(
                "Skipping follower inboxes for activity %s (not addressed to followers)",
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

        # Skip inboxes on blocked or non-allowed instances
        if self.allowed_instances or self.blocked_instances:
            unfiltered = inboxes
            inboxes = [
                url
                for url in inboxes
                if not is_domain_blocked(
                    url,
                    allowed=self.allowed_instances,
                    blocked=self.blocked_instances,
                )
            ]
            skipped = len(unfiltered) - len(inboxes)
            if skipped:
                logger.info(
                    "Skipped %d inbox(es) on blocked/non-allowed instances "
                    "for activity %s",
                    skipped,
                    activity_id,
                )

        logger.info(
            "Delivering activity %s to %d inboxes",
            activity_id,
            len(inboxes),
        )

        if self.deliver is not None:
            # Custom delivery seam: hand each inbox to the caller-supplied
            # callable (e.g. a task-queue enqueue) instead of fanning out
            # via ThreadPoolExecutor.
            for inbox_url in inboxes:
                try:
                    self.deliver(inbox_url, activity)
                except Exception:
                    logger.error(
                        "Custom deliver callable failed for inbox %s",
                        inbox_url,
                        exc_info=True,
                    )
        elif self.async_delivery:
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

        Delegates to the module-level :func:`collect_inboxes`.

        :param followers: List of followers.
        :return: Deduplicated list of inbox URLs.
        """
        return collect_inboxes(followers)

    def _is_addressed_to_followers(self, activity: dict) -> bool:
        """
        Check if an activity is addressed to the followers collection.

        This is used to determine whether to fan out to all followers.
        Activities that are not addressed to followers (e.g., direct messages)
        should only be delivered to explicitly mentioned recipients.

        :param activity: The activity dictionary.
        :return: True if followers collection URL appears in to or cc.
        """
        if not self.followers_collection_url:
            # If no followers collection is configured, assume public activities
            # should go to followers
            to_cc = activity.get("to", []) + activity.get("cc", [])
            return AS_PUBLIC in to_cc

        for field in ("to", "cc"):
            recipients = activity.get(field, [])
            if isinstance(recipients, str):
                recipients = [recipients]

            if self.followers_collection_url in recipients:
                return True

            # Also check for AS_PUBLIC - public posts should go to followers
            if AS_PUBLIC in recipients:
                return True

        return False

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
            # Skip blocked/non-allowed recipient instances before fetching
            # their actor document, so blocked instances are never contacted.
            if is_domain_blocked(
                actor_url,
                allowed=self.allowed_instances,
                blocked=self.blocked_instances,
            ):
                logger.debug(
                    "Skipping recipient on blocked/non-allowed instance: %s",
                    actor_url,
                )
                continue

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

        Delegates to the module-level :func:`deliver_activity`.

        :param inbox_url: The inbox URL.
        :param activity: The activity to deliver.
        :return: True if the server accepted the delivery (2xx).
        """
        status = deliver_activity(
            activity,
            inbox_url,
            key_id=self.key_id,
            private_key=self.private_key,  # type: ignore
            user_agent=self.user_agent,
            timeout=self.http_timeout,
        )
        return 200 <= status < 300

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
