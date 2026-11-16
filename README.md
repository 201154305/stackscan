# stackscan

An async crawler that finds out which technologies websites run on (e-commerce platform,
CMS, CDN, web server, front-end framework, analytics) and stores every fetch and detection
in PostgreSQL, where the reports are plain SQL.

I built it to learn how a crawl pipeline holds up beyond a single script: a work queue
that many workers can share, retries that do not hammer a struggling site, and data
modelled so that questions like "what do VTEX stores put in front of their servers?"
are one query away.

## What a run looks like

```
$ stackscan add seeds/varejo-br.txt
56 valid domains: 56 new, 56 queued for crawling
$ stackscan crawl --workers 16
carrefour.com.br                         done   HTTP 200 350 ms, 5 technologies
pichau.com.br                            done   HTTP 403 499 ms, 2 technologies
submarino.com.br                         failed dns: [Errno 11001] getaddrinfo failed
...
finished: done 53, failed 3, retry 2
```

56 Brazilian retail sites take about 40 seconds with 16 workers.

### Results: Brazilian online retail, 16 Nov 2026

53 of 56 sites reached; median home page response 1.5 s.

| Technology | Category | Sites | % of reached |
| --- | --- | --- | --- |
| Google Tag Manager | Tag manager | 34 | 64.2 |
| Next.js | JavaScript framework | 21 | 39.6 |
| React | JavaScript framework | 21 | 39.6 |
| Cloudflare | CDN | 17 | 32.1 |
| Akamai | CDN | 13 | 24.5 |
| Amazon CloudFront | CDN | 13 | 24.5 |
| VTEX | Ecommerce | 11 | 20.8 |
| Google Analytics | Analytics | 7 | 13.2 |
| WordPress | CMS | 5 | 9.4 |
| Shopify | Ecommerce | 3 | 5.7 |

E-commerce platform per site: VTEX 11, Shopify 3, Nuvemshop 2, Salesforce Commerce Cloud 2,
Loja Integrada 1, Tray 1, none detected 33. Most of the "none" group are the largest
retailers, which run in-house platforms (Magazine Luiza, Casas Bahia, Mercado Livre),
or sites that answered with a bot-protection page (see Limitations).

## How it works

```
seeds.txt ──► stackscan add ──► domains + crawl_jobs (PostgreSQL)
                                        │  FOR UPDATE SKIP LOCKED
                    ┌───────────────────┼───────────────────┐
                 worker 1            worker 2     …      worker N      (asyncio tasks)
                    │ robots.txt → GET / (max 2 MB) → detect
                    └──► one transaction: fetches + detections + job status
                                        │
                              stackscan report (SQL)
```

**The queue is a table.** Workers claim jobs with `UPDATE … WHERE id IN (SELECT … FOR UPDATE
SKIP LOCKED)`, so any number of workers, in one process or on several machines, can share it
without ever taking the same job. A partial unique index allows only one open job per domain,
so adding the same list twice queues nothing new. Jobs left `running` by a crashed worker go
back to the queue on the next start. No Redis or broker needed.

**Failures are classified, not just counted.**

| What happened | Outcome |
| --- | --- |
| Any HTTP response, including 403 | done: a block page still shows the CDN and server |
| 429, 500, 502, 503, 504 | retried with exponential backoff (5 s, 10 s, 20 s…) |
| Timeout, connection reset | retried with backoff |
| HTTPS refuses to connect | tries plain HTTP once |
| Domain does not exist (DNS) | failed immediately: retrying will not change DNS |
| Disallowed by robots.txt | failed immediately, never fetched |

**Every attempt is kept.** `fetches` stores each attempt with status, timing, size and the
headers worth keeping (cookies and tracking ids are dropped). Reports read from the
`latest_fetches` view, so re-crawling updates the numbers without losing history.

**Detection is data, not code.** [`fingerprints.json`](src/stackscan/fingerprints.json) lists
48 technologies with regexes over response headers, cookie names, `<meta>` tags, `<script src>`
URLs and raw HTML. A capture group becomes the version (`nginx/1.25.3` → 1.25.3), and
`implies` chains detections (Nuxt implies Vue.js, WooCommerce implies WordPress). It includes
platforms common in Brazil: VTEX, Nuvemshop, Tray and Loja Integrada.

**Politeness.** One home page and one robots.txt per domain, a 2 MB cap on each body, a
descriptive User-Agent, and robots.txt honoured by default.

## Running it

With Docker:

```sh
docker compose up -d db
docker compose run --rm app stackscan migrate
docker compose run --rm app stackscan add seeds/varejo-br.txt
docker compose run --rm app stackscan crawl --workers 16
docker compose run --rm app stackscan report            # or --format markdown
docker compose run --rm app pytest
```

Without Docker (Python 3.12+ and a local PostgreSQL):

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # .venv\Scripts on Windows
export DATABASE_URL=postgresql://stackscan:stackscan@localhost:5432/stackscan
stackscan migrate && stackscan add seeds/varejo-br.txt && stackscan crawl && stackscan report
TEST_DATABASE_URL=postgresql://stackscan:stackscan@localhost:5432/stackscan_test pytest
```

The test database is created on first run. Database tests are skipped when
`TEST_DATABASE_URL` is not set.

## Tests

28 tests: detector rules, the fetcher against mocked HTTP (robots.txt, HTTPS fallback, DNS
failures, timeouts, body cap), the queue against a real PostgreSQL (idempotent enqueue, two
workers claiming concurrently, retry scheduling, stale-job recovery), the reports, and one
end-to-end crawl with scripted sites that succeed, recover after a 503, or keep timing out.
CI runs ruff and the whole suite against PostgreSQL 17 on every push.

## Limitations

- Only the home page is fetched, so technologies used only deeper in a site are missed.
- No JavaScript is executed. Single-page apps that inject their stack at runtime look
  emptier than they are.
- Large retailers sit behind Akamai or Cloudflare bot protection. 8 of the 56 answered 403;
  stackscan records the CDN it can see and does not try to get around the block.
- Regex fingerprints can produce false positives. Every detection stores its evidence
  (`header server: nginx/1.25.3`, `script: https://cdn.shopify.com/…`) so it can be checked.

## Ideas for next steps

- Follow links to a sample of inner pages per site.
- Per-host rate limiting for crawls with many subdomains of one site.
- Track changes over time: which sites moved platform between two crawls.
- Load fingerprints into PostgreSQL so they can be versioned and joined in reports.

## License

MIT
