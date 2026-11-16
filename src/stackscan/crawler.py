"""The crawl loop: N async workers pulling jobs from the PostgreSQL queue."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections import Counter
from collections.abc import Callable
from datetime import timedelta

from psycopg_pool import AsyncConnectionPool

from stackscan import db
from stackscan.detector import Detector, Page
from stackscan.fetcher import Fetcher, FetchResult

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def decide(result: FetchResult, attempts: int, max_attempts: int) -> str:
    """What happens to a job after one attempt: 'done', 'failed' or 'retry'.

    Any HTTP response counts as done, even a 403: its headers still reveal the
    CDN and server. Network errors and overload statuses are retried with backoff.
    """
    transient = result.retryable or result.status in RETRY_STATUSES
    if transient and attempts < max_attempts:
        return "retry"
    return "failed" if result.error else "done"


def backoff(attempts: int, base: float) -> timedelta:
    return timedelta(seconds=base * 2 ** (attempts - 1))


async def crawl(
    pool: AsyncConnectionPool,
    fetcher: Fetcher,
    detector: Detector,
    *,
    workers: int = 20,
    max_attempts: int = 3,
    backoff_base: float = 5.0,
    idle_poll: float = 1.0,
    on_result: Callable[[str, str], None] | None = None,
) -> Counter[str]:
    """Run until the queue has no pending jobs left. Returns a count per outcome."""
    worker_prefix = f"{socket.gethostname()}:{os.getpid()}"
    stats: Counter[str] = Counter()
    stale = await db.requeue_stale(pool, timedelta(minutes=10))
    if stale:
        log.info("requeued %d stale jobs", stale)

    async def work(n: int) -> None:
        name = f"{worker_prefix}:{n}"
        while True:
            jobs = await db.claim_jobs(pool, 1, name)
            if not jobs:
                if await db.open_job_count(pool) == 0:
                    return
                await asyncio.sleep(idle_poll)  # retries are scheduled for later
                continue
            for job in jobs:
                result = await fetcher.fetch(job.domain)
                detections = []
                if result.error is None:
                    page = Page(result.body, result.all_headers, result.cookies)
                    detections = detector.detect(page)
                outcome = decide(result, job.attempts, max_attempts)
                await db.record_result(
                    pool,
                    job,
                    result,
                    detections,
                    outcome=outcome,
                    retry_in=backoff(job.attempts, backoff_base),
                )
                stats[outcome] += 1
                if on_result:
                    on_result(job.domain, _describe(result, outcome, len(detections)))

    await asyncio.gather(*(work(n) for n in range(workers)))
    return stats


def _describe(result: FetchResult, outcome: str, found: int) -> str:
    if result.error:
        return f"{outcome:<6} {result.error[:80]}"
    return f"{outcome:<6} HTTP {result.status} {result.elapsed_ms} ms, {found} technologies"
