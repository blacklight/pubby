from datetime import datetime, timezone
from typing import Callable

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...._model import (
    Follower,
    Interaction,
    InteractionStatus,
    InteractionType,
)
from ..._base import ActivityPubStorage
from ._model import DbActivity, DbActorCache, DbFollower, DbInteraction


class DbActivityPubStorage(ActivityPubStorage):
    """
    SQLAlchemy-based storage backend for ActivityPub data.

    :param engine: SQLAlchemy engine.
    :param follower_model: Mapped model inheriting from DbFollower.
    :param interaction_model: Mapped model inheriting from DbInteraction.
    :param activity_model: Mapped model inheriting from DbActivity.
    :param actor_cache_model: Mapped model inheriting from DbActorCache.
    :param session_factory: SQLAlchemy session factory.
    """

    def __init__(
        self,
        engine: sa.Engine,
        follower_model: type[DbFollower],
        interaction_model: type[DbInteraction],
        activity_model: type[DbActivity],
        actor_cache_model: type[DbActorCache],
        session_factory: Callable[[], Session],
        *_,
        **__,
    ):
        self.engine = engine
        self.session_factory = session_factory
        self.follower_model = follower_model
        self.interaction_model = interaction_model
        self.activity_model = activity_model
        self.actor_cache_model = actor_cache_model

    # ---------- Followers ----------

    def store_follower(self, follower: Follower):
        session = self.session_factory()
        try:
            try:
                session.add(self.follower_model.from_follower(follower))
                session.commit()
                return
            except IntegrityError:
                session.rollback()

            existing = (
                session.query(self.follower_model)
                .filter(self.follower_model.actor_id == follower.actor_id)
                .one_or_none()
            )

            if existing is None:
                session.add(self.follower_model.from_follower(follower))
                session.commit()
                return

            existing.inbox = follower.inbox
            existing.shared_inbox = follower.shared_inbox
            existing.actor_data = follower.actor_data
            session.commit()
        finally:
            session.close()

    def remove_follower(self, actor_id: str):
        session = self.session_factory()
        try:
            session.query(self.follower_model).filter(
                self.follower_model.actor_id == actor_id
            ).delete(synchronize_session=False)
            session.commit()
        finally:
            session.close()

    def get_followers(self) -> list[Follower]:
        session = self.session_factory()
        try:
            return [
                row.to_follower()
                for row in session.query(self.follower_model).all()
            ]
        finally:
            session.close()

    # ---------- Interactions ----------

    def store_interaction(self, interaction: Interaction):
        session = self.session_factory()
        try:
            try:
                session.add(
                    self.interaction_model.from_interaction(interaction)
                )
                session.commit()
                return
            except IntegrityError:
                session.rollback()

            existing = (
                session.query(self.interaction_model)
                .filter(
                    sa.and_(
                        self.interaction_model.source_actor_id
                        == interaction.source_actor_id,
                        self.interaction_model.target_resource
                        == interaction.target_resource,
                        self.interaction_model.interaction_type
                        == interaction.interaction_type,
                    )
                )
                .one_or_none()
            )

            if existing is None:
                session.add(
                    self.interaction_model.from_interaction(interaction)
                )
                session.commit()
                return

            existing.activity_id = interaction.activity_id
            existing.object_id = interaction.object_id
            existing.content = interaction.content
            existing.author_name = interaction.author_name
            existing.author_url = interaction.author_url
            existing.author_photo = interaction.author_photo
            existing.published = interaction.published
            existing.status = interaction.status
            existing.meta = interaction.metadata or {}
            existing.updated_at = datetime.now(timezone.utc)
            session.commit()
        finally:
            session.close()

    def delete_interaction(
        self,
        source_actor_id: str,
        target_resource: str,
        interaction_type: InteractionType,
    ):
        session = self.session_factory()
        try:
            existing = (
                session.query(self.interaction_model)
                .filter(
                    sa.and_(
                        self.interaction_model.source_actor_id == source_actor_id,
                        self.interaction_model.target_resource == target_resource,
                        self.interaction_model.interaction_type == interaction_type,
                    )
                )
                .one_or_none()
            )
            if existing is not None:
                existing.status = InteractionStatus.DELETED
                existing.updated_at = datetime.now(timezone.utc)
                session.commit()
        finally:
            session.close()

    def get_interactions(
        self,
        target_resource: str,
        interaction_type: InteractionType | None = None,
        status: InteractionStatus = InteractionStatus.CONFIRMED,
    ) -> list[Interaction]:
        session = self.session_factory()
        try:
            query = session.query(self.interaction_model).filter(
                sa.and_(
                    self.interaction_model.target_resource == target_resource,
                    self.interaction_model.status == status,
                )
            )
            if interaction_type is not None:
                query = query.filter(
                    self.interaction_model.interaction_type == interaction_type
                )
            return [row.to_interaction() for row in query.all()]
        finally:
            session.close()

    # ---------- Activities ----------

    def store_activity(self, activity_id: str, activity_data: dict):
        session = self.session_factory()
        try:
            try:
                session.add(
                    self.activity_model(
                        activity_id=activity_id,
                        activity_data=activity_data,
                        created_at=datetime.now(timezone.utc),
                    )
                )
                session.commit()
                return
            except IntegrityError:
                session.rollback()

            existing = (
                session.query(self.activity_model)
                .filter(self.activity_model.activity_id == activity_id)
                .one_or_none()
            )

            if existing is not None:
                existing.activity_data = activity_data
                session.commit()
            else:
                session.add(
                    self.activity_model(
                        activity_id=activity_id,
                        activity_data=activity_data,
                        created_at=datetime.now(timezone.utc),
                    )
                )
                session.commit()
        finally:
            session.close()

    def get_activities(
        self,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        session = self.session_factory()
        try:
            rows = (
                session.query(self.activity_model)
                .order_by(self.activity_model.created_at.desc())
                .offset(offset)
                .limit(limit)
                .all()
            )
            return [dict(row.activity_data) for row in rows]
        finally:
            session.close()

    # ---------- Actor cache ----------

    def cache_remote_actor(
        self,
        actor_id: str,
        actor_data: dict,
        fetched_at: datetime | None = None,
    ):
        now = fetched_at or datetime.now(timezone.utc)
        session = self.session_factory()
        try:
            try:
                session.add(
                    self.actor_cache_model(
                        actor_id=actor_id,
                        actor_data=actor_data,
                        fetched_at=now,
                    )
                )
                session.commit()
                return
            except IntegrityError:
                session.rollback()

            existing = (
                session.query(self.actor_cache_model)
                .filter(self.actor_cache_model.actor_id == actor_id)
                .one_or_none()
            )

            if existing is not None:
                existing.actor_data = actor_data
                existing.fetched_at = now
                session.commit()
            else:
                session.add(
                    self.actor_cache_model(
                        actor_id=actor_id,
                        actor_data=actor_data,
                        fetched_at=now,
                    )
                )
                session.commit()
        finally:
            session.close()

    def get_cached_actor(
        self,
        actor_id: str,
        max_age_seconds: float = 86400.0,
    ) -> dict | None:
        session = self.session_factory()
        try:
            row = (
                session.query(self.actor_cache_model)
                .filter(self.actor_cache_model.actor_id == actor_id)
                .one_or_none()
            )
            if row is None:
                return None

            age = (
                datetime.now(timezone.utc) - row.fetched_at.replace(tzinfo=timezone.utc)
            ).total_seconds()
            if age > max_age_seconds:
                return None

            return dict(row.actor_data)
        finally:
            session.close()
