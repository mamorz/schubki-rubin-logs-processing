#!/usr/bin/env bash
# Test current source in a disposable container; deployed services stay running.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# The production service's /tmp intentionally has noexec. Shell fixtures need
# their own executable bind mount; preserve the original scripts' mode bits.
mkdir -p "$ROOT/artifacts/compose-test-tmp"
exec "$ROOT/scripts/jury-compose.sh" run --rm --no-deps -T \
    -v "$ROOT:/workspace:ro" \
    -v "$ROOT/artifacts/compose-test-tmp:/test-tmp" \
    -w /workspace \
    -e RUN_SQL_BACKEND_INTEGRATION_TESTS=1 \
    -e RUN_LOCAL_HTTP_FIXTURE_TESTS=1 \
    mcp-server python -m pytest -q -p no:cacheprovider -rs --tb=short \
    --basetemp=/test-tmp/pytest "$@"
