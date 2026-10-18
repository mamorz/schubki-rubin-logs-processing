#!/usr/bin/env bash
# Optional: start the browser agent UI on top of the verified jury stack.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
compose() { "$ROOT/scripts/jury-compose.sh" "$@"; }

if [ "$#" -gt 0 ]; then
    echo "Usage: $0 (no arguments)" >&2
    exit 2
fi

"$ROOT/scripts/jury-up.sh"
compose --profile agent build agent-ui
compose --profile agent up -d --no-deps --wait --wait-timeout 120 agent-ui
echo "Agent UI ready: open http://localhost:18080 and sign in with your Kiro (AWS Builder ID) account."
