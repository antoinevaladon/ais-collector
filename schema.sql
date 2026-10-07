-- AIS collector schema, version 2 (introduced in collector 0.2.0)
--
-- Apply or upgrade with:  psql -h localhost -U ais -d ais -f schema.sql
--
-- Safe to re-run at any time: it creates what's missing and upgrades an
-- existing database in place, without touching collected data. It applies
-- all-or-nothing, so a failure halfway leaves the database as it was.
-- All timestamps are timestamptz, and every partition boundary is in UTC.
--
-- When you change this file, bump the schema_version at the bottom and
-- SCHEMA_VERSION in collector.py to match.

\set ON_ERROR_STOP on
SET client_min_messages = warning;
BEGIN;

------------------------------------------------------------------------
-- Raw messages: one row per message received, partitioned by month.
-- The full message is kept untouched in `payload`; the other columns are
-- just the fields worth indexing. Nothing is filtered at collection time.
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ais_raw (
    id            bigserial,
    received_at   timestamptz      NOT NULL,   -- when the collector received it
    source        text             NOT NULL DEFAULT 'aisstream',
    message_type  text             NOT NULL,   -- PositionReport, ShipStaticData, ...
    mmsi          integer,
    ship_name     text,
    lat           double precision,
    lon           double precision,
    payload       jsonb            NOT NULL,   -- the complete message as received
    run_id        bigint                       -- which collector run (and so which version) stored it
) PARTITION BY RANGE (received_at);

-- Upgrade from schema 1. No foreign key on purpose: it would slow every insert.
ALTER TABLE ais_raw ADD COLUMN IF NOT EXISTS run_id bigint;

-- Per-vessel lookups ("track of MMSI X last week") and cheap time-range scans.
CREATE INDEX IF NOT EXISTS ais_raw_mmsi_time_idx ON ais_raw (mmsi, received_at);
CREATE INDEX IF NOT EXISTS ais_raw_time_brin     ON ais_raw USING brin (received_at);

-- Safety net: anything outside the monthly partitions lands here instead of
-- failing. It should stay empty; if it doesn't, the clock or the partition
-- maintenance needs a look.
CREATE TABLE IF NOT EXISTS ais_raw_default PARTITION OF ais_raw DEFAULT;

-- Creates the current month's partition plus `months_ahead` future ones.
-- The collector calls this at startup and every few hours.
CREATE OR REPLACE FUNCTION ensure_ais_partitions(months_ahead integer DEFAULT 2)
RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    this_month date := date_trunc('month', now() AT TIME ZONE 'UTC')::date;
    start_d    date;
    end_d      date;
    part_name  text;
BEGIN
    FOR i IN 0..months_ahead LOOP
        start_d   := (this_month + make_interval(months => i))::date;
        end_d     := (this_month + make_interval(months => i + 1))::date;
        part_name := format('ais_raw_%s', to_char(start_d, 'YYYY_MM'));
        IF to_regclass(part_name) IS NULL THEN
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF ais_raw FOR VALUES FROM (%L) TO (%L)',
                part_name, start_d || ' 00:00:00+00', end_d || ' 00:00:00+00');
        END IF;
    END LOOP;
END;
$$;

DO $$ BEGIN PERFORM ensure_ais_partitions(2); END $$;

------------------------------------------------------------------------
-- The gap log.
-- collector_runs: one row per time the collector process starts, with the
--   exact collector version and the versions of everything around it. A run
--   that never recorded a clean stop ended in a crash, kill or power loss;
--   the next run marks it as such, using the last message it wrote as the
--   end time.
-- feed_events: everything that happened to the feed connection.
--   Each break in coverage is opened by exactly one of:
--     disconnected - the connection dropped or failed
--     stale        - connected, but no messages for too long (forced reconnect)
--     error        - the server sent an error (e.g. invalid API key)
--   and closed by the next `connected`. `since` holds when the data actually
--   stopped (the last message received), which can be earlier than when the
--   problem was noticed. `dropped` events record messages lost because the
--   in-memory buffer filled up while the database was unavailable.
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collector_runs (
    run_id             bigserial   PRIMARY KEY,
    source             text        NOT NULL DEFAULT 'aisstream',
    hostname           text,
    version            text,       -- e.g. 0.3.0+git.1a2b3c4
    started_at         timestamptz NOT NULL DEFAULT now(),
    last_heartbeat     timestamptz NOT NULL DEFAULT now(),
    stopped_at         timestamptz,
    stop_reason        text,
    messages_received  bigint      NOT NULL DEFAULT 0,
    messages_written   bigint      NOT NULL DEFAULT 0,
    messages_dropped   bigint      NOT NULL DEFAULT 0,
    runtime            text        -- Python, library, PostgreSQL and OS versions
);

-- Upgrade from schema 1.
ALTER TABLE collector_runs ADD COLUMN IF NOT EXISTS runtime text;

CREATE TABLE IF NOT EXISTS feed_events (
    event_id    bigserial   PRIMARY KEY,
    run_id      bigint      NOT NULL REFERENCES collector_runs (run_id),
    event_time  timestamptz NOT NULL,
    event_type  text        NOT NULL,
    since       timestamptz,
    detail      text
);

CREATE INDEX IF NOT EXISTS feed_events_run_time_idx ON feed_events (run_id, event_time);

-- Windows of time when the feed was connected.
CREATE OR REPLACE VIEW feed_coverage AS
SELECT r.source,
       c.run_id,
       c.event_time                                              AS covered_from,
       COALESCE(COALESCE(e.since, e.event_time), r.ended_at)     AS covered_until,
       COALESCE(e.event_type, r.end_cause)                       AS ended_by,
       COALESCE(e.detail, r.stop_reason)                         AS detail,
       r.end_cause                                               AS run_ended_by,
       r.version
FROM feed_events c
JOIN (
    SELECT run_id,
           source,
           version,
           stop_reason,
           COALESCE(stopped_at, last_heartbeat) AS ended_at,
           CASE
               WHEN stopped_at IS NULL              THEN 'still_running'
               WHEN stop_reason = 'clean shutdown'  THEN 'collector_stopped'
               ELSE 'collector_crashed'
           END AS end_cause
    FROM collector_runs
) r ON r.run_id = c.run_id
LEFT JOIN LATERAL (
    SELECT x.event_type, x.event_time, x.since, x.detail
    FROM feed_events x
    WHERE x.run_id = c.run_id
      AND x.event_type IN ('disconnected', 'stale', 'error')
      AND x.event_time >= c.event_time
    ORDER BY x.event_time
    LIMIT 1
) e ON true
WHERE c.event_type = 'connected';

-- The gaps between those windows, labelled with what caused each one:
--   disconnected / stale / error  - feed problem while the collector was running
--   collector_stopped              - the collector was stopped deliberately
--   collector_crashed              - crash, kill or power loss
--   buffer_full                    - partial gap: the database was unavailable long
--                                    enough to fill the in-memory buffer, so SOME
--                                    messages in this window were lost (count in detail)
-- If the collector also stopped during a feed outage, the cause says so,
-- e.g. "disconnected, then collector_crashed". run_id and version identify
-- the run in which the gap began, so crashes can be traced to a release.
-- A gap still in progress (feed down right now) appears once it closes.
CREATE OR REPLACE VIEW collection_gaps AS
SELECT source,
       covered_until               AS gap_start,
       next_from                   AS gap_end,
       next_from - covered_until   AS duration,
       CASE WHEN next_run_id <> run_id AND ended_by IN ('disconnected', 'stale', 'error')
            THEN ended_by || ', then ' || run_ended_by
            ELSE ended_by
       END                         AS cause,
       detail,
       run_id,
       version
FROM (
    SELECT *,
           lead(covered_from) OVER w AS next_from,
           lead(run_id)       OVER w AS next_run_id
    FROM feed_coverage
    WINDOW w AS (PARTITION BY source ORDER BY covered_from)
) x
WHERE next_from IS NOT NULL
  AND next_from > covered_until
UNION ALL
SELECT r.source, e.since, e.event_time, e.event_time - e.since, 'buffer_full', e.detail,
       r.run_id, r.version
FROM feed_events e
JOIN collector_runs r ON r.run_id = e.run_id
WHERE e.event_type = 'dropped'
  AND e.since IS NOT NULL;

-- One-time upgrade from schema 1: tag messages stored before run_id existed
-- with the run that was active when they arrived. Only one collector runs at
-- a time, so each message belongs to the latest run started before it.
-- Skipped on every later re-run.
DO $$
BEGIN
    IF to_regclass('schema_info') IS NULL THEN
        UPDATE ais_raw a
        SET run_id = r.run_id
        FROM (SELECT run_id, source, started_at,
                     lead(started_at) OVER (PARTITION BY source ORDER BY started_at) AS next_start
              FROM collector_runs) r
        WHERE a.run_id IS NULL
          AND a.source = r.source
          AND a.received_at >= r.started_at
          AND (r.next_start IS NULL OR a.received_at < r.next_start);
    END IF;
END $$;

------------------------------------------------------------------------
-- Schema version. The collector checks this at startup and refuses to run
-- against a schema older than it needs. Never moves backwards.
------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_info (
    key    text PRIMARY KEY,
    value  text NOT NULL
);

INSERT INTO schema_info (key, value) VALUES ('schema_version', '2')
ON CONFLICT (key) DO UPDATE
    SET value = GREATEST(schema_info.value::integer, EXCLUDED.value::integer)::text;

COMMIT;

SELECT 'schema is up to date: version ' || value AS result
FROM schema_info WHERE key = 'schema_version';
