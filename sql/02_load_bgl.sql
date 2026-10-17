-- Load /data/raw/BGL.log into bgl_logs.
-- Line format: label epoch date node time node_repeat type component level message
-- A few hundred lines have a missing node or junk before "type component level",
-- so the regex anchors on that triple instead of on field positions.

INSERT INTO bgl_logs
WITH raw AS (
    SELECT row_number() OVER () AS line_no, line
    FROM read_csv('/data/raw/BGL.log',
                  columns = {'line': 'VARCHAR'}, delim = '\x01', quote = '', escape = '',
                  header = false, auto_detect = false)
), parsed AS (
    SELECT line_no,
           regexp_extract(line,
               '^(\S+) (\d+) (\S+) (\S+) (\d{4}-\d\d-\d\d-\d\d\.\d\d\.\d\d\.\d{6}) ?(.*?) ?(RAS|NULL) (\S+) (INFO|FATAL|ERROR|WARNING|SEVERE|FAILURE)(?: (.*))?$',
               ['label', 'epoch', 'date', 'node', 'ts', 'node_repeat', 'type', 'component', 'level', 'message']) AS p
    FROM raw
)
SELECT line_no,
       strptime(p.ts, '%Y-%m-%d-%H.%M.%S.%f'),
       p.epoch::BIGINT,
       nullif(p.node, '-'),
       nullif(p.label, '-'),
       p.label <> '-',
       p.type,
       p.component,
       p.level,
       coalesce(p.message, '')
FROM parsed;
