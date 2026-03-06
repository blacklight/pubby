import sqlalchemy as sa
from sqlalchemy.orm import declarative_base, sessionmaker

from ._model import DbActivity, DbActorCache, DbFollower, DbInteraction
from ._storage import DbActivityPubStorage


def init_db_storage(
    engine: str | sa.Engine,
    *args,
    followers_table: str = "ap_followers",
    interactions_table: str = "ap_interactions",
    activities_table: str = "ap_activities",
    actor_cache_table: str = "ap_actor_cache",
    **kwargs,
) -> DbActivityPubStorage:
    """
    Helper function that initializes a database storage for ActivityPub.

    Use this if you want to use a dedicated SQLAlchemy engine.
    Otherwise, extend the Db* models with your own table names, register
    them to your engine, and initialize a ``DbActivityPubStorage`` directly.

    :param engine: SQLAlchemy engine (string URL or Engine instance).
    :param followers_table: Table name for followers.
    :param interactions_table: Table name for interactions.
    :param activities_table: Table name for activities.
    :param actor_cache_table: Table name for the actor cache.
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

    if isinstance(engine, str):
        engine = sa.create_engine(engine, *args, **kwargs)

    Base.metadata.create_all(engine)
    return DbActivityPubStorage(
        engine=engine,
        follower_model=DefaultDbFollower,
        interaction_model=DefaultDbInteraction,
        activity_model=DefaultDbActivity,
        actor_cache_model=DefaultDbActorCache,
        session_factory=sessionmaker(bind=engine),
    )
