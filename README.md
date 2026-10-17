# RUBIN — Making Sense of Logs

One SereneDB SQL infrastructure and one read-only MCP server for BGL and OpenStack.
The integrated default uses the SQL teammate's `sql/` schema, indexes, loaders,
and `data/ownership.csv`. No schema or data is created by MCP startup.
No AI agent is included.

## Architecture and compatibility

`server.py` → MCP tool handlers → parameterized `QueryService` → SereneDB.
`MCP_DATASET_BACKEND=sql` is the default. Only the fixed tables/indexes below are
queried; no compatibility tables, views, or second default schema are created.

| MCP field / feature | SQL infrastructure |
| --- | --- |
| BGL corpus / full-text search | `bgl_logs` / `bgl_logs_idx` |
| OpenStack corpus / full-text search | `openstack_logs` / `openstack_logs_idx` |
| timestamp / severity / component / message | `ts` (TIMESTAMP without zone) / `level` / `component` / `message` |
| BGL `record_id` | `line_no` |
| OpenStack `record_id` | source-file ordinal × 1,000,000,000 + `line_no` |
| `event_id` | NULL: raw logs have no EventId or EventTemplate |
| team grouping | LEFT JOIN `ownership` on dataset + component; missing keys → `UNMAPPED` |

OpenStack ordinals are 1 for `openstack_abnormal`, 2 for `openstack_normal1`, and
3 for `openstack_normal2` (SQL source names have no `.log` suffix). IDs distinguish
identical line numbers across files. Use IDs returned by `search_logs` for nearby
lookups. `line_no` must be positive and below one billion, source identities must
be unique, and ownership must have one row per dataset/component. The preflight
below checks these assumptions. SQL loaders use `row_number() OVER ()` per file;
retain SereneDB's default insertion-order preservation and stable input files.

Main's mapping has 22 fictional assignments (10 BGL, 12 OpenStack), recorded in
provenance as `data/ownership.csv`, revision `main-a75f858` (the expected mapping
source, not an attestation of current database contents). Ownership is not an
authoritative organizational fact. MCP reports actual loaded counts, not an
assumed full-corpus size. `development_sample=false` denotes the SQL backend, not
proof that the entire corpus is loaded. Unavailable event IDs aggregate into a
NULL group. Existing response shapes, filters, limits, SQL/parameter provenance,
coverage, truncation, and chronological context are preserved.

The Compose default follows main: **SereneDB 26.09.1.3**. The earlier MCP sample
baseline was tested on 26.08.1; it does not certify main's DDL on that version.
Dictionary syntax is version-sensitive: the connected documentation MCP's
[dictionary reference](https://serenedb.com/docs/sql/statements/create_text_search_dictionary#examples)
describes a newer form than `sql/01_schema.sql`. The
[refresh reference](https://serenedb.com/docs/sql/statements/vacuum#refreshing--refresh_)
confirms that indexed rows need refresh before reliable search visibility.
Main's dictionary/index creation and `@@` search
still require validation against the selected SereneDB image. Do not silently
downgrade the database tag or alter an existing database to resolve this.

## Worktree-local setup

Prerequisites: Python 3.12 (the Docker runtime), Docker Desktop with Compose v2
on macOS, and an explicitly selected database. Each worktree needs its own virtual
environment and configuration; never copy credentials from another worktree.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
cp .env.example .env
```

`.env`, `.venv`, raw data, and artifacts are ignored by Git and Docker. The only
tracked file under `data/` is the small fictional ownership CSV. Keep secrets out
of commands, Git, screenshots, and `docker compose config` output.

## Docker: isolated SQL + MCP stack

Inspect the project name/ports first. Choose a unique `COMPOSE_PROJECT_NAME` if
`schubki-integration` is already in use. Default resources do not reference an
external volume or any original worktree's database.

| Service | Default host binding |
| --- | --- |
| MCP Streamable HTTP | `http://127.0.0.1:18000/mcp` → container 8000 |
| MCP process health | `http://127.0.0.1:18000/healthz` |
| SereneDB SQL | `127.0.0.1:17890` → container 7890 |
| Optional UI (`--profile ui`) | `http://127.0.0.1:16543` |

After approval to create/start a new development database:

```bash
docker compose config --quiet
docker compose up --build -d serenedb mcp-server
docker compose ps
curl --fail http://127.0.0.1:18000/healthz
```

SereneDB persists in `schubki-integration_serenedb-data` (or the chosen project
prefix). Do not use `down -v`, reuse an existing volume, or run schema SQL without
the database owner's approval. Trust authentication is for this isolated local
development network only. All published ports bind to loopback. Do not expose
either MCP or SereneDB publicly.

MCP uses `host=serenedb port=7890 dbname=postgres user=postgres` inside Compose by
default. An empty `SERENEDB_DOCKER_DSN` keeps this default. It runs non-root with a
read-only filesystem, HTTP health check, and `restart: unless-stopped`.
`depends_on` orders startup, not database readiness. `/healthz` and `health_check`
are process checks only; queries need a reachable, loaded, indexed database.

For an **existing approved database**, configure `SERENEDB_DOCKER_DSN` privately
in this worktree's `.env` and start only MCP:

```bash
docker compose up --build -d --no-deps mcp-server
```

On macOS, a host-local database uses `host.docker.internal` from the container.
Do not assume a DSN inherited from another shell/worktree points to the right DB.
For direct Python startup, set this worktree's `SERENEDB_DSN` and run
`.venv/bin/python server.py`; the entrypoint reads only the adjacent `.env` without
overriding explicitly exported variables. The example selects HTTP on port 18000.

## Data initialization is a separate, approved operation

A fresh Compose database has **no log tables**. The SQL teammate owns its setup:
`sql/01_schema.sql` creates the schema/dictionary; `02`/`03` parse raw BGL/OpenStack;
`04` loads ownership; `05` creates and refreshes inverted indexes. `scripts/download.sh`
retrieves the complete original archives into ignored `data/raw/`.

Do not run the SQL files as migrations on an existing database. The schema script
drops tables; the data scripts append and are not independently idempotent.
`scripts/load.sh` now refuses to run without the explicit
`--confirm-recreate-and-full-load` argument. That destructive/full-load workflow
is **not** part of startup or verification and requires separate approval.
For the next integration test, request a preloaded test endpoint or agree on a
fresh disposable database and bounded fixtures before loading anything.

The validated Python streaming ingestion pipeline and immutable manifests remain
available as an **optional alternate workflow**, not a competing default loader:
[full-ingestion documentation](docs/full_dataset_ingestion.md). Its versioned
`loghub_*` tables are not used by the SQL backend. Likewise the original 2,000-row
mode is preserved via `MCP_DATASET_BACKEND=sample`; see
[sample backend setup](docs/sample_backend.md). Do not run both loader families
as an initialization recipe. No full import has been performed for this merge.

## MCP tools and Kiro

The four tools are `health_check`, `search_logs`, `summarize_logs` (including
component, severity, event_id, time and team), and `get_nearby_events`. Search
returns at most 50 records; summaries at most 100 groups; nearby context at most
50 records on either side. Time filters are inclusive-start/exclusive-end ISO
timestamps without a timezone. All user filter values remain bound parameters.
Neither proximity nor log-record counts establish incident causation.

After the database is ready, run the real read-only HTTP verifier:

```bash
.venv/bin/python -m scripts.verify_mcp_http --url http://127.0.0.1:18000/mcp
```

Use [the Kiro example](examples/kiro/mcp.local.example.json) and
[local/remote setup guide](docs/kiro_mcp_integration.md). For another computer,
prefer an authenticated SSH tunnel to the loopback-only MCP port. No public URL
or authentication gateway is assumed or provisioned.

## Verification

Safe tests need no SereneDB and never load persistent data:

```bash
.venv/bin/python -m pytest -q
RUN_LOCAL_HTTP_FIXTURE_TESTS=1 .venv/bin/python -m pytest -q
docker compose config --quiet
```

The HTTP fixture option starts temporary loopback servers with in-memory SQLite
fixtures built from main's table DDL. It verifies MCP initialization, all four
tools, validation errors, both current/legacy protocols, joins, source IDs, and
NULL EventIds. Its `@@` translation to LIKE is **not a SereneDB full-text test**.

With an explicitly supplied, approved read-only `SERENEDB_DSN` for main's already
loaded schema, run:

```bash
.venv/bin/python -m scripts.check_sql_backend
RUN_SQL_BACKEND_INTEGRATION_TESTS=1 .venv/bin/python -m pytest tests/test_sql_backend_integration.py -q
RUN_MCP_HTTP_INTEGRATION_TESTS=1 .venv/bin/python -m pytest tests/test_mcp_http_integration.py -q
```

The preflight reads this worktree's `.env`; the live pytest test deliberately
requires the DSN in its process environment and does not search parent `.env`
files. The HTTP test targets port 18000 unless `MCP_TEST_URL` is set. Preflight
checks required projections, unique source IDs/mapping keys, null fields, actual
`@@` execution, and index/table visibility counts, using SELECT only. Live tests
also execute all aggregation modes, including `date_trunc` time buckets.

Legacy tests gated by `RUN_SERENEDB_INTEGRATION_TESTS` still require the separately
loaded sample/staging schema. The ownership reload test additionally requires
`ALLOW_SERENEDB_TEST_WRITES=1`; do not enable it on a read-only test endpoint.

Merge verification: **96 passed, 12 skipped** in the worktree-local Python 3.12
environment and in the built Docker image, with loopback fixture tests enabled.
The Docker test container used `--network none`, a read-only filesystem, and no
mounted volumes or database connection. Compose config validation, image build,
Python compilation, and dependency consistency checks also passed.
The skipped tests require real SereneDB/loaded data. The prior 96-test live result
belongs to the feature branch's sample environment, not this new SQL integration.
No commit, push, full load, or persistent database mutation is part of this setup.
