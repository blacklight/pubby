import threading
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
    create_tables: bool = True,
    **kwargs,
) -> DbActivityPubStorage:
    """
    Helper function that initializes a database storage for ActivityPub.

    Use this if you want to use a dedicated SQLAlchemy engine.
    Otherwise, extend the Db* models with your own table names, register
    them to your engine, and initialize a ``DbActivityPubStorage`` directly.

    The returned storage owns its engine and connection pool: create it
    once at startup and reuse the instance for the lifetime of the process
    (or use :func:`get_db_storage`, which memoizes by configuration). Each
    ``init_db_storage`` call builds a new engine — and, unless
    ``create_tables`` is disabled, runs ``create_all`` — so calling it per
    request leaks a connection pool per call.

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
    :param create_tables: Run ``Base.metadata.create_all`` on the engine
        (default ``True``). Pass ``False`` when tables are managed by the
        application's own migrations, or to skip the DDL round-trip when a
        storage is (re)created against an already-migrated database.
    :param args: Positional arguments for ``sa.create_engine``.
    :param kwargs: Keyword arguments for ``sa.create_engine``. Pool-bounding
        options (``pool_size``, ``max_overflow``, ``pool_timeout``,
        ``pool_recycle``, ``pool_pre_ping``) are worth passing explicitly
        on server databases: the default ``QueuePool`` size is
        ``pool_size=5, max_overflow=10`` per storage instance.
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

    if create_tables:
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


# Storage instances memoized by :func:`get_db_storage`, keyed on the full
# initialization configuration (engine URL + create_engine args + table
# names). Each storage owns an engine with a connection pool, so repeated
# ``init_db_storage`` calls would leak one pool per call — enough to exhaust
# the database's ``max_connections`` during a federation traffic burst on
# its own.
_storage_cache: dict = {}
_storage_lock = threading.Lock()


def _storage_cache_key(engine, args, kwargs) -> tuple:
    engine_key = engine if isinstance(engine, str) else id(engine)
    return (
        engine_key,
        repr(args),
        repr(sorted((k, repr(v)) for k, v in kwargs.items())),
    )


def get_db_storage(engine: str | sa.Engine, *args, **kwargs) -> DbActivityPubStorage:
    """
    Return a shared ``DbActivityPubStorage``, creating it on first use.

    Memoized variant of :func:`init_db_storage`: all arguments are forwarded
    verbatim (table names, ``driver_map``, ``create_tables``, engine kwargs
    — including pool bounds such as ``pool_size``/``max_overflow``), and the
    result is cached per unique argument combination. Tests or applications
    that need a fresh backend can reset with :func:`reset_db_storage`.

    :param engine: SQLAlchemy engine (string URL or Engine instance).
    :return: The shared ``DbActivityPubStorage`` for the given arguments.
    """
    key = _storage_cache_key(engine, args, kwargs)
    if key not in _storage_cache:
        with _storage_lock:
            if key not in _storage_cache:
                _storage_cache[key] = init_db_storage(engine, *args, **kwargs)
    return _storage_cache[key]


def reset_db_storage() -> None:
    """Drop every memoized storage instance, disposing their engines.

    For tests and config reloads: the dropped storages' connection pools
    are disposed so they do not keep database connections open past their
    usefulness.
    """
    with _storage_lock:
        storages = list(_storage_cache.values())
        _storage_cache.clear()
    for storage in storages:
        storage.engine.dispose()
