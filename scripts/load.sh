#!/usr/bin/env bash
# Compatibility entry point: initialization now uses the guarded jury workflow.
set -euo pipefail

if [ "$#" -ne 0 ]; then
    echo "Recreate/full-load flags are no longer supported. Run ./scripts/jury-up.sh." >&2
    exit 2
fi

exec "$(dirname "$0")/jury-up.sh"
