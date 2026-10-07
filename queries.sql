-- Health checks for the collector. Run all of them with:
--   psql -h localhost -U ais -d ais -f queries.sql
-- or paste any single query into psql / pgcli. Times are shown in UTC.
SET TIME ZONE 'UTC';

\echo '== Latest runs (a healthy running collector has a heartbeat under ~15 s old) =='
SELECT run_id, version, started_at::timestamp(0), last_heartbeat::timestamp(0),
       date_trunc('second', now() - last_heartbeat) AS heartbeat_age,
       stopped_at::timestamp(0), left(stop_reason, 40) AS stop_reason,
       messages_received, messages_written, messages_dropped
FROM collector_runs ORDER BY run_id DESC LIMIT 5;

\echo '== Collector versions that have run =='
SELECT version, count(*) AS runs,
       min(started_at)::timestamp(0) AS first_started,
       max(coalesce(stopped_at, last_heartbeat))::timestamp(0) AS last_active,
       sum(messages_written) AS messages_written,
       count(*) FILTER (WHERE stopped_at IS NOT NULL AND stop_reason <> 'clean shutdown') AS unclean_stops
FROM collector_runs GROUP BY version ORDER BY min(started_at);

\echo '== Messages and vessels per hour, last 24 h =='
SELECT date_trunc('hour', received_at)::timestamp(0) AS hour,
       count(*) AS messages, count(DISTINCT mmsi) AS vessels
FROM ais_raw
WHERE received_at > now() - interval '24 hours'
GROUP BY 1 ORDER BY 1;

\echo '== Message types, last 24 h =='
SELECT message_type, count(*) AS messages
FROM ais_raw
WHERE received_at > now() - interval '24 hours'
GROUP BY 1 ORDER BY 2 DESC;

\echo '== Most recently heard vessels =='
SELECT mmsi, max(ship_name) AS name, count(*) AS messages,
       max(received_at)::timestamp(0) AS last_heard
FROM ais_raw
WHERE received_at > now() - interval '1 hour'
GROUP BY mmsi ORDER BY last_heard DESC LIMIT 15;

\echo '== Share of the last 24 h the feed was connected =='
-- Counts time before your first run as not covered, so it starts low and climbs.
SELECT round(100.0 * coalesce(sum(extract(epoch FROM
           least(covered_until, now()) - greatest(covered_from, now() - interval '24 hours'))), 0)
       / 86400, 2) AS pct_connected
FROM feed_coverage
WHERE covered_until > now() - interval '24 hours';

\echo '== Gaps, newest first =='
SELECT gap_start::timestamp(0), gap_end::timestamp(0),
       date_trunc('second', duration) AS duration, cause, version, left(detail, 60) AS detail
FROM collection_gaps ORDER BY gap_start DESC LIMIT 20;

\echo '== Recent feed events =='
SELECT event_time::timestamp(0), event_type, left(detail, 90) AS detail
FROM feed_events ORDER BY event_id DESC LIMIT 15;

\echo '== Disk used per monthly partition =='
SELECT c.relname AS partition, pg_size_pretty(pg_total_relation_size(c.oid)) AS size
FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
WHERE i.inhparent = 'ais_raw'::regclass
ORDER BY c.relname;

\echo '== Rows in the catch-all partition (should be 0) =='
SELECT count(*) AS stray_rows FROM ais_raw_default;
