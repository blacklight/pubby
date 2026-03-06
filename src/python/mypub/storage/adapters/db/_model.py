from datetime import datetime, timezone

import sqlalchemy as sa

from ...._model import (
    Follower,
    Interaction,
    InteractionStatus,
    InteractionType,
)


class DbFollower:
    """
    SQLAlchemy base model for followers.

    Inherit this in a mapped model with a ``__tablename__``.
    """

    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    actor_id = sa.Column(sa.String, nullable=False, unique=True)
    inbox = sa.Column(sa.String, nullable=False)
    shared_inbox = sa.Column(sa.String, nullable=False, default="")
    followed_at = sa.Column(sa.DateTime, nullable=False)
    actor_data = sa.Column(sa.JSON, nullable=False, default=dict)

    def __init__(self, *_, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    @classmethod
    def columns(cls) -> set[str]:
        return {c.name for c in cls.__table__.columns}  # type: ignore

    @classmethod
    def from_follower(cls, follower: Follower) -> "DbFollower":
        return cls(
            actor_id=follower.actor_id,
            inbox=follower.inbox,
            shared_inbox=follower.shared_inbox,
            followed_at=follower.followed_at or datetime.now(timezone.utc),
            actor_data=follower.actor_data or {},
        )

    def to_follower(self) -> Follower:
        return Follower(
            actor_id=self.actor_id,  # type: ignore
            inbox=self.inbox,  # type: ignore
            shared_inbox=self.shared_inbox or "",  # type: ignore
            followed_at=self.followed_at,  # type: ignore
            actor_data=dict(self.actor_data) if self.actor_data else {},  # type: ignore
        )


class DbInteraction:
    """
    SQLAlchemy base model for interactions.
    """

    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    source_actor_id = sa.Column(sa.String, nullable=False)
    target_resource = sa.Column(sa.String, nullable=False)
    interaction_type = sa.Column(
        sa.Enum(InteractionType, name="interaction_type"), nullable=False
    )
    activity_id = sa.Column(sa.String, nullable=False, default="")
    object_id = sa.Column(sa.String, nullable=False, default="")
    content = sa.Column(sa.String, nullable=False, default="")
    author_name = sa.Column(sa.String, nullable=False, default="")
    author_url = sa.Column(sa.String, nullable=False, default="")
    author_photo = sa.Column(sa.String, nullable=False, default="")
    published = sa.Column(sa.DateTime, nullable=True)
    status = sa.Column(
        sa.Enum(InteractionStatus, name="interaction_status"),
        nullable=False,
        default=InteractionStatus.CONFIRMED,
    )
    meta = sa.Column(sa.JSON, nullable=False, default=dict)
    created_at = sa.Column(sa.DateTime, nullable=False)
    updated_at = sa.Column(sa.DateTime, nullable=False)

    __table_args__ = (
        sa.UniqueConstraint(
            "source_actor_id",
            "target_resource",
            "interaction_type",
            name="uix_actor_resource_type",
        ),
    )

    def __init__(self, *_, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    @classmethod
    def columns(cls) -> set[str]:
        return {c.name for c in cls.__table__.columns}  # type: ignore

    @classmethod
    def from_interaction(cls, interaction: Interaction) -> "DbInteraction":
        now = datetime.now(timezone.utc)
        return cls(
            source_actor_id=interaction.source_actor_id,
            target_resource=interaction.target_resource,
            interaction_type=interaction.interaction_type,
            activity_id=interaction.activity_id,
            object_id=interaction.object_id,
            content=interaction.content,
            author_name=interaction.author_name,
            author_url=interaction.author_url,
            author_photo=interaction.author_photo,
            published=interaction.published,
            status=interaction.status,
            meta=interaction.metadata or {},
            created_at=interaction.created_at or now,
            updated_at=interaction.updated_at or now,
        )

    def to_interaction(self) -> Interaction:
        return Interaction(
            source_actor_id=self.source_actor_id,  # type: ignore
            target_resource=self.target_resource,  # type: ignore
            interaction_type=InteractionType(self.interaction_type),  # type: ignore
            activity_id=self.activity_id or "",  # type: ignore
            object_id=self.object_id or "",  # type: ignore
            content=self.content or "",  # type: ignore
            author_name=self.author_name or "",  # type: ignore
            author_url=self.author_url or "",  # type: ignore
            author_photo=self.author_photo or "",  # type: ignore
            published=self.published,  # type: ignore
            status=InteractionStatus(self.status) if self.status else InteractionStatus.CONFIRMED,  # type: ignore
            metadata=dict(self.meta) if self.meta else {},  # type: ignore
            created_at=self.created_at,  # type: ignore
            updated_at=self.updated_at,  # type: ignore
        )


class DbActivity:
    """
    SQLAlchemy base model for outbound activities.
    """

    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    activity_id = sa.Column(sa.String, nullable=False, unique=True)
    activity_data = sa.Column(sa.JSON, nullable=False)
    created_at = sa.Column(sa.DateTime, nullable=False)

    def __init__(self, *_, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    @classmethod
    def columns(cls) -> set[str]:
        return {c.name for c in cls.__table__.columns}  # type: ignore


class DbActorCache:
    """
    SQLAlchemy base model for cached remote actors.
    """

    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    actor_id = sa.Column(sa.String, nullable=False, unique=True)
    actor_data = sa.Column(sa.JSON, nullable=False)
    fetched_at = sa.Column(sa.DateTime, nullable=False)

    def __init__(self, *_, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    @classmethod
    def columns(cls) -> set[str]:
        return {c.name for c in cls.__table__.columns}  # type: ignore
