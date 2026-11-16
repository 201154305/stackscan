-- stackscan schema. Idempotent: safe to run on every `stackscan migrate`.

CREATE TABLE IF NOT EXISTS domains (
    id         bigserial PRIMARY KEY,
    name       text NOT NULL UNIQUE CHECK (name = lower(name)),
    added_at   timestamptz NOT NULL DEFAULT now()
);

-- A work queue in plain PostgreSQL. Workers claim rows with
-- FOR UPDATE SKIP LOCKED, so any number of them can run in parallel
-- (even on different machines) without handing out the same job twice.
CREATE TABLE IF NOT EXISTS crawl_jobs (
    id            bigserial PRIMARY KEY,
    domain_id     bigint NOT NULL REFERENCES domains (id) ON DELETE CASCADE,
    status        text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'running', 'done', 'failed')),
    attempts      int NOT NULL DEFAULT 0,
    scheduled_at  timestamptz NOT NULL DEFAULT now(),
    started_at    timestamptz,
    finished_at   timestamptz,
    worker        text
);

-- At most one open job per domain: re-adding a domain never duplicates work.
CREATE UNIQUE INDEX IF NOT EXISTS crawl_jobs_one_open_per_domain
    ON crawl_jobs (domain_id) WHERE status IN ('pending', 'running');

-- Keeps the claim query an index scan even with millions of finished jobs.
CREATE INDEX IF NOT EXISTS crawl_jobs_pending
    ON crawl_jobs (scheduled_at, id) WHERE status = 'pending';

-- One row per HTTP attempt, successful or not: the raw history.
CREATE TABLE IF NOT EXISTS fetches (
    id           bigserial PRIMARY KEY,
    job_id       bigint NOT NULL REFERENCES crawl_jobs (id) ON DELETE CASCADE,
    domain_id    bigint NOT NULL REFERENCES domains (id) ON DELETE CASCADE,
    url          text NOT NULL,
    final_url    text,
    http_status  int,
    elapsed_ms   int,
    body_bytes   int,
    error        text,
    headers      jsonb NOT NULL DEFAULT '{}'::jsonb,
    fetched_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fetches_domain_recent
    ON fetches (domain_id, fetched_at DESC);

CREATE TABLE IF NOT EXISTS detections (
    fetch_id    bigint NOT NULL REFERENCES fetches (id) ON DELETE CASCADE,
    technology  text NOT NULL,
    category    text NOT NULL,
    version     text,
    evidence    text NOT NULL,
    PRIMARY KEY (fetch_id, technology)
);

CREATE INDEX IF NOT EXISTS detections_technology ON detections (technology);

-- The most recent successful fetch of each domain: what reports read from.
CREATE OR REPLACE VIEW latest_fetches AS
SELECT DISTINCT ON (domain_id) *
FROM fetches
WHERE error IS NULL
ORDER BY domain_id, fetched_at DESC, id DESC;
