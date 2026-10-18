# Operations: guarded setup, recovery and verification

Details behind the one-command setup in the [README](../README.md). Judges don't need this page
to run the project; it documents what the scripts guarantee and how to verify or recover them.

## What `scripts/jury-up.sh` does

Every Compose operation goes through `scripts/jury-compose.sh`, which explicitly selects the
**`schubki-jury`** project, this repository's Compose file and an empty environment file. It fixes
the published ports and ignores inherited project names, alternate Compose files, profiles and
external database DSNs. MCP and the setup helper connect only to `serenedb:7890/postgres` on the
project's own Compose network. The pinned database image is SereneDB **26.09.1.3**; the named
volume **`schubki-jury_serenedb-data`** is retained across runs.

The script validates Compose, verifies/downloads the original BGL and OpenStack files against the
checked-in SHA-256 manifest, builds the MCP image, starts SereneDB and waits up to 120 seconds for
SQL readiness. It inspects the catalog before deciding what to do:

- **Empty application database:** run `sql/01_schema.sql` through `sql/05_indexes.sql` in order,
  then verify the full corpus and index visibility. SereneDB's bundled `sdb_docs` objects are
  recognised separately.
- **Complete database:** verify 4,747,963 BGL rows, 207,820 OpenStack rows (all three sources),
  four anomaly labels, 22 ownership rows, schema projections, unique identities and working, fully
  visible search indexes. Skip all loads.
- **Partial or unknown state:** stop with an error. No automatic repair, table replacement, index
  rebuild or append is attempted. Catalog/query errors also stop setup.

`01_schema.sql` is create-only. Its first unconditional dictionary creation also makes concurrent
initialization fail before a second loader can append rows. A local lock prevents simultaneous
startup/download runs from the same checkout. SQL scripts are not migrations: do not run
individual load scripts on an existing database. The legacy `scripts/load.sh` routes through the
same guarded workflow; its old recreate flag is rejected.

Once data is ready, setup starts MCP, waits for its HTTP health check and runs
`scripts.verify_mcp_http` inside the container against the real HTTP endpoint. Success is printed
only after transport, tool schemas and the tool smoke checks pass. MCP exposes only read-only
query tools and runs as a non-root user with a read-only filesystem. SQL trust authentication is
intended for this isolated local Compose network; the database account itself is not restricted
by SQL grants.

Running `./scripts/jury-up.sh` again restarts and rechecks the stack. Source checksums and SELECT
checks run again, but the five million log lines are **not reloaded**. Keep this checkout's
`data/` directory for the SereneDB bind mount. Setup does not recreate an existing SereneDB
container, upgrade its image or remove volumes.

`scripts/agent-ui-up.sh` runs `jury-up.sh` first and then builds and starts the `agent-ui`
service (Compose profile `agent`) on `http://localhost:18080`.

## Status, shutdown and recovery

```bash
./scripts/jury-compose.sh ps
./scripts/jury-compose.sh logs --tail=100 serenedb mcp-server
./scripts/jury-compose.sh --profile agent logs --tail=100 agent-ui
# Stop services while retaining containers and every volume:
./scripts/jury-compose.sh --profile agent stop
# Start again and verify:
./scripts/agent-ui-up.sh        # or ./scripts/jury-up.sh for the MCP server only
```

If ports 17890, 18000 or 18080 are occupied by another project, stop that project through its
owner before starting this stack. Do not redirect this setup to another database. If a download
fails, rerun setup: only absent files are fetched, and partial downloads/extractions never become
final files. A corrupt existing source or archive causes refusal and is preserved for inspection.

If initialization fails midway (for example, disk exhaustion), preserve the volume and ask the
maintainers to inspect it. Setup intentionally refuses to resume a partial load and never resets
data or repairs an uncertain state. If an interrupted process leaves `data/.jury-up.lock`, first
confirm that no setup or initializer is still running, then remove **only that empty lock
directory** with `rmdir data/.jury-up.lock` and retry. Never remove or prune database volumes.

The Kiro login of the agent UI lives in the `schubki-jury_kiro-home` volume; the header's
sign-out button (or deleting that volume) removes it.

## Verification

Repeat the automated MCP verification without importing data:

```bash
./scripts/jury-compose.sh exec -T mcp-server python -m scripts.verify_mcp_http --url http://127.0.0.1:8000/mcp
# Also check pattern aggregation, OpenStack tracing, spawn performance and BGL spikes on the full corpus:
./scripts/jury-compose.sh exec -T mcp-server python -m scripts.verify_mcp_http --url http://127.0.0.1:8000/mcp --patterns --tracing --performance --spikes
curl --fail http://localhost:18000/healthz
```

`/healthz` and the `health_check` tool check the process; the other tools exercise the database.

Every SQL statement issued by the MCP server has a deadline, `MCP_SQL_TIMEOUT_SECONDS` (default
15 s, allowed 0.001–3600). The full-corpus verification above finishes in a few seconds on a
laptop. On a much slower machine, raise it for the session; the setup wrapper passes the shell
variable through even though it ignores `.env` files:

```bash
MCP_SQL_TIMEOUT_SECONDS=60 ./scripts/agent-ui-up.sh     # or ./scripts/jury-up.sh
```

Run the complete Python suite in Compose against the running database (source mounted read-only,
deployed services keep running):

```bash
./scripts/test-compose.sh
./scripts/test-compose.sh tests/test_jury_setup.py      # optional selection
```

The runner mounts shell fixtures on a separate executable directory under ignored `artifacts/`
because the MCP service's `/tmp` is `noexec`. It enables the SELECT-only SQL and temporary HTTP
tests.

Safe tests that need no SereneDB (Python 3.12 virtual environment with `requirements.lock`):

```bash
.venv/bin/python -m pytest -q
RUN_LOCAL_HTTP_FIXTURE_TESTS=1 .venv/bin/python -m pytest -q
./scripts/jury-compose.sh config --quiet
```

The HTTP fixture option starts temporary loopback servers with in-memory SQLite fixtures built
from the table DDL; its `@@` translation to `LIKE` is **not a SereneDB full-text test**.

With an explicitly supplied, approved read-only `SERENEDB_DSN` for the loaded schema:

```bash
.venv/bin/python -m scripts.check_sql_backend
RUN_SQL_BACKEND_INTEGRATION_TESTS=1 .venv/bin/python -m pytest tests/test_sql_backend_integration.py -q
RUN_MCP_HTTP_INTEGRATION_TESTS=1 .venv/bin/python -m pytest tests/test_mcp_http_integration.py -q
```

The preflight checks required projections, unique source IDs/mapping keys, null fields, actual `@@`
execution and index/table visibility counts, using SELECT only. Legacy tests gated by
`RUN_SERENEDB_INTEGRATION_TESTS` require the separately loaded sample/staging schema; the
ownership reload test additionally requires `ALLOW_SERENEDB_TEST_WRITES=1` and must not run against
a read-only endpoint.

Agent UI tests (Node 22, or the `node:22` image):

```bash
cd agent-ui && npm ci && npm run typecheck && npm test
```

They replay a recorded Kiro ACP session and check the card renderer against real responses of all
five MCP tools (`agent-ui/test/fixtures/`).

## Developer setup and alternate loaders

For Python-only development, create a Python 3.12 virtual environment and install
`requirements.lock`. Configure an explicit `SERENEDB_DSN` only for direct Python commands; Compose
deliberately ignores external DSNs. `.env`, `.venv` and raw data are ignored by Git and Docker;
the ownership CSV is tracked.

The Python streaming ingestion pipeline is an optional separate workflow
([full-ingestion documentation](full_dataset_ingestion.md)); its versioned `loghub_*` tables are
not used by the SQL backend. The original 2,000-row sample backend is available with
`MCP_DATASET_BACKEND=sample` ([sample backend setup](sample_backend.md)). Do not mix these loaders
into the jury database.

## SereneDB version notes

The Compose default is **SereneDB 26.09.1.3**, on which the complete corpus, dictionary/index
creation and all MCP tools were verified. Dictionary syntax is version-sensitive: the
[dictionary reference](https://serenedb.com/docs/sql/statements/create_text_search_dictionary#examples)
describes a newer form than `sql/01_schema.sql`, and the
[refresh reference](https://serenedb.com/docs/sql/statements/vacuum#refreshing--refresh_) confirms
that indexed rows need a refresh before reliable search visibility. Do not silently downgrade the
database tag or alter an existing database to resolve version incompatibilities.
