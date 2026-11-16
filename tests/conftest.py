import asyncio
import os
import sys

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from stackscan import db

TEST_DSN = os.environ.get("TEST_DATABASE_URL")
_created = False


@pytest.fixture(scope="session")
def event_loop_policy():
    # psycopg's async mode cannot run on Windows' default Proactor loop.
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


def _ensure_database(dsn: str) -> None:
    """Create the test database on first use (CI services create it themselves)."""
    global _created
    if _created:
        return
    name = conninfo_to_dict(dsn)["dbname"]
    with psycopg.connect(make_conninfo(dsn, dbname="postgres"), autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    _created = True


@pytest.fixture
async def pool():
    if not TEST_DSN:
        pytest.skip("set TEST_DATABASE_URL to run database tests")
    _ensure_database(TEST_DSN)
    p = db.open_pool(TEST_DSN, max_size=6)
    await p.open(wait=True, timeout=15)
    await db.migrate(p)
    async with p.connection() as conn:
        await conn.execute("TRUNCATE domains, crawl_jobs, fetches, detections RESTART IDENTITY CASCADE")
    yield p
    await p.close()
