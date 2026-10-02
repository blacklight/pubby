"""
Main ActivityPub handler — analogous to WebmentionsHandler.
"""

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Collection, Dict, List, Optional, Union

from cryptography.hazmat.primitives.asymmetric import rsa
from jinja2 import Template
from markupsafe import Markup

from .._model import (
    Actor,
    ActorConfig,
    FollowPolicy,
    Interaction,
    Object,
    AP_CONTEXT,
)
from .. import cache as _cache
from ..crypto._keys import load_private_key, export_public_key_pem
from ..render import InteractionsRenderer
from ..storage import ActivityPubStorage
from ._discovery import (
    build_nodeinfo_discovery,
    build_nodeinfo_document,
    build_webfinger_response,
)
from ._inbox import InboxProcessor
from ._outbox import OutboxProcessor

logger = logging.getLogger(__name__)


class ActivityPubHandler:
    """
    Main ActivityPub handler.

    :param storage: The storage backend.
    :param actor_config: Actor configuration — an :class:`ActorConfig` instance
        or a plain ``dict`` (converted automatically for backwards compatibility).
    :param private_key: RSA private key (object or PEM string/bytes).
    :param private_key_path: Path to a PEM-encoded private key file
        (alternative to ``private_key``).
    :param on_interaction_received: Optional callback when an interaction
        is received.
    :param webfinger_domain: Domain for WebFinger ``acct:`` URIs. Defaults
        to the domain from ``base_url``.
    :param user_agent: User-Agent string for outgoing HTTP requests.
        Defaults to ``pubby/{__version__}``.
    :param http_timeout: Timeout in seconds for outgoing HTTP requests.
    :param max_retries: Maximum delivery retry attempts.
    :param max_delivery_workers: Maximum threads for concurrent delivery fan-out.
    :param auto_approve_quotes: If ``True`` (default), automatically send a
        ``QuoteAuthorization`` when an incoming quote targets a local object,
        so the remote server clears its "pending" state.
    :param store_local_only: If ``True``, only store interactions whose
        ``target_resource`` starts with a configured base URL, or that
        mention the local actor. The ``on_interaction_received`` callback
        is still invoked for all interactions.
    :param local_base_urls: List of base URLs considered "local". If empty,
        defaults to the actor's base URL.
    :param software_name: Software name for NodeInfo.
    :param software_version: Software version for NodeInfo. Defaults to
        pubby's ``__version__``.
    :param async_delivery: If ``True``, delivery fan-out runs in a background
        thread and ``publish_object()`` / ``publish_activity()`` return
        immediately after storing the activity. This prevents slow or
        unreachable inboxes from blocking the caller. Defaults to ``True``.
        Set it to ``False`` for synchronous delivery (easier to debug, errors
        appear in the same stack trace as the caller, but it may cause delays
        upon failed deliveries).
    :param allowed_instances: Optional allow-list of remote instance domains.
        When non-empty, only these instances may send activities to the inbox
        or receive deliveries.
    :param blocked_instances: Optional block-list of remote instance domains.
        Activities from, and deliveries to, these instances are dropped.
    :param deliver: Optional custom delivery callable invoked once per
        collected inbox as ``deliver(inbox_url, activity)`` instead of the
        built-in threaded fan-out.  Use it to route deliveries through a
        task queue (e.g. Celery or RQ) while keeping Pubby's inbox
        collection, shared-inbox deduplication, and instance filtering.
        Combine with :func:`pubby.deliver_activity` inside your task to
        perform the signed POST.
    :param strict_attribution: If ``True``, inbound ``Create``/``Update``
        objects are validated via :func:`pubby.attribution.validate` before
        processing: a non-empty ``attributedTo`` must name the delivering
        actor and the object ``id`` must share its authority. Mismatched
        objects are logged and dropped. Defaults to ``False`` — deployments
        behind relays or account migration may legitimately receive
        cross-host objects.
    :param follow_policy: Optional callback deciding how an incoming
        ``Follow`` is handled, forwarded to the inbox processor. When
        unset and ``actor_config.manually_approves_followers`` is true,
        every local target defaults to :attr:`FollowPolicy.MANUAL` so the
        advertised ``manuallyApprovesFollowers`` flag is actually
        enforced.
    :param document_cache: Optional :class:`pubby.cache.DocumentCache`
        used by the framework adapters for dereference endpoints. The
        handler only needs it to invalidate the actor, WebFinger and
        collection entries when local state changes (actor updates,
        published activities, followers gained or lost). Adapters fall
        back to this attribute when ``bind_activitypub`` receives no
        explicit cache.
    """

    def __init__(
        self,
        storage: ActivityPubStorage,
        actor_config: Union[ActorConfig, dict],
        *,
        private_key: Optional[Union[rsa.RSAPrivateKey, str, bytes]] = None,
        private_key_path: Optional[Union[str, Path]] = None,
        on_interaction_received: Optional[Callable[[Interaction], None]] = None,
        webfinger_domain: Optional[str] = None,
        user_agent: Optional[str] = None,
        http_timeout: float = 15.0,
        max_retries: int = 3,
        max_delivery_workers: int = 10,
        auto_approve_quotes: bool = True,
        store_local_only: bool = False,
        local_base_urls: Optional[List[str]] = None,
        software_name: str = "pubby",
        software_version: Optional[str] = None,
        async_delivery: bool = True,
        allowed_instances: Optional[Collection[str]] = None,
        blocked_instances: Optional[Collection[str]] = None,
        deliver: Optional[Callable[[str, dict], None]] = None,
        strict_attribution: bool = False,
        follow_policy: Optional[
            Callable[[str, str], Optional[Union[FollowPolicy, str]]]
        ] = None,
        document_cache: Optional[_cache.DocumentCache] = None,
    ):
        self.storage = storage
        self.document_cache = document_cache  # property: propagates to processors

        # Accept dict or ActorConfig
        if isinstance(actor_config, dict):
            actor_config = ActorConfig.from_dict(actor_config)

        # Parse actor config
        self.base_url = actor_config.base_url
        self.username = actor_config.username
        self.actor_name = actor_config.name
        self.actor_summary = actor_config.summary
        self.icon_url = actor_config.icon_url
        self.actor_path = actor_config.actor_path
        self.actor_type = actor_config.type
        self.manually_approves = actor_config.manually_approves_followers
        self.actor_attachment = actor_config.attachment
        self.actor_url = actor_config.url or actor_config.base_url

        # Derived URLs
        self.actor_id = f"{self.base_url}{self.actor_path}"
        self.inbox_url = f"{self.base_url}/ap/inbox"
        self.outbox_url = f"{self.base_url}/ap/outbox"
        self.followers_url = f"{self.base_url}/ap/followers"
        self.following_url = f"{self.base_url}/ap/following"
        self.shared_inbox_url = f"{self.base_url}/ap/inbox"
        self.key_id = f"{self.actor_id}#main-key"

        # WebFinger domain
        from urllib.parse import urlparse

        parsed = urlparse(self.base_url)
        self.webfinger_domain = webfinger_domain or parsed.hostname or ""

        # Load private key
        if private_key is None and private_key_path is not None:
            pem_data = Path(private_key_path).read_text(encoding="utf-8")
            self._private_key = load_private_key(pem_data)
        elif isinstance(private_key, (str, bytes)):
            self._private_key = load_private_key(private_key)
        elif isinstance(private_key, rsa.RSAPrivateKey):
            self._private_key = private_key
        else:
            raise ValueError("Either private_key or private_key_path must be provided")

        self.public_key_pem = export_public_key_pem(self._private_key.public_key())

        # Software info
        self.software_name = software_name
        if software_version is None:
            from pubby import __version__

            software_version = __version__
        self.software_version = software_version

        # Sub-processors
        self.inbox = InboxProcessor(
            storage=storage,
            actor_id=self.actor_id,
            private_key=self._private_key,
            key_id=self.key_id,
            on_interaction_received=on_interaction_received,
            user_agent=user_agent,
            http_timeout=http_timeout,
            auto_approve_quotes=auto_approve_quotes,
            store_local_only=store_local_only,
            local_base_urls=local_base_urls,
            allowed_instances=allowed_instances,
            blocked_instances=blocked_instances,
            strict_attribution=strict_attribution,
            follow_policy=(
                follow_policy
                if follow_policy is not None
                else (
                    (lambda *_: FollowPolicy.MANUAL) if self.manually_approves else None
                )
            ),
            document_cache=document_cache,
        )

        self.outbox = OutboxProcessor(
            storage=storage,
            actor_id=self.actor_id,
            private_key=self._private_key,
            key_id=self.key_id,
            followers_collection_url=self.followers_url,
            max_retries=max_retries,
            max_delivery_workers=max_delivery_workers,
            user_agent=user_agent,
            http_timeout=http_timeout,
            async_delivery=async_delivery,
            allowed_instances=allowed_instances,
            blocked_instances=blocked_instances,
            deliver=deliver,
            document_cache=document_cache,
        )

        self.renderer = InteractionsRenderer()

    @property
    def document_cache(self) -> Optional[_cache.DocumentCache]:
        return self._document_cache

    @document_cache.setter
    def document_cache(self, cache: Optional[_cache.DocumentCache]) -> None:
        self._document_cache = cache
        # Propagate to the sub-processors so mutations they handle (incoming
        # follows/unfollows, published activities) invalidate the adapter's
        # cached documents — e.g. when the cache is attached through
        # ``bind_activitypub`` after the handler was constructed.
        for proc in (getattr(self, "inbox", None), getattr(self, "outbox", None)):
            if proc is not None:
                proc.document_cache = cache

    def _invalidate_cached_documents(self, *prefixes) -> None:
        """Drop cached dereference documents when local state changes."""
        cache = self.document_cache
        if cache is not None:
            cache.invalidate_prefix(*prefixes)

    # ---------- Inbox ----------

    def process_inbox_activity(
        self,
        activity_data: dict,
        method: str = "POST",
        path: str = "/ap/inbox",
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
        skip_verification: bool = False,
    ) -> Optional[dict]:
        """
        Process an incoming activity on the inbox.

        :param activity_data: Parsed activity JSON-LD.
        :param method: HTTP method of the incoming request.
        :param path: Request path.
        :param headers: Request headers. Required unless
            ``skip_verification`` is set — the verified signature is what
            authenticates ``activity.actor``.
        :param body: Raw request body.
        :param skip_verification: Skip HTTP signature verification.
        :return: Response data or None.
        """
        return self.inbox.process(
            activity_data,
            method=method,
            path=path,
            headers=headers,
            body=body,
            skip_verification=skip_verification,
        )

    # ---------- Outbox ----------

    def publish_object(self, obj: Object, activity_type: str = "Create") -> dict:
        """
        Publish an object to followers.

        :param obj: The Object to publish.
        :param activity_type: The activity type (``Create``, ``Update``, or
            ``Delete``).
        :return: The published activity dictionary.
        """
        if activity_type == "Create":
            activity = self.outbox.build_create_activity(obj)
        elif activity_type == "Update":
            activity = self.outbox.build_update_activity(obj)
        elif activity_type == "Delete":
            activity = self.outbox.build_delete_activity(obj.id)
        else:
            raise ValueError(f"Unsupported activity type: {activity_type}")

        return self.outbox.publish(activity)

    def publish_activity(self, activity: dict) -> dict:
        """
        Publish a pre-built activity to followers.

        Unlike :meth:`publish_object`, this method does not wrap the
        payload in a Create/Update envelope — it publishes the activity
        dict as-is. Use this for activity types that are not Object
        wrappers, such as ``Like``, ``Announce``, ``Undo``, and
        ``Follow``.

        :param activity: A complete JSON-LD activity dictionary.
        :return: The published activity dictionary.
        """
        return self.outbox.publish(activity)

    def get_outbox(self, limit: int = 20, offset: int = 0) -> dict:
        """
        Get the outbox collection.

        :param limit: Maximum number of items.
        :param offset: Pagination offset.
        :return: An OrderedCollection dictionary.
        """
        return self.outbox.get_outbox_collection(
            self.outbox_url, limit=limit, offset=offset
        )

    # ---------- Actor ----------

    def get_actor_document(self) -> dict:
        """
        Build the actor's JSON-LD document.

        :return: The actor document dictionary.
        """
        actor = Actor(
            id=self.actor_id,
            type=self.actor_type,
            preferred_username=self.username,
            name=self.actor_name or "",
            summary=self.actor_summary,
            inbox=self.inbox_url,
            outbox=self.outbox_url,
            followers=self.followers_url,
            following=self.following_url,
            icon=({"type": "Image", "url": self.icon_url} if self.icon_url else None),
            public_key_pem=self.public_key_pem,
            manually_approves_followers=self.manually_approves,
            discoverable=True,
            url=self.actor_url,
            endpoints={"sharedInbox": self.shared_inbox_url},
            attachment=self.actor_attachment,
        )

        return actor.to_dict()

    def publish_actor_update(self, document: Optional[dict] = None) -> dict:
        """
        Publish an ``Update`` activity for the actor itself.

        This pushes profile changes (name, summary, attachment/fields, icon,
        etc.) to all followers so remote instances refresh their cached copy.

        :param document: Optional prebuilt actor document used as the
            activity's ``object``.  When ``None`` (the default), the
            document is built from the handler's ``actor_config`` via
            :meth:`get_actor_document`.  Pass a custom document when the
            actor profile lives in your own models rather than in
            ``actor_config``.
        :return: The published activity dictionary.
        """
        actor_doc = document if document is not None else self.get_actor_document()
        # The actor document and everything derived from it (WebFinger
        # lookup, collection memberships) changed — drop cached copies so
        # the next dereference renders the updated profile.
        self._invalidate_cached_documents(
            _cache.actor_document_key(),
            _cache.route_key("webfinger"),
            _cache.route_key("outbox"),
            _cache.followers_key(),
            _cache.following_key(),
        )
        activity = {
            "@context": AP_CONTEXT,
            "id": f"{self.actor_id}#update-profile-{uuid.uuid4()}",
            "type": "Update",
            "actor": self.actor_id,
            "published": datetime.now(timezone.utc).isoformat(),
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
            "cc": [self.followers_url],
            "object": actor_doc,
        }

        return self.outbox.publish(activity)

    # ---------- Collections ----------

    def get_followers_collection(
        self,
        actor_id: Optional[str] = None,
    ) -> dict:
        """
        Build the followers OrderedCollection.

        :param actor_id: If provided, return followers of this actor.
            If None, return followers of the handler's configured actor.
        :return: The followers collection dictionary.

        Unassigned/legacy followers with an empty ``target_actor_id`` are
        included through ``get_followers(actor_id=...)`` for backward
        compatibility. Applications upgrading a single-actor deployment to
        multi-actor should backfill ``target_actor_id`` on existing followers
        to avoid them appearing in every actor's collection.
        """
        target = actor_id or self.actor_id
        followers = self.storage.get_followers(actor_id=target)
        return {
            "@context": AP_CONTEXT,
            "id": self.followers_url,
            "type": "OrderedCollection",
            "totalItems": len(followers),
            "orderedItems": [f.actor_id for f in followers],
        }

    def get_following_collection(self) -> dict:
        """
        Build the following OrderedCollection (empty for blogs).

        :return: The following collection dictionary.
        """
        return {
            "@context": AP_CONTEXT,
            "id": self.following_url,
            "type": "OrderedCollection",
            "totalItems": 0,
            "orderedItems": [],
        }

    # ---------- Discovery ----------

    def is_valid_webfinger_resource(self, resource: Optional[str]) -> bool:
        """
        Whether ``resource`` is a spelling of this actor's ``acct:`` URI
        that :meth:`get_webfinger_response` accepts.

        Accepted: ``acct:user@domain`` and ``acct:@user@domain``
        (case-insensitive). Rejected: bare ``user@domain``, a leading ``@``
        without the ``acct:`` scheme, surrounding whitespace — anything the
        renderer would treat differently from the canonical form. Adapters
        call this *before* cache lookup so a malformed request cannot
        negative-cache the valid account's entry under the same key.
        """
        if resource is None:
            return True
        normalized = resource
        if not normalized.lower().startswith("acct:"):
            return False
        normalized = normalized[5:]
        if normalized.startswith("@"):
            normalized = normalized[1:]
        expected = f"{self.username}@{self.webfinger_domain}"
        return normalized.lower() == expected.lower()

    def get_webfinger_response(self, resource: Optional[str] = None) -> Optional[dict]:
        """
        Build the WebFinger response for the configured actor.

        :param resource: The requested ``acct:`` resource. If provided,
            validates that it matches the configured actor.
        :return: The JRD response or None if the resource doesn't match.
        """
        if resource is not None and not self.is_valid_webfinger_resource(resource):
            return None

        return build_webfinger_response(
            username=self.username,
            domain=self.webfinger_domain,
            actor_url=self.actor_id,
        )

    def get_nodeinfo_discovery(self) -> dict:
        """
        Build the NodeInfo well-known discovery document.

        :return: The discovery document.
        """
        return build_nodeinfo_discovery(self.base_url)

    def get_nodeinfo_document(self) -> dict:
        """
        Build the NodeInfo 2.1 document.

        :return: The NodeInfo document.
        """
        activities = self.storage.get_activities(limit=10000, offset=0)
        return build_nodeinfo_document(
            software_name=self.software_name,
            software_version=self.software_version,
            total_posts=len(activities),
        )

    # ---------- Quote authorizations ----------

    def get_quote_authorization(self, authorization_id: str) -> Optional[dict]:
        """
        Retrieve a stored QuoteAuthorization by its full ID/URL.

        :param authorization_id: The authorization's ID (URL).
        :return: The JSON-LD document, or None if not found.
        """
        return self.storage.get_quote_authorization(authorization_id)

    # ---------- Rendering ----------

    def render_interaction(
        self,
        interaction: Interaction,
        template: Optional[Union[str, Path, Template]] = None,
    ) -> Markup:
        """
        Render a single interaction as HTML.

        :param interaction: The interaction to render.
        :param template: Optional custom template.
        :return: The rendered HTML markup.
        """
        return self.renderer.render_interaction(interaction, template=template)

    def render_interactions(
        self,
        interactions: Collection[Interaction],
        template: Optional[Union[str, Path, Template]] = None,
    ) -> Markup:
        """
        Render a list of interactions as HTML.

        :param interactions: The interactions to render.
        :param template: Optional custom template.
        :return: The rendered HTML markup.
        """
        return self.renderer.render_interactions(interactions, template=template)
