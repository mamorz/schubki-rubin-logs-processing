-- Schema for the base corpus: BGL + OpenStack logs and the team-ownership table.
-- Create-only: jury_setup checks that the application database is empty first.
-- Deliberately no IF NOT EXISTS: concurrent/partial setup must fail before loads.

-- Log messages are mostly ASCII words, hex values and IDs. Lower-case, no
-- stemming, so "error" and "errors" stay distinct. position enables phrase
-- search, frequency enables relevance scoring. (SereneDB 26.09 syntax.)
CREATE TEXT SEARCH DICTIONARY log_dict (
    template = 'text',
    locale = 'en_US.UTF-8',
    case = 'lower',
    stemming = false,
    accent = false,
    frequency = true,
    position = true
);

-- BGL: BlueGene/L supercomputer RAS log, 4,747,963 lines, 2005-06-03 .. 2006-01-04.
CREATE TABLE bgl_logs (
    line_no     BIGINT    NOT NULL, -- 1-based line number in BGL.log (file order)
    ts          TIMESTAMP NOT NULL, -- local time as written in the log (microseconds)
    epoch       BIGINT    NOT NULL, -- Unix seconds from the first numeric field
    node        TEXT,               -- e.g. R02-M1-N0-C:J12-U11; NULL when logged as '-'
    alert_label TEXT,               -- alert category (e.g. KERNDTLB); NULL for non-alert lines
    is_alert    BOOLEAN   NOT NULL,
    type        TEXT      NOT NULL, -- RAS | NULL
    component   TEXT      NOT NULL, -- KERNEL, APP, MMCS, DISCOVERY, HARDWARE, ...
    level       TEXT      NOT NULL, -- INFO, WARNING, SEVERE, ERROR, FAILURE, FATAL
    message     TEXT      NOT NULL
);

-- OpenStack: nova-api / nova-compute / nova-scheduler logs, 207,820 lines.
CREATE TABLE openstack_logs (
    source_file   TEXT      NOT NULL, -- openstack_normal1 | openstack_normal2 | openstack_abnormal
    line_no       BIGINT    NOT NULL, -- 1-based line number within source_file
    log_file      TEXT      NOT NULL, -- original rotated log name, e.g. nova-api.log.1.2017-05-16_13:53:08
    service       TEXT      NOT NULL, -- nova-api | nova-compute | nova-scheduler
    ts            TIMESTAMP NOT NULL,
    pid           INTEGER,
    level         TEXT      NOT NULL, -- INFO, WARNING, ERROR, CRITICAL
    component     TEXT      NOT NULL, -- Python logger, e.g. nova.compute.manager
    request_id    TEXT,               -- req-<uuid>, NULL when not logged
    user_id       TEXT,
    tenant_id     TEXT,
    instance_id   TEXT,               -- from "[instance: <uuid>]" in the message
    http_method   TEXT,               -- wsgi access lines only
    http_path     TEXT,
    http_status   INTEGER,
    response_len  INTEGER,
    latency_s     DOUBLE,             -- request time in seconds ("time: 0.247")
    is_anomaly    BOOLEAN   NOT NULL, -- line mentions an instance from anomaly_labels.txt
    message       TEXT      NOT NULL
);

-- The 4 VM instances with injected anomalies (anomaly_labels.txt).
CREATE TABLE openstack_anomalous_instances (
    instance_id TEXT NOT NULL
);

-- Fictional ownership mapping: which team owns which log component.
CREATE TABLE ownership (
    dataset   TEXT NOT NULL, -- bgl | openstack
    component TEXT NOT NULL, -- matches bgl_logs.component / openstack_logs.component
    team      TEXT NOT NULL,
    area      TEXT NOT NULL  -- short description of what the component covers
);
