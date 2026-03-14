"""
Main ActivityPub handler — analogous to WebmentionsHandler.
"""

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Collection

from cryptography.hazmat.primitives.asymmetric import rsa
from jinja2 import Template
from markupsafe import Markup

from .._model import (
    Actor,
    ActorConfig,
    Interaction,
    Object,
    AP_CONTEXT,
)
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
    :param software_version: Software version for NodeInfo.
    """

    def __init__(
        self,
        storage: ActivityPubStorage,
        actor_config: ActorConfig | dict,
        *,
        private_key: rsa.RSAPrivateKey | str | bytes | None = None,
        private_key_path: str | Path | None = None,
        on_interaction_received: Callable[[Interaction], None] | None = None,
        webfinger_domain: str | None = None,
        user_agent: str = "pubby/0.0.1",
        http_timeout: float = 15.0,
        max_retries: int = 3,
        max_delivery_workers: int = 10,
        auto_approve_quotes: bool = True,
        store_local_only: bool = False,
        local_base_urls: list[str] | None = None,
        software_name: str = "pubby",
        software_version: str = "0.0.1",
    ):
        self.storage = storage

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
        )

        self.renderer = InteractionsRenderer()

    # ---------- Inbox ----------

    def process_inbox_activity(
        self,
        activity_data: dict,
        method: str = "POST",
        path: str = "/ap/inbox",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        skip_verification: bool = False,
    ) -> dict | None:
        """
        Process an incoming activity on the inbox.

        :param activity_data: Parsed activity JSON-LD.
        :param method: HTTP method of the incoming request.
        :param path: Request path.
        :param headers: Request headers.
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

    def publish_actor_update(self) -> dict:
        """
        Publish an ``Update`` activity for the actor itself.

        This pushes profile changes (name, summary, attachment/fields, icon,
        etc.) to all followers so remote instances refresh their cached copy.

        :return: The published activity dictionary.
        """
        actor_doc = self.get_actor_document()
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

    def get_followers_collection(self) -> dict:
        """
        Build the followers OrderedCollection.

        :return: The followers collection dictionary.
        """
        followers = self.storage.get_followers()
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

    def get_webfinger_response(self, resource: str | None = None) -> dict | None:
        """
        Build the WebFinger response for the configured actor.

        :param resource: The requested ``acct:`` resource. If provided,
            validates that it matches the configured actor.
        :return: The JRD response or None if the resource doesn't match.
        """
        expected = f"acct:{self.username}@{self.webfinger_domain}"

        if resource is not None:
            # Some clients incorrectly include a leading '@' in the acct user
            # part (e.g. 'acct:@user@example.com'). Accept both forms.
            normalized = resource
            if normalized.lower().startswith("acct:@"):
                normalized = "acct:" + normalized[6:]

            if normalized.lower() != expected.lower():
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

    def get_quote_authorization(self, authorization_id: str) -> dict | None:
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
        template: str | Path | Template | None = None,
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
        template: str | Path | Template | None = None,
    ) -> Markup:
        """
        Render a list of interactions as HTML.

        :param interactions: The interactions to render.
        :param template: Optional custom template.
        :return: The rendered HTML markup.
        """
        return self.renderer.render_interactions(interactions, template=template)
