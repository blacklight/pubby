from typing import Mapping

import sqlalchemy as sa
from sqlalchemy.orm import declarative_base, sessionmaker

from ._model import (
    DbActivity,
    DbActorCache,
    DbFollower,
    DbFollowRequest,
    DbInteraction,
)
from ._storage import DbActivityPubStorage

# Mapping from async SQLAlchemy drivers to their synchronous equivalents.
DEFAULT_ASYNC_DRIVER_MAP: Mapping[str, str] = {
    "sqlite+aiosqlite": "sqlite",
    "postgresql+asyncpg": "postgresql+psycopg2",
}


def _is_async_driver(drivername: str) -> bool:
    """Heuristic check for async DBAPI drivers (aiosqlite, asyncpg, …)."""
    driver = drivername.split("+", 1)[1] if "+" in drivername else ""
    return "async" in driver or driver.startswith("aio")


def to_sync_url(url: str, driver_map: Mapping[str, str] | None = None) -> str:
    """
    Return a sync-driver equivalent of an async SQLAlchemy URL.

    ``DbActivityPubStorage`` is synchronous; applications on an async
    database stack (``asyncpg``, ``aiosqlite``, …) can use this helper to
    derive a sync URL for :func:`init_db_storage`.

    Already-sync URLs are returned unchanged.  Async drivers with no entry
    in the (merged) driver map raise :exc:`ValueError`.

    :param url: SQLAlchemy database URL.
    :param driver_map: Optional map of ``async_driver -> sync_driver`` that
        extends or overrides :data:`DEFAULT_ASYNC_DRIVER_MAP` (e.g.
        ``{"postgresql+asyncpg": "postgresql+psycopg"}`` for psycopg3).
    :return: A SQLAlchemy URL using a synchronous driver.
    :raises ValueError: If the URL uses an unknown async driver.
    """
    parsed = sa.make_url(url)
    mapping = {**DEFAULT_ASYNC_DRIVER_MAP, **(driver_map or {})}

    if parsed.drivername in mapping:
        parsed = parsed.set(drivername=mapping[parsed.drivername])
        return parsed.render_as_string(hide_password=False)

    if _is_async_driver(parsed.drivername):
        raise ValueError(
            f"Unsupported async database driver: {parsed.drivername}. "
            "Pass a driver_map entry mapping it to a synchronous driver."
        )

    return url


def init_db_storage(
    engine: str | sa.Engine,
    *args,
    followers_table: str = "ap_followers",
    interactions_table: str = "ap_interactions",
    activities_table: str = "ap_activities",
    actor_cache_table: str = "ap_actor_cache",
    follow_requests_table: str = "ap_follow_requests",
    driver_map: Mapping[str, str] | None = None,
    **kwargs,
) -> DbActivityPubStorage:
    """
    Helper function that initializes a database storage for ActivityPub.

    Use this if you want to use a dedicated SQLAlchemy engine.
    Otherwise, extend the Db* models with your own table names, register
    them to your engine, and initialize a ``DbActivityPubStorage`` directly.

    :param engine: SQLAlchemy engine (string URL or Engine instance).
        String URLs that use a known async driver are converted to their
        sync equivalent via :func:`to_sync_url` before ``create_engine``.
    :param followers_table: Table name for followers.
    :param interactions_table: Table name for interactions.
    :param activities_table: Table name for activities.
    :param actor_cache_table: Table name for the actor cache.
    :param follow_requests_table: Table name for pending follow requests.
    :param driver_map: Optional async→sync driver overrides/extensions,
        forwarded to :func:`to_sync_url` when ``engine`` is a string URL.
    :param args: Positional arguments for ``sa.create_engine``.
    :param kwargs: Keyword arguments for ``sa.create_engine``.
    :return: Configured DbActivityPubStorage instance.
    """
    Base = declarative_base()

    class DefaultDbFollower(Base, DbFollower):  # type: ignore
        __tablename__ = followers_table

    class DefaultDbInteraction(Base, DbInteraction):  # type: ignore
        __tablename__ = interactions_table

    class DefaultDbActivity(Base, DbActivity):  # type: ignore
        __tablename__ = activities_table

    class DefaultDbActorCache(Base, DbActorCache):  # type: ignore
        __tablename__ = actor_cache_table

    class DefaultDbFollowRequest(Base, DbFollowRequest):  # type: ignore
        __tablename__ = follow_requests_table

    if isinstance(engine, str):
        engine = sa.create_engine(to_sync_url(engine, driver_map), *args, **kwargs)

    Base.metadata.create_all(engine)
    return DbActivityPubStorage(
        engine=engine,
        follower_model=DefaultDbFollower,
        interaction_model=DefaultDbInteraction,
        activity_model=DefaultDbActivity,
        actor_cache_model=DefaultDbActorCache,
        follow_request_model=DefaultDbFollowRequest,
        session_factory=sessionmaker(bind=engine),
    )
