from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ---------- Enums ----------


class ActivityType(str, Enum):
    """Supported ActivityPub activity types."""

    CREATE = "Create"
    UPDATE = "Update"
    DELETE = "Delete"
    FOLLOW = "Follow"
    UNDO = "Undo"
    ACCEPT = "Accept"
    REJECT = "Reject"
    LIKE = "Like"
    ANNOUNCE = "Announce"

    @classmethod
    def from_raw(cls, raw: str) -> "ActivityType":
        normalized = raw.strip()
        for member in cls:
            if member.value.lower() == normalized.lower():
                return member
        raise ValueError(f"Unknown activity type: {raw}")


class ObjectType(str, Enum):
    """Supported ActivityPub object types."""

    NOTE = "Note"
    ARTICLE = "Article"
    IMAGE = "Image"
    VIDEO = "Video"
    AUDIO = "Audio"
    PAGE = "Page"
    EVENT = "Event"
    TOMBSTONE = "Tombstone"

    @classmethod
    def from_raw(cls, raw: str) -> "ObjectType":
        normalized = raw.strip()
        for member in cls:
            if member.value.lower() == normalized.lower():
                return member
        raise ValueError(f"Unknown object type: {raw}")


class InteractionType(str, Enum):
    """Types of interactions received from the fediverse."""

    REPLY = "reply"
    LIKE = "like"
    BOOST = "boost"
    MENTION = "mention"

    @classmethod
    def from_activity_type(cls, activity_type: str) -> "InteractionType":
        mapping = {
            "create": cls.REPLY,
            "like": cls.LIKE,
            "announce": cls.BOOST,
        }
        result = mapping.get(activity_type.lower())
        if result is None:
            raise ValueError(
                f"Cannot map activity type '{activity_type}' to interaction type"
            )
        return result


class InteractionStatus(str, Enum):
    """Status of a stored interaction."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    DELETED = "deleted"


class DeliveryStatus(str, Enum):
    """Status of outbound activity delivery."""

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


# ---------- Helper ----------


def _normalize(value: Any) -> Any:
    """Recursively normalize a value for JSON serialization."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, tuple):
        return [_normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    return value


def _parse_language(data: dict) -> str | None:
    """Extract language from ``contentMap`` keys or explicit ``language`` field."""
    content_map = data.get("contentMap")
    if isinstance(content_map, dict) and content_map:
        return next(iter(content_map))
    return data.get("language")


def _parse_dt(value: object) -> datetime | None:
    """Parse a datetime from various input formats."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        # Python <3.11 doesn't accept trailing 'Z' in fromisoformat
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------- Dataclasses ----------


AP_CONTEXT = [
    "https://www.w3.org/ns/activitystreams",
    "https://w3id.org/security/v1",
    "https://w3id.org/fep/0449",
]


@dataclass
class ActorConfig:
    """
    Configuration for an ActivityPub actor.

    :param base_url: Public base URL of your site (e.g. ``https://example.com``).
    :param username: Actor username, used in WebFinger ``acct:`` URIs.
    :param name: Display name shown on remote instances.
    :param summary: Short bio / description (HTML allowed).
    :param icon_url: URL to an avatar image.
    :param actor_path: URL path to the actor endpoint (appended to *base_url*).
    :param type: ActivityPub actor type (``Person``, ``Application``, ``Service``, etc.).
    :param manually_approves_followers: If ``True``, follow requests require
        explicit approval.
    """

    base_url: str
    username: str = "blog"
    name: str | None = None
    summary: str = ""
    icon_url: str = ""
    actor_path: str = "/ap/actor"
    type: str = "Person"
    manually_approves_followers: bool = False

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if self.name is None:
            self.name = self.username

    @classmethod
    def from_dict(cls, d: dict) -> "ActorConfig":
        """Create from a plain dict (backwards compatibility)."""
        return cls(
            base_url=d["base_url"],
            username=d.get("username", "blog"),
            name=d.get("name"),
            summary=d.get("summary", ""),
            icon_url=d.get("icon_url", ""),
            actor_path=d.get("actor_path", "/ap/actor"),
            type=d.get("type", "Person"),
            manually_approves_followers=d.get("manually_approves_followers", False),
        )


@dataclass
class Actor:
    """
    An ActivityPub Actor (Person, Application, Service, etc.).
    """

    id: str
    type: str = "Person"
    preferred_username: str = ""
    name: str = ""
    summary: str = ""
    inbox: str = ""
    outbox: str = ""
    followers: str = ""
    following: str = ""
    icon: dict | None = None
    public_key_pem: str = ""
    manually_approves_followers: bool = False
    discoverable: bool = True
    url: str = ""
    endpoints: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Return the ActivityPub JSON-LD representation."""
        doc: dict[str, Any] = {
            "@context": AP_CONTEXT,
            "id": self.id,
            "type": self.type,
            "preferredUsername": self.preferred_username,
            "name": self.name,
            "summary": self.summary,
            "inbox": self.inbox,
            "outbox": self.outbox,
            "followers": self.followers,
            "following": self.following,
            "url": self.url or self.id,
            "manuallyApprovesFollowers": self.manually_approves_followers,
            "discoverable": self.discoverable,
        }

        if self.icon:
            doc["icon"] = self.icon

        if self.public_key_pem:
            doc["publicKey"] = {
                "id": f"{self.id}#main-key",
                "owner": self.id,
                "publicKeyPem": self.public_key_pem,
            }

        if self.endpoints:
            doc["endpoints"] = self.endpoints

        return doc

    @classmethod
    def build(cls, data: dict) -> "Actor":
        """Build an Actor from an ActivityPub JSON-LD document."""
        public_key_pem = ""
        pk = data.get("publicKey")
        if isinstance(pk, dict):
            public_key_pem = pk.get("publicKeyPem", "")

        icon = data.get("icon")
        if isinstance(icon, str):
            icon = {"type": "Image", "url": icon}

        return cls(
            id=data.get("id", ""),
            type=data.get("type", "Person"),
            preferred_username=data.get("preferredUsername", ""),
            name=data.get("name", ""),
            summary=data.get("summary", ""),
            inbox=data.get("inbox", ""),
            outbox=data.get("outbox", ""),
            followers=data.get("followers", ""),
            following=data.get("following", ""),
            icon=icon,
            public_key_pem=public_key_pem,
            manually_approves_followers=data.get("manuallyApprovesFollowers", False),
            discoverable=data.get("discoverable", True),
            url=data.get("url", ""),
            endpoints=data.get("endpoints", {}),
        )


@dataclass
class Object:
    """
    An ActivityPub Object (Note, Article, Image, etc.).
    """

    id: str
    type: str = "Note"
    name: str | None = None
    content: str = ""
    url: str = ""
    attributed_to: str = ""
    in_reply_to: str | None = None
    published: datetime | None = None
    updated: datetime | None = None
    summary: str | None = None
    sensitive: bool = False
    tag: list[dict] = field(default_factory=list)
    attachment: list[dict] = field(default_factory=list)
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    media_type: str | None = None
    language: str | None = None
    quote_control: dict | None = None

    def to_dict(self) -> dict:
        """Return the ActivityPub JSON-LD representation."""
        doc: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "content": self.content,
            "attributedTo": self.attributed_to,
            "to": self.to,
            "cc": self.cc,
        }

        if self.language:
            doc["contentMap"] = {self.language: self.content}
            if self.summary is not None:
                doc["summaryMap"] = {self.language: self.summary}

        if self.name is not None:
            doc["name"] = self.name
        if self.url:
            doc["url"] = self.url
        if self.in_reply_to:
            doc["inReplyTo"] = self.in_reply_to
        if self.published:
            doc["published"] = self.published.isoformat()
        if self.updated:
            doc["updated"] = self.updated.isoformat()
        if self.summary is not None:
            doc["summary"] = self.summary
        if self.sensitive:
            doc["sensitive"] = True
        if self.tag:
            doc["tag"] = self.tag
        if self.attachment:
            doc["attachment"] = self.attachment
        if self.media_type:
            doc["mediaType"] = self.media_type
        if self.quote_control is not None:
            doc["quoteControl"] = self.quote_control

        return doc

    @classmethod
    def build(cls, data: dict) -> "Object":
        """Build an Object from an ActivityPub JSON-LD document."""
        if isinstance(data, str):
            return cls(id=data)

        return cls(
            id=data.get("id", ""),
            type=data.get("type", "Note"),
            name=data.get("name"),
            content=data.get("content", ""),
            url=data.get("url", ""),
            attributed_to=data.get("attributedTo", ""),
            in_reply_to=data.get("inReplyTo"),
            published=_parse_dt(data.get("published")),
            updated=_parse_dt(data.get("updated")),
            summary=data.get("summary"),
            sensitive=data.get("sensitive", False),
            tag=data.get("tag", []),
            attachment=data.get("attachment", []),
            to=(
                data.get("to", [])
                if isinstance(data.get("to"), list)
                else [data["to"]] if data.get("to") else []
            ),
            cc=(
                data.get("cc", [])
                if isinstance(data.get("cc"), list)
                else [data["cc"]] if data.get("cc") else []
            ),
            media_type=data.get("mediaType"),
            language=_parse_language(data),
            quote_control=data.get("quoteControl"),
        )


@dataclass
class Activity:
    """
    An ActivityPub Activity (Create, Follow, Like, etc.).
    """

    id: str
    type: str
    actor: str
    object: dict | str | None = None
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    published: datetime | None = None
    signature: dict | None = None

    def to_dict(self) -> dict:
        """Return the ActivityPub JSON-LD representation."""
        doc: dict[str, Any] = {
            "@context": AP_CONTEXT,
            "id": self.id,
            "type": self.type,
            "actor": self.actor,
            "to": self.to,
            "cc": self.cc,
        }

        if self.object is not None:
            if isinstance(self.object, dict):
                doc["object"] = self.object
            else:
                doc["object"] = self.object

        if self.published:
            doc["published"] = self.published.isoformat()
        if self.signature:
            doc["signature"] = self.signature

        return doc

    @classmethod
    def build(cls, data: dict) -> "Activity":
        """Build an Activity from an ActivityPub JSON-LD document."""
        to = data.get("to", [])
        if isinstance(to, str):
            to = [to]
        cc = data.get("cc", [])
        if isinstance(cc, str):
            cc = [cc]

        return cls(
            id=data.get("id", ""),
            type=data.get("type", ""),
            actor=data.get("actor", ""),
            object=data.get("object"),
            to=to,
            cc=cc,
            published=_parse_dt(data.get("published")),
            signature=data.get("signature"),
        )


@dataclass
class Interaction:
    """
    A stored interaction from a remote fediverse actor (reply, like, boost).

    Analogous to a Webmention — maps AP interactions to a displayable format.
    """

    source_actor_id: str
    target_resource: str
    interaction_type: InteractionType
    activity_id: str = ""
    object_id: str = ""
    content: str = ""
    author_name: str = ""
    author_url: str = ""
    author_photo: str = ""
    published: datetime | None = None
    status: InteractionStatus = InteractionStatus.CONFIRMED
    metadata: dict = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict:
        """Return a JSON-serializable dictionary."""
        return _normalize(asdict(self))

    def __hash__(self):
        return hash((self.source_actor_id, self.target_resource, self.interaction_type))

    @classmethod
    def build(cls, data: dict) -> "Interaction":
        """Build an Interaction from a dictionary."""
        interaction_type = data.get("interaction_type", InteractionType.MENTION)
        if isinstance(interaction_type, str):
            interaction_type = InteractionType(interaction_type)

        status = data.get("status", InteractionStatus.CONFIRMED)
        if isinstance(status, str):
            status = InteractionStatus(status)

        return cls(
            source_actor_id=data.get("source_actor_id", ""),
            target_resource=data.get("target_resource", ""),
            interaction_type=interaction_type,
            activity_id=data.get("activity_id", ""),
            object_id=data.get("object_id", ""),
            content=data.get("content", ""),
            author_name=data.get("author_name", ""),
            author_url=data.get("author_url", ""),
            author_photo=data.get("author_photo", ""),
            published=_parse_dt(data.get("published")),
            status=status,
            metadata=data.get("metadata", {}),
            created_at=_parse_dt(data.get("created_at")),
            updated_at=_parse_dt(data.get("updated_at")),
        )


@dataclass
class Follower:
    """
    A stored follower record.
    """

    actor_id: str
    inbox: str
    shared_inbox: str = ""
    followed_at: datetime | None = None
    actor_data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Return a JSON-serializable dictionary."""
        return _normalize(asdict(self))

    @classmethod
    def build(cls, data: dict) -> "Follower":
        """Build a Follower from a dictionary."""
        return cls(
            actor_id=data.get("actor_id", ""),
            inbox=data.get("inbox", ""),
            shared_inbox=data.get("shared_inbox", ""),
            followed_at=_parse_dt(data.get("followed_at")),
            actor_data=data.get("actor_data", {}),
        )
