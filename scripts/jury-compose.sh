#!/usr/bin/env bash
# One project, one file, no dotenv overrides or inherited database endpoints.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec env -u SERENEDB_DSN -u SERENEDB_DOCKER_DSN \
    COMPOSE_PROJECT_NAME=schubki-jury COMPOSE_PROFILES= COMPOSE_DISABLE_ENV_FILE=1 \
    SERENEDB_PORT=17890 MCP_PUBLISHED_PORT=18000 SERENE_UI_PORT=16543 \
    docker compose --project-name schubki-jury --env-file /dev/null \
    --project-directory "$ROOT" -f "$ROOT/docker-compose.yml" "$@"
