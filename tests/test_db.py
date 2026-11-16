import asyncio
from datetime import timedelta

from stackscan import db, report
from stackscan.detector import Detection
from stackscan.fetcher import FetchResult


async def scalar(pool, query, params=None):
    async with pool.connection() as conn:
        row = await (await conn.execute(query, params)).fetchone()
        return row[0]


async def test_enqueue_is_idempotent(pool):
    assert await db.enqueue(pool, ["a.com", "b.com"]) == (2, 2)
    assert await db.enqueue(pool, ["a.com", "b.com", "c.com"]) == (1, 1)
    assert await scalar(pool, "SELECT count(*) FROM crawl_jobs") == 3


async def test_concurrent_workers_never_claim_the_same_job(pool):
    await db.enqueue(pool, [f"site{i}.com" for i in range(10)])
    a, b = await asyncio.gather(db.claim_jobs(pool, 6, "a"), db.claim_jobs(pool, 6, "b"))
    ids = [j.id for j in a + b]
    assert len(ids) == 10
    assert len(set(ids)) == 10
    assert await scalar(pool, "SELECT count(*) FROM crawl_jobs WHERE status = 'running'") == 10


async def test_retry_reschedules_and_done_finishes(pool):
    await db.enqueue(pool, ["a.com"])
    [job] = await db.claim_jobs(pool, 1, "w")
    failed = FetchResult(url="https://a.com/", error="timeout: slow", error_kind="timeout")
    await db.record_result(pool, job, failed, [], outcome="retry", retry_in=timedelta(hours=1))

    assert await db.claim_jobs(pool, 1, "w") == []  # not due yet
    assert await db.open_job_count(pool) == 1

    async with pool.connection() as conn:
        await conn.execute("UPDATE crawl_jobs SET scheduled_at = now()")
    [again] = await db.claim_jobs(pool, 1, "w")
    assert again.attempts == 2

    ok = FetchResult(url="https://a.com/", status=200, headers={"server": "nginx"})
    detections = [Detection("Nginx", "Web server", None, "header server: nginx")]
    await db.record_result(pool, again, ok, detections, outcome="done")
    assert await scalar(pool, "SELECT status FROM crawl_jobs") == "done"
    assert await scalar(pool, "SELECT count(*) FROM fetches") == 2
    assert await scalar(pool, "SELECT technology FROM detections") == "Nginx"


async def test_requeue_stale_running_jobs(pool):
    await db.enqueue(pool, ["a.com"])
    await db.claim_jobs(pool, 1, "crashed-worker")
    async with pool.connection() as conn:
        await conn.execute("UPDATE crawl_jobs SET started_at = now() - interval '1 hour'")
    assert await db.requeue_stale(pool, timedelta(minutes=10)) == 1
    assert len(await db.claim_jobs(pool, 1, "w")) == 1


async def test_report_counts_a_domain_failing_twice_once(pool):
    down = FetchResult(url="u", error="dns: unknown host", error_kind="dns")
    for _ in range(2):  # two crawl runs
        await db.enqueue(pool, ["gone.com"])
        [job] = await db.claim_jobs(pool, 1, "w")
        await db.record_result(pool, job, down, [], outcome="failed")
    tables = {t.title: t for t in await report.build(pool)}
    assert tables["Summary"].rows[0][:3] == (1, 0, 1)


async def test_report_uses_latest_successful_fetch(pool):
    await db.enqueue(pool, ["a.com", "b.com"])
    jobs = await db.claim_jobs(pool, 2, "w")
    shop = [Detection("Shopify", "Ecommerce", None, "x"), Detection("Cloudflare", "CDN", None, "x")]
    await db.record_result(
        pool, jobs[0], FetchResult(url="u", status=200, elapsed_ms=100), shop, outcome="done"
    )
    await db.record_result(
        pool, jobs[1], FetchResult(url="u", status=200, elapsed_ms=300), [], outcome="done"
    )

    tables = {t.title: t for t in await report.build(pool)}
    assert tables["Summary"].rows[0][:4] == (2, 2, 0, 0)
    assert [r[:3] for r in tables["Most used technologies"].rows] == [
        ("Cloudflare", "CDN", 1),
        ("Shopify", "Ecommerce", 1),
    ]
    assert dict(tables["E-commerce platform per site"].rows) == {"Shopify": 1, "(none detected)": 1}
    assert tables["Technologies seen together"].rows == [("Cloudflare", "Shopify", 1)]
