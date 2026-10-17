#!/usr/bin/env bash
# (Re)create the schema and load BGL, OpenStack and the ownership table into the
# running SereneDB. DESTRUCTIVE: requires explicit operator approval.
set -euo pipefail

if [ "${1:-}" != "--confirm-recreate-and-full-load" ]; then
    echo "Refusing to drop schema and load full corpora without --confirm-recreate-and-full-load" >&2
    exit 2
fi

cd "$(dirname "$0")/.."

psql() { docker compose run --rm -T psql -q "$@"; }

for f in 01_schema 02_load_bgl 03_load_openstack 04_load_ownership 05_indexes; do
    echo "== $f"
    psql -f "/sql/$f.sql" > /dev/null
done

psql -c "
SELECT 'bgl_logs' AS table_name, count(*) AS row_count FROM bgl_logs
UNION ALL SELECT 'openstack_logs', count(*) FROM openstack_logs
UNION ALL SELECT 'openstack_anomalous_instances', count(*) FROM openstack_anomalous_instances
UNION ALL SELECT 'ownership', count(*) FROM ownership;"
