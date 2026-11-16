"""Command line: stackscan migrate | add | crawl | report."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from stackscan import __version__, db, report
from stackscan.crawler import crawl
from stackscan.detector import Detector
from stackscan.domains import parse_lines
from stackscan.fetcher import Fetcher, make_client

DEFAULT_DSN = "postgresql://stackscan:stackscan@localhost:5432/stackscan"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stackscan", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--dsn",
        default=os.environ.get("DATABASE_URL", DEFAULT_DSN),
        help="PostgreSQL connection string (default: $DATABASE_URL)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="create or update the database schema")

    add = sub.add_parser("add", help="add domains (one per line) and queue them for crawling")
    add.add_argument("file", type=Path, help="text file of domains or URLs; '-' reads stdin")

    run = sub.add_parser("crawl", help="crawl every queued domain")
    run.add_argument("--workers", type=int, default=20)
    run.add_argument("--max-attempts", type=int, default=3)
    run.add_argument("--timeout", type=float, default=15.0, help="seconds per request")
    run.add_argument("--ignore-robots", action="store_true")

    rep = sub.add_parser("report", help="print aggregate statistics")
    rep.add_argument("--top", type=int, default=20)
    rep.add_argument("--format", choices=["text", "markdown"], default="text")
    return parser


async def run(args: argparse.Namespace) -> int:
    workers = getattr(args, "workers", 4)
    pool = db.open_pool(args.dsn, max_size=max(4, min(workers, 20)))
    await pool.open(wait=True, timeout=15)
    try:
        if args.command == "migrate":
            await db.migrate(pool)
            print("schema is up to date")
        elif args.command == "add":
            lines = sys.stdin if str(args.file) == "-" else args.file.read_text("utf-8").splitlines()
            names = parse_lines(lines)
            added, queued = await db.enqueue(pool, names)
            print(f"{len(names)} valid domains: {added} new, {queued} queued for crawling")
        elif args.command == "crawl":
            async with make_client(concurrency=args.workers, timeout=args.timeout) as client:
                fetcher = Fetcher(client, respect_robots=not args.ignore_robots)
                stats = await crawl(
                    pool,
                    fetcher,
                    Detector.default(),
                    workers=args.workers,
                    max_attempts=args.max_attempts,
                    on_result=lambda domain, msg: print(f"{domain:<40} {msg}", flush=True),
                )
            print("finished:", ", ".join(f"{k} {v}" for k, v in sorted(stats.items())) or "queue was empty")
        elif args.command == "report":
            print(report.render(await report.build(pool, top=args.top), args.format), end="")
    finally:
        await pool.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"))
    args = build_parser().parse_args(argv)
    if sys.platform == "win32":  # psycopg's async mode needs a selector loop on Windows
        return asyncio.run(run(args), loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
