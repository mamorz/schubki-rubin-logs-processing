#!/usr/bin/env bash
# Safe first setup and repeat startup. Never remove containers, volumes or data.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
compose() { "$ROOT/scripts/jury-compose.sh" "$@"; }

if [ "$#" -gt 0 ]; then
    echo "Usage: $0 (no arguments)" >&2
    exit 2
fi
command -v docker >/dev/null || { echo "Install Docker with Compose v2 first." >&2; exit 1; }
compose config --quiet
docker info >/dev/null

# Host lock also protects downloads. Database CREATE without IF NOT EXISTS is
# the second guard against concurrent initializers from different checkouts.
mkdir -p "$ROOT/data"
LOCK="$ROOT/data/.jury-up.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
    echo "Another setup may be running: $LOCK. See README recovery instructions." >&2
    exit 1
fi
trap 'rmdir "$LOCK"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"$ROOT/scripts/download.sh"
compose build mcp-server
# Preserve an existing container's image/config as well as its named data volume.
compose up -d --no-recreate serenedb
compose run --rm -T --no-deps mcp-server python -m scripts.jury_setup --initialize-empty
compose up -d --no-deps --wait --wait-timeout 120 mcp-server
compose exec -T mcp-server python -m scripts.verify_mcp_http --url http://127.0.0.1:8000/mcp
echo "Jury setup verified. Connect Claude Code or Codex to http://localhost:18000/mcp"
