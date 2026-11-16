"""PostgreSQL access: schema, the job queue and result storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from importlib import resources
from typing import TYPE_CHECKING

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

if TYPE_CHECKING:
    from stackscan.detector import Detection
    from stackscan.fetcher import FetchResult


@dataclass(frozen=True)
class Job:
    id: int
    domain_id: int
    domain: str
    attempts: int


def open_pool(dsn: str, *, max_size: int = 10) -> AsyncConnectionPool:
    return AsyncConnectionPool(dsn, min_size=1, max_size=max_size, open=False)


async def migrate(pool: AsyncConnectionPool) -> None:
    schema = resources.files("stackscan").joinpath("schema.sql").read_text("utf-8")
    async with pool.connection() as conn:
        await conn.execute(schema)


async def enqueue(pool: AsyncConnectionPool, names: list[str]) -> tuple[int, int]:
    """Add domains and queue a crawl for each. Returns (new domains, new jobs).

    Running it twice with the same names changes nothing the second time:
    the partial unique index allows only one open job per domain.
    """
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "INSERT INTO domains (name) SELECT unnest(%s::text[]) ON CONFLICT (name) DO NOTHING",
            (names,),
        )
        added = cur.rowcount
        cur = await conn.execute(
            """
            INSERT INTO crawl_jobs (domain_id)
            SELECT id FROM domains WHERE name = ANY(%s)
            ON CONFLICT (domain_id) WHERE status IN ('pending', 'running') DO NOTHING
            """,
            (names,),
        )
        return added, cur.rowcount


async def claim_jobs(pool: AsyncConnectionPool, limit: int, worker: str) -> list[Job]:
    """Atomically take up to `limit` due jobs. Safe under any number of concurrent workers."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            WITH picked AS (
                SELECT id FROM crawl_jobs
                WHERE status = 'pending' AND scheduled_at <= now()
                ORDER BY scheduled_at, id
                LIMIT %(limit)s
                FOR UPDATE SKIP LOCKED
            ), claimed AS (
                UPDATE crawl_jobs j
                SET status = 'running', started_at = now(),
                    attempts = j.attempts + 1, worker = %(worker)s
                FROM picked
                WHERE j.id = picked.id
                RETURNING j.id, j.domain_id, j.attempts
            )
            SELECT c.id, c.domain_id, d.name, c.attempts
            FROM claimed c JOIN domains d ON d.id = c.domain_id
            ORDER BY c.id
            """,
            {"limit": limit, "worker": worker},
        )
        return [Job(*row) for row in await cur.fetchall()]


async def open_job_count(pool: AsyncConnectionPool) -> int:
    """Jobs still pending, including retries scheduled for later."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM crawl_jobs WHERE status = 'pending'")
        row = await cur.fetchone()
        return row[0] if row else 0


async def requeue_stale(pool: AsyncConnectionPool, older_than: timedelta) -> int:
    """Return jobs left 'running' by a crashed worker to the queue."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE crawl_jobs SET status = 'pending', scheduled_at = now(), worker = NULL
            WHERE status = 'running' AND started_at < now() - %s
            """,
            (older_than,),
        )
        return cur.rowcount


async def record_result(
    pool: AsyncConnectionPool,
    job: Job,
    result: FetchResult,
    detections: list[Detection],
    *,
    outcome: str,
    retry_in: timedelta | None = None,
) -> None:
    """Store one fetch, its detections and the job's new state in a single transaction."""
    if outcome not in {"done", "failed", "retry"}:
        raise ValueError(f"unknown outcome {outcome!r}")
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            """
            INSERT INTO fetches (job_id, domain_id, url, final_url, http_status,
                                 elapsed_ms, body_bytes, error, headers)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                job.id,
                job.domain_id,
                result.url,
                result.final_url,
                result.status,
                result.elapsed_ms,
                result.body_bytes,
                result.error,
                Jsonb(result.headers),
            ),
        )
        row = await cur.fetchone()
        assert row is not None
        fetch_id = row[0]
        if detections:
            async with conn.cursor() as dcur:
                await dcur.executemany(
                    """
                    INSERT INTO detections (fetch_id, technology, category, version, evidence)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    [(fetch_id, d.technology, d.category, d.version, d.evidence) for d in detections],
                )
        if outcome == "retry":
            await conn.execute(
                """
                UPDATE crawl_jobs
                SET status = 'pending', scheduled_at = now() + %s, worker = NULL
                WHERE id = %s
                """,
                (retry_in or timedelta(0), job.id),
            )
        else:
            await conn.execute(
                "UPDATE crawl_jobs SET status = %s, finished_at = now() WHERE id = %s",
                (outcome, job.id),
            )
