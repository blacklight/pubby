"""
Tests for the SQLAlchemy storage helpers — async→sync URL bridging.
"""

import pytest

from pubby.storage.adapters.db import (
    DbActivityPubStorage,
    init_db_storage,
    to_sync_url,
)


class TestToSyncUrl:
    def test_aiosqlite_maps_to_sqlite(self):
        assert (
            to_sync_url("sqlite+aiosqlite:////tmp/pubby.db")
            == "sqlite:////tmp/pubby.db"
        )

    def test_asyncpg_maps_to_psycopg2(self):
        assert (
            to_sync_url("postgresql+asyncpg://user:pass@db.example.com/mydb")
            == "postgresql+psycopg2://user:pass@db.example.com/mydb"
        )

    @pytest.mark.parametrize(
        "url",
        [
            "sqlite:///:memory:",
            "sqlite:////tmp/pubby.db",
            "postgresql://user:pass@db.example.com/mydb",
            "postgresql+psycopg2://user:pass@db.example.com/mydb",
            "postgresql+psycopg://user:pass@db.example.com/mydb",
            "mysql+pymysql://user:pass@db.example.com/mydb",
        ],
    )
    def test_sync_urls_pass_through_unchanged(self, url):
        assert to_sync_url(url) == url

    def test_unknown_async_driver_raises(self):
        with pytest.raises(ValueError, match="asyncmy"):
            to_sync_url("mysql+asyncmy://user:pass@db.example.com/mydb")

    def test_driver_map_extends_defaults(self):
        assert (
            to_sync_url(
                "mysql+aiomysql://user:pass@db.example.com/mydb",
                driver_map={"mysql+aiomysql": "mysql+pymysql"},
            )
            == "mysql+pymysql://user:pass@db.example.com/mydb"
        )

    def test_driver_map_overrides_defaults(self):
        assert (
            to_sync_url(
                "postgresql+asyncpg://user:pass@db.example.com/mydb",
                driver_map={"postgresql+asyncpg": "postgresql+psycopg"},
            )
            == "postgresql+psycopg://user:pass@db.example.com/mydb"
        )

    def test_password_is_preserved(self):
        url = to_sync_url("postgresql+asyncpg://user:s3cret@db.example.com/mydb")
        assert "user:s3cret@db.example.com" in url

    def test_init_db_storage_accepts_async_url(self):
        """A known async driver URL is converted before create_engine."""
        storage = init_db_storage("sqlite+aiosqlite:///:memory:")
        assert isinstance(storage, DbActivityPubStorage)

    def test_init_db_storage_rejects_unknown_async_url(self):
        with pytest.raises(ValueError, match="aiomysql"):
            init_db_storage("mysql+aiomysql://user:pass@db.example.com/mydb")
