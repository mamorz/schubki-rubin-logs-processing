-- Full-text (inverted) indexes. Built after the bulk load, then refreshed so
-- every loaded row is searchable before the first query.
-- Search queries select FROM the index by name: SELECT ... FROM bgl_logs_idx WHERE message @@ ...

CREATE INDEX bgl_logs_idx ON bgl_logs
    USING inverted (line_no, ts, node, alert_label, component, level, message log_dict);

CREATE INDEX openstack_logs_idx ON openstack_logs
    USING inverted (source_file, line_no, ts, service, level, component,
                    request_id, instance_id, http_status, message log_dict);

VACUUM (REFRESH_TABLE) bgl_logs;
VACUUM (REFRESH_TABLE) openstack_logs;
