from datetime import timedelta

from stackscan import db
from stackscan.crawler import backoff, crawl, decide
from stackscan.detector import Detector
from stackscan.fetcher import FetchResult

TIMEOUT = FetchResult(url="u", error="timeout: slow", error_kind="timeout")
ROBOTS = FetchResult(url="u", error="disallowed by robots.txt", error_kind="robots")


def ok(status=200, **kw) -> FetchResult:
    return FetchResult(url="u", status=status, **kw)


def test_decide():
    assert decide(ok(), 1, 3) == "done"
    assert decide(ok(403), 1, 3) == "done"  # a block page still shows the CDN
    assert decide(ok(503), 1, 3) == "retry"
    assert decide(ok(503), 3, 3) == "done"
    assert decide(TIMEOUT, 2, 3) == "retry"
    assert decide(TIMEOUT, 3, 3) == "failed"
    assert decide(ROBOTS, 1, 3) == "failed"  # never retried


def test_backoff_doubles():
    assert [backoff(n, 5).total_seconds() for n in (1, 2, 3)] == [5, 10, 20]
    assert backoff(1, 0) == timedelta(0)


class ScriptedFetcher:
    """Returns a scripted sequence of results per domain, repeating the last one."""

    def __init__(self, script: dict[str, list[FetchResult]]):
        self.script = script
        self.calls: list[str] = []

    async def fetch(self, domain: str) -> FetchResult:
        self.calls.append(domain)
        queue = self.script[domain]
        return queue.pop(0) if len(queue) > 1 else queue[0]


async def test_crawl_end_to_end(pool):
    await db.enqueue(pool, ["shop.com", "flaky.com", "down.com"])
    fetcher = ScriptedFetcher(
        {
            "shop.com": [
                ok(headers={"x-shopid": "1"}, all_headers={"x-shopid": "1", "server": "cloudflare"})
            ],
            "flaky.com": [ok(503), ok(200, all_headers={"server": "nginx/1.27.0"})],
            "down.com": [TIMEOUT],
        }
    )
    stats = await crawl(
        pool, fetcher, Detector.default(), workers=3, max_attempts=2, backoff_base=0, idle_poll=0.01
    )

    assert stats == {"done": 2, "retry": 2, "failed": 1}
    assert sorted(fetcher.calls) == ["down.com", "down.com", "flaky.com", "flaky.com", "shop.com"]
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT d.name, j.status, j.attempts FROM crawl_jobs j JOIN domains d ON d.id = j.domain_id "
                "ORDER BY d.name"
            )
        ).fetchall()
        techs = await (
            await conn.execute("SELECT technology, version FROM detections ORDER BY technology")
        ).fetchall()
    assert rows == [("down.com", "failed", 2), ("flaky.com", "done", 2), ("shop.com", "done", 1)]
    assert techs == [("Cloudflare", None), ("Nginx", "1.27.0"), ("Shopify", None)]
