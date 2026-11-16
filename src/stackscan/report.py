"""Aggregate reports, computed in SQL over each domain's latest successful fetch."""

from __future__ import annotations

from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

SUMMARY = """
SELECT
    (SELECT count(*) FROM domains)                                        AS domains,
    (SELECT count(*) FROM latest_fetches)                                 AS reached,
    (SELECT count(*) FROM (
        SELECT DISTINCT ON (domain_id) status FROM crawl_jobs ORDER BY domain_id, id DESC
     ) last_job WHERE status = 'failed')                                  AS failed,
    (SELECT count(*) FROM crawl_jobs WHERE status IN ('pending', 'running')) AS queued,
    (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY elapsed_ms)::int
       FROM latest_fetches)                                               AS median_ms
"""

TOP_TECHNOLOGIES = """
SELECT d.technology, d.category, count(*) AS sites,
       round(100.0 * count(*) / NULLIF((SELECT count(*) FROM latest_fetches), 0), 1) AS pct
FROM detections d
JOIN latest_fetches f ON f.id = d.fetch_id
GROUP BY d.technology, d.category
ORDER BY sites DESC, d.technology
LIMIT %s
"""

# Which technologies show up together on the same site.
PAIRS = """
SELECT a.technology, b.technology, count(*) AS sites
FROM detections a
JOIN detections b ON b.fetch_id = a.fetch_id AND a.technology < b.technology
JOIN latest_fetches f ON f.id = a.fetch_id
WHERE a.category NOT IN ('Security', 'Protocol') AND b.category NOT IN ('Security', 'Protocol')
GROUP BY 1, 2
ORDER BY sites DESC, 1, 2
LIMIT %s
"""

# One platform per site for the e-commerce market-share view.
ECOMMERCE = """
SELECT coalesce(e.technology, '(none detected)') AS platform, count(*) AS sites
FROM latest_fetches f
LEFT JOIN LATERAL (
    SELECT technology FROM detections
    WHERE fetch_id = f.id AND category = 'Ecommerce'
    ORDER BY technology LIMIT 1
) e ON true
GROUP BY 1
ORDER BY sites DESC, 1
"""


@dataclass
class Table:
    title: str
    columns: list[str]
    rows: list[tuple]


async def build(pool: AsyncConnectionPool, *, top: int = 20) -> list[Table]:
    async with pool.connection() as conn:
        summary = await (await conn.execute(SUMMARY)).fetchone()
        techs = await (await conn.execute(TOP_TECHNOLOGIES, (top,))).fetchall()
        pairs = await (await conn.execute(PAIRS, (min(top, 10),))).fetchall()
        shops = await (await conn.execute(ECOMMERCE)).fetchall()
    assert summary is not None
    return [
        Table("Summary", ["domains", "reached", "failed", "queued", "median response (ms)"], [summary]),
        Table("Most used technologies", ["technology", "category", "sites", "% of reached"], techs),
        Table("E-commerce platform per site", ["platform", "sites"], shops),
        Table("Technologies seen together", ["technology", "technology", "sites"], pairs),
    ]


def render(tables: list[Table], fmt: str = "text") -> str:
    return "\n\n".join(_markdown(t) if fmt == "markdown" else _text(t) for t in tables) + "\n"


def _cells(row: tuple) -> list[str]:
    return ["" if v is None else str(v) for v in row]


def _text(table: Table) -> str:
    rows = [_cells(r) for r in table.rows]
    widths = [
        max(len(c), *(len(r[i]) for r in rows)) if rows else len(c) for i, c in enumerate(table.columns)
    ]
    line = "  ".join("-" * w for w in widths)
    out = [table.title, "  ".join(c.ljust(w) for c, w in zip(table.columns, widths, strict=True)), line]
    out += ["  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)) for r in rows]
    return "\n".join(out)


def _markdown(table: Table) -> str:
    out = [f"### {table.title}", "", "| " + " | ".join(table.columns) + " |"]
    out.append("|" + "|".join(" --- " for _ in table.columns) + "|")
    out += ["| " + " | ".join(_cells(r)) + " |" for r in table.rows]
    return "\n".join(out)
