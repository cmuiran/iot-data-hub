from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import pytest


def pytest_asyncio_loop_factories(config: Any, item: Any) -> Mapping[str, Callable[[], Any]]:
    # aiomqtt (paho) and psycopg's async mode need the selector loop on Windows;
    # the hub itself runs on it too (hub.runtime.run).
    from hub.runtime import new_event_loop

    return {"hub": new_event_loop}


# --- PostgreSQL ---------------------------------------------------------------------------------
# HUB_TEST_DATABASE_URL: a real server (e.g. with TimescaleDB) where the user may create databases.
# Otherwise pgserver starts a throw-away PostgreSQL 16 for the session (no install needed).


@pytest.fixture(scope="session")
def pg_admin_url() -> Iterator[str]:
    url = os.environ.get("HUB_TEST_DATABASE_URL")
    if url:
        yield url
        return
    pgserver = pytest.importorskip("pgserver")
    datadir = tempfile.mkdtemp(prefix="hub-pg-")
    server = pgserver.get_server(datadir, cleanup_mode="delete")
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


@pytest.fixture
def database_url(pg_admin_url: str) -> Iterator[str]:
    """A fresh, fully migrated database for one test."""
    import psycopg
    from psycopg.conninfo import make_conninfo

    from hub.storage.migrate import migrate

    name = f"hub_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = make_conninfo(pg_admin_url, dbname=name)
    try:
        migrate(url)
        yield url
    finally:
        with psycopg.connect(pg_admin_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
async def store(database_url: str) -> Any:
    from hub.storage.postgres import PostgresStore

    async with PostgresStore(database_url, max_size=4) as s:
        yield s
