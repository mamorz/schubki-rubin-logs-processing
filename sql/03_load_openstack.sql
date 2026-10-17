-- Load the three OpenStack log files and the anomaly labels.
-- Line format: log_file date time pid level component [req-id user tenant - - -] message
-- Traceback continuation lines have no [...] context block, so it is optional.

INSERT INTO openstack_anomalous_instances
SELECT trim(line)
FROM read_csv('/data/raw/openstack/anomaly_labels.txt',
              columns = {'line': 'VARCHAR'}, delim = '\x01', quote = '', escape = '',
              header = false, auto_detect = false)
WHERE regexp_matches(trim(line), '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$');

INSERT INTO openstack_logs
WITH raw AS (
    -- One read per file so row_number() OVER () keeps each file's own line order.
    SELECT 'openstack_normal1' AS source_file, row_number() OVER () AS line_no, line
    FROM read_csv('/data/raw/openstack/openstack_normal1.log',
                  columns = {'line': 'VARCHAR'}, delim = '\x01', quote = '', escape = '',
                  header = false, auto_detect = false)
    UNION ALL
    SELECT 'openstack_normal2', row_number() OVER (), line
    FROM read_csv('/data/raw/openstack/openstack_normal2.log',
                  columns = {'line': 'VARCHAR'}, delim = '\x01', quote = '', escape = '',
                  header = false, auto_detect = false)
    UNION ALL
    SELECT 'openstack_abnormal', row_number() OVER (), line
    FROM read_csv('/data/raw/openstack/openstack_abnormal.log',
                  columns = {'line': 'VARCHAR'}, delim = '\x01', quote = '', escape = '',
                  header = false, auto_detect = false)
), parsed AS (
    SELECT source_file, line_no,
           regexp_extract(line,
               '^(\S+) (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) (\d+) (\S+) (\S+) ?(?:\[([^\]]*)\] ?)?(.*)$',
               ['log_file', 'ts', 'pid', 'level', 'component', 'ctx', 'message']) AS p
    FROM raw
), enriched AS (
    SELECT source_file, line_no, p,
           regexp_extract(p.ctx, '^(req-[0-9a-f-]+) (\S+) (\S+)', ['req', 'usr', 'tenant']) AS c,
           nullif(regexp_extract(p.message, '\[instance: ([0-9a-f-]{36})\]', 1), '') AS instance_id,
           regexp_extract(p.message,
               '"(\w+) (\S+) HTTP/[\d.]+" status: (\d+) len: (\d+) time: ([\d.]+)',
               ['method', 'path', 'status', 'len', 'time']) AS h
    FROM parsed
)
SELECT source_file,
       line_no,
       p.log_file,
       regexp_extract(p.log_file, '^(nova-[a-z]+)', 1),
       strptime(p.ts, '%Y-%m-%d %H:%M:%S.%f'),
       p.pid::INTEGER,
       p.level,
       p.component,
       nullif(c.req, ''),
       nullif(nullif(c.usr, ''), '-'),
       nullif(nullif(c.tenant, ''), '-'),
       instance_id,
       nullif(h.method, ''),
       nullif(h.path, ''),
       nullif(h.status, '')::INTEGER,
       nullif(h.len, '')::INTEGER,
       nullif(h.time, '')::DOUBLE,
       instance_id IS NOT NULL
           AND instance_id IN (SELECT instance_id FROM openstack_anomalous_instances),
       p.message
FROM enriched;
