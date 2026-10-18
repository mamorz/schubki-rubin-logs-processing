-- OpenStack VM lifecycle tracing. SELECT-only: no schema, index or data changes.
-- Run from the repo root:
--   scripts/jury-compose.sh --profile tools run --rm psql -f /sql/examples/openstack_vm_tracing.sql
-- Results and limitations: docs/openstack_vm_tracing.md

-- V1. Background request IDs: shared by many VMs or far larger than any user request.
SELECT request_id, count(*) AS lines, count(DISTINCT instance_id) AS instances,
       string_agg(DISTINCT service, ',') AS services, count(DISTINCT component) AS components
FROM openstack_logs WHERE request_id IS NOT NULL
GROUP BY request_id HAVING count(DISTINCT instance_id) > 1 OR count(*) > 100
ORDER BY lines DESC;

-- V2. Largest remaining (user) request: confirms the threshold is safe.
SELECT max(n) AS max_lines, max(ni) AS max_instances FROM (
  SELECT request_id, count(*) AS n, count(DISTINCT instance_id) AS ni
  FROM openstack_logs WHERE request_id IS NOT NULL
  GROUP BY request_id HAVING NOT (count(DISTINCT instance_id) > 1 OR count(*) > 100)) x;

-- V3. Do VM UUIDs in nova-api /servers/{uuid} URLs match compute instance_id values?
WITH api AS (
  SELECT DISTINCT regexp_extract(http_path, '/servers/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', 1) AS uuid
  FROM openstack_logs WHERE service = 'nova-api' AND http_path LIKE '%/servers/%-%'),
cmp AS (SELECT DISTINCT instance_id FROM openstack_logs WHERE instance_id IS NOT NULL)
SELECT count(*) AS api_url_uuids,
       sum(CASE WHEN c.instance_id IS NOT NULL THEN 1 ELSE 0 END) AS matched_compute,
       (SELECT count(*) FROM cmp) AS compute_instances
FROM api a LEFT JOIN cmp c ON c.instance_id = a.uuid WHERE a.uuid <> '';

-- V4. No log line mentions more than one known VM (UUID matching cannot mix VMs).
WITH inst AS (SELECT DISTINCT instance_id FROM openstack_logs WHERE instance_id IS NOT NULL),
u AS (SELECT source_file, line_no,
             unnest(regexp_extract_all(message, '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')) AS uuid
      FROM openstack_logs)
SELECT count(*) AS lines_with_multiple_instances FROM (
  SELECT source_file, line_no FROM u JOIN inst ON inst.instance_id = u.uuid
  GROUP BY 1, 2 HAVING count(DISTINCT u.uuid) > 1) x;

-- V5. Records per correlation method, whole corpus. Sets are deduplicated on
-- (instance_id, source_file, line_no) before counting.
WITH bg AS (SELECT request_id FROM openstack_logs WHERE request_id IS NOT NULL
            GROUP BY 1 HAVING count(DISTINCT instance_id) > 1 OR count(*) > 100),
inst AS (SELECT DISTINCT instance_id FROM openstack_logs WHERE instance_id IS NOT NULL),
by_uuid AS (
  SELECT DISTINCT u.uuid AS instance_id, u.source_file, u.line_no FROM (
    SELECT source_file, line_no,
           unnest(regexp_extract_all(message, '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')) AS uuid
    FROM openstack_logs) u
  JOIN inst ON inst.instance_id = u.uuid),
ireq AS (SELECT DISTINCT instance_id, request_id FROM openstack_logs
         WHERE instance_id IS NOT NULL AND request_id IS NOT NULL
           AND request_id NOT IN (SELECT request_id FROM bg)),
by_req AS (SELECT DISTINCT i.instance_id, l.source_file, l.line_no
           FROM ireq i JOIN openstack_logs l ON l.request_id = i.request_id),
by_col AS (SELECT instance_id, source_file, line_no FROM openstack_logs WHERE instance_id IS NOT NULL),
uni AS (SELECT * FROM by_uuid UNION SELECT * FROM by_req)
SELECT (SELECT count(*) FROM by_col) AS m1_instance_id_col,
       (SELECT count(*) FROM by_uuid) AS m2_uuid_in_message,
       (SELECT count(*) FROM by_req) AS m3_user_request_id,
       (SELECT count(*) FROM uni) AS m2_union_m3,
       (SELECT count(*) FROM (SELECT * FROM by_uuid EXCEPT SELECT * FROM by_col) x) AS m2_extra_over_m1,
       (SELECT count(*) FROM (SELECT * FROM by_req EXCEPT SELECT * FROM by_uuid) x) AS m3_extra_over_m2;

-- V6. The four labelled anomalous VMs: records found by each method.
WITH bg AS (SELECT request_id FROM openstack_logs WHERE request_id IS NOT NULL
            GROUP BY 1 HAVING count(DISTINCT instance_id) > 1 OR count(*) > 100),
a AS (SELECT instance_id FROM openstack_anomalous_instances),
by_col AS (SELECT l.instance_id, l.source_file, l.line_no FROM openstack_logs l JOIN a USING (instance_id)),
by_uuid AS (SELECT a.instance_id, l.source_file, l.line_no
            FROM a JOIN openstack_logs l ON strpos(l.message, a.instance_id) > 0),
ireq AS (SELECT DISTINCT instance_id, request_id FROM openstack_logs
         WHERE instance_id IN (SELECT instance_id FROM a) AND request_id IS NOT NULL
           AND request_id NOT IN (SELECT request_id FROM bg)),
by_req AS (SELECT i.instance_id, l.source_file, l.line_no
           FROM ireq i JOIN openstack_logs l ON l.request_id = i.request_id),
uni AS (SELECT * FROM by_uuid UNION SELECT * FROM by_req),
flag AS (SELECT l.instance_id FROM openstack_logs l JOIN a USING (instance_id) WHERE l.is_anomaly)
SELECT a.instance_id,
  (SELECT count(*) FROM flag f WHERE f.instance_id = a.instance_id) AS is_anomaly_true,
  (SELECT count(*) FROM by_col c WHERE c.instance_id = a.instance_id) AS m1_col,
  (SELECT count(*) FROM by_uuid u WHERE u.instance_id = a.instance_id) AS m2_uuid,
  (SELECT count(*) FROM by_req r WHERE r.instance_id = a.instance_id) AS m3_req,
  (SELECT count(*) FROM uni x WHERE x.instance_id = a.instance_id) AS union_m2_m3,
  (SELECT count(*) FROM ireq i WHERE i.instance_id = a.instance_id) AS user_requests
FROM a ORDER BY 1;

-- V7. Recommended single-VM timeline: UUID mention UNION user request_id.
-- UNION deduplicates rows found by both methods. Background IDs are excluded only
-- when collecting the VM's request IDs, not from UUID matches.
WITH bg AS (SELECT request_id FROM openstack_logs WHERE request_id IS NOT NULL
            GROUP BY 1 HAVING count(DISTINCT instance_id) > 1 OR count(*) > 100),
reqs AS (SELECT DISTINCT request_id FROM openstack_logs
         WHERE instance_id = '544fd51c-4edc-4780-baae-ba1d80a0acfc' AND request_id IS NOT NULL
           AND request_id NOT IN (SELECT request_id FROM bg)),
tl AS (SELECT * FROM openstack_logs WHERE strpos(message, '544fd51c-4edc-4780-baae-ba1d80a0acfc') > 0
       UNION
       SELECT l.* FROM openstack_logs l JOIN reqs USING (request_id))
SELECT source_file, line_no, ts, service, component, request_id,
       CASE WHEN strpos(message, '544fd51c-4edc-4780-baae-ba1d80a0acfc') > 0 THEN 'uuid' ELSE 'request' END AS found_by,
       left(message, 110) AS msg
FROM tl ORDER BY ts, source_file, line_no
LIMIT 50;

-- V8. VMs whose boot POST or DELETE cannot be linked by request_id.
WITH bg AS (SELECT request_id FROM openstack_logs WHERE request_id IS NOT NULL
            GROUP BY 1 HAVING count(DISTINCT instance_id) > 1 OR count(*) > 100),
inst AS (SELECT instance_id, min(source_file) AS first_file, min(ts) AS t0, max(ts) AS t1, count(*) AS n
         FROM openstack_logs WHERE instance_id IS NOT NULL GROUP BY 1),
ireq AS (SELECT DISTINCT instance_id, request_id FROM openstack_logs
         WHERE instance_id IS NOT NULL AND request_id IS NOT NULL
           AND request_id NOT IN (SELECT request_id FROM bg)),
api AS (SELECT request_id, http_method FROM openstack_logs WHERE service = 'nova-api' AND http_method IS NOT NULL),
per AS (SELECT x.instance_id,
               max(CASE WHEN a.http_method = 'POST' THEN 1 ELSE 0 END) AS has_post,
               max(CASE WHEN a.http_method = 'DELETE' THEN 1 ELSE 0 END) AS has_delete
        FROM inst x LEFT JOIN ireq i ON i.instance_id = x.instance_id
        LEFT JOIN api a ON a.request_id = i.request_id GROUP BY 1)
SELECT per.instance_id, has_post, has_delete, first_file, t0, t1, n
FROM per JOIN inst USING (instance_id) WHERE has_post = 0 OR has_delete = 0 ORDER BY t0;
