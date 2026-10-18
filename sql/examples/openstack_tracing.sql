-- OpenStack incident-tracing examples. SELECT-only: no schema, index or data changes.
-- Run from the repo root:
--   scripts/jury-compose.sh --profile tools run --rm psql -f /sql/examples/openstack_tracing.sql
-- Results and limitations: docs/openstack_incident_tracing.md

-- Q1. What happened to one request? API call plus the compute steps it triggered.
SELECT source_file, line_no, ts, service, component, level,
       instance_id, http_method, http_status, latency_s, left(message, 90) AS msg
FROM openstack_logs
WHERE request_id = 'req-d6143bd4-9784-4e95-a0c8-83fba00530b6'
ORDER BY ts, source_file, line_no
LIMIT 50;

-- Q2. Full timeline of one VM. instance_id is only parsed on nova-compute lines;
-- the strpos() branch also catches nova-api URLs and external-event lines.
SELECT ts, service, component, request_id, is_anomaly,
       left(message, 100) AS msg
FROM openstack_logs
WHERE instance_id = '544fd51c-4edc-4780-baae-ba1d80a0acfc'
   OR strpos(message, '544fd51c-4edc-4780-baae-ba1d80a0acfc') > 0
ORDER BY ts, source_file, line_no
LIMIT 50;

-- Q3. Hypervisor spawn time, labelled anomalous VMs vs all others (whole corpus).
WITH spawn AS (
  SELECT source_file, instance_id, is_anomaly,
         regexp_extract(message, 'Took ([0-9.]+) seconds to spawn', 1)::DOUBLE AS secs
  FROM openstack_logs
  WHERE component = 'nova.compute.manager'
    AND message LIKE '%seconds to spawn the instance on the hypervisor%')
SELECT is_anomaly, count(*) AS spawns,
       round(quantile_cont(secs, 0.5), 2) AS p50_s,
       round(quantile_cont(secs, 0.95), 2) AS p95_s,
       round(max(secs), 2) AS max_s
FROM spawn GROUP BY is_anomaly ORDER BY is_anomaly;

-- Q4. Coverage of the 4 labelled anomalous VMs across services and requests.
SELECT a.instance_id,
       count(*) AS lines_mentioning,
       sum(CASE WHEN l.is_anomaly THEN 1 ELSE 0 END) AS flagged_lines,
       count(DISTINCT l.request_id) AS request_ids,
       string_agg(DISTINCT l.service, ',') AS services,
       min(l.ts) AS first_seen, max(l.ts) AS last_seen
FROM openstack_anomalous_instances a
JOIN openstack_logs l ON strpos(l.message, a.instance_id) > 0
GROUP BY a.instance_id ORDER BY first_seen;

-- Q5. HTTP status codes and latency (nova-api access lines only).
SELECT service, http_method, http_status, count(*) AS requests,
       round(quantile_cont(latency_s, 0.5), 3) AS p50_s,
       round(quantile_cont(latency_s, 0.95), 3) AS p95_s,
       round(max(latency_s), 3) AS max_s
FROM openstack_logs
WHERE http_status IS NOT NULL
GROUP BY 1, 2, 3 ORDER BY requests DESC;

-- Q6. Which spawn phase is slow? Per-VM phase durations from lifecycle messages.
WITH ph AS (
  SELECT instance_id, bool_or(is_anomaly) AS anomalous,
         min(CASE WHEN message LIKE '%Creating image%' THEN ts END) AS t_img,
         min(CASE WHEN message LIKE '%VM Started (Lifecycle Event)%' THEN ts END) AS t_started,
         min(CASE WHEN message LIKE '%Instance spawned successfully%' THEN ts END) AS t_spawned
  FROM openstack_logs WHERE instance_id IS NOT NULL GROUP BY instance_id)
SELECT anomalous, count(*) AS instances,
       round(quantile_cont(epoch(t_started - t_img), 0.5), 1) AS img_to_started_p50,
       round(max(epoch(t_started - t_img)), 1) AS img_to_started_max,
       round(quantile_cont(epoch(t_spawned - t_started), 0.5), 1) AS started_to_spawned_p50,
       round(max(epoch(t_spawned - t_started)), 1) AS started_to_spawned_max
FROM ph WHERE t_img IS NOT NULL AND t_started IS NOT NULL AND t_spawned IS NOT NULL
GROUP BY anomalous ORDER BY anomalous;
