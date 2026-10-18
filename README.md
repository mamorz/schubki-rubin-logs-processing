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
The complete corpus, dictionary/index creation, and all four MCP tools were
previously verified on SereneDB 26.09.1.3. Do not silently downgrade the database
tag or alter an existing database to resolve version incompatibilities.

## Judges: one-command local setup

Prerequisites: Docker Engine/Desktop with **Compose v2** supporting `up --wait`
and `--wait-timeout`, a running Docker
daemon, Bash, `curl`, `tar`, `unzip`, and `sha256sum` or `shasum`. On Windows,
use WSL2 with Docker integration. Host Python and a host database are not needed.
Allow approximately **4 CPUs, 8 GB RAM allocated to Docker, and 15 GB free disk**
as a planning budget, not a measured minimum. The raw corpus is about 805 MB,
archives about 63 MB; images, database storage, and full-text indexes need extra
space. Initial download, import, and indexing can take several minutes or longer
on slower machines. Internet access is needed for images, Python packages, and
Zenodo on the first run. No GPU is required.

```bash
git clone https://github.com/mamorz/schubki-rubin-logs-processing.git
cd schubki-rubin-logs-processing
# Until the jury setup is merged, select the published jury-docker branch.
git switch jury-docker
./scripts/jury-up.sh
```

The setup is currently being developed on `jury-docker`; the commands above
require these changes to be published on that branch before judges clone it.
No `.env` file or credentials are needed. **Tailscale is NOT required.** Docker
and the MCP client run on the same computer; all published ports bind to loopback.

| Service | Local endpoint |
| --- | --- |
| MCP Streamable HTTP | `http://localhost:18000/mcp` |
| MCP process health | `http://localhost:18000/healthz` |
| SereneDB SQL | `127.0.0.1:17890` |
| Optional UI | `http://localhost:16543` |

Every Compose operation in the jury workflow goes through
`scripts/jury-compose.sh`, which explicitly selects **`schubki-jury`**, this
repository's Compose file, and an empty environment file. It fixes the published
ports and ignores inherited project names, alternate Compose files, profiles,
and external database DSNs. MCP and the setup helper connect only to
`serenedb:7890/postgres` on the project's own Compose network. The pinned database
image is SereneDB **26.09.1.3**. The existing named volume
**`schubki-jury_serenedb-data`** is retained.

The script validates Compose, verifies/downloads the original BGL and OpenStack
files against the checked-in SHA-256 manifest, builds the existing Dockerfile,
starts SereneDB, and waits up to 120 seconds for SQL readiness. It inspects the
catalog before deciding what to do:

- **Empty application database:** run `01_schema.sql` through `05_indexes.sql`,
  in order, then verify the full corpus and index visibility. SereneDB's bundled
  `sdb_docs` objects are recognized separately.
- **Complete database:** verify 4,747,963 BGL rows, 207,820 OpenStack rows (all
  three sources), four anomaly labels, 22 ownership rows, schema projections,
  unique identities, and working, fully visible search indexes. Skip all loads.
- **Partial or unknown state:** stop with an error. No automatic repair, table
  replacement, index rebuild, or append is attempted. Catalog/query errors also
  stop setup.

`01_schema.sql` is create-only. Its first unconditional dictionary creation also
makes concurrent initialization fail before a second loader can append rows.
A local lock prevents simultaneous startup/download runs from the same checkout.
SQL scripts are not migrations: do not run individual load scripts on an existing
DB. The legacy `scripts/load.sh` routes through the same guarded workflow;
its old recreate flag is rejected.

Once data is ready, setup starts MCP, waits for its HTTP health check, and runs
`scripts.verify_mcp_http` inside the container against the real HTTP endpoint.
Success is printed only after transport, tool schemas and the original four
tool smoke checks pass. The optional `--performance` verifier additionally checks
the full-corpus spawn analytics contract. MCP continues to expose only
read-only query tools and runs as a non-root user with a read-only filesystem.
SQL trust authentication is intended for this isolated local Compose network;
the database account itself is not restricted by SQL grants.

Run `./scripts/jury-up.sh` again to restart/recheck the stack. Source checksums
and SELECT checks run again, but the five million logs are **not reloaded**.
Keep this checkout's `data/` directory for the SereneDB bind mount. Setup does not
recreate an existing SereneDB container, upgrade its image, or remove volumes.

## Connect Claude Code or Codex

Choose either client, installed locally:

```bash
# Claude Code
claude mcp add --transport http rubin http://localhost:18000/mcp
claude mcp list

# Codex CLI (also configures the Codex IDE extension)
codex mcp add rubin --url http://localhost:18000/mcp
codex mcp list
```

See the official [Claude Code MCP instructions](https://code.claude.com/docs/en/mcp)
and [Codex HTTP MCP setup](https://developers.openai.com/learn/docs-mcp).
Restart the client/session if necessary. The tools should be `health_check`,
`search_logs`, `summarize_logs`, and `get_nearby_events`. Ask the client to call
`health_check`, search BGL for `core`, summarize OpenStack by `team`, and get nearby
events for a BGL record ID returned by the search. The ownership mapping is fictional.

### Optional: browser agent UI (Kiro)

If you don't want to configure a local client, run `./scripts/agent-ui-up.sh` and open
`http://localhost:18080`. It starts the same verified stack plus a container that runs Kiro CLI
against this MCP server. Sign in with an AWS Builder ID (device code shown on the page), choose
Engineer, Team lead or Manager, and every tool call is shown with its SQL, parameters, counts
and truncation. See [agent-ui/README.md](agent-ui/README.md).

To repeat the automated four-tool verification without importing data:

```bash
./scripts/jury-compose.sh exec -T mcp-server python -m scripts.verify_mcp_http --url http://127.0.0.1:8000/mcp
curl --fail http://localhost:18000/healthz
```

`/healthz` and the `health_check` tool check the process; the other three tools
exercise the database. No Tailscale, remote endpoint, public URL, or tunnel is
needed for local judging.

## Status, shutdown, and recovery

Run these commands from the repository:

```bash
./scripts/jury-compose.sh ps
./scripts/jury-compose.sh logs --tail=100 serenedb mcp-server
# Stop services while retaining containers and every volume:
./scripts/jury-compose.sh stop
# Start again and verify:
./scripts/jury-up.sh
```

If ports 17890 or 18000 are occupied by another project, stop that project through
its owner before starting the jury stack. Do not redirect this setup to another
database. If a download fails, rerun setup: only absent files are fetched, and
partial downloads/extractions never become final files. A corrupt existing source
or an archive needed for extraction causes refusal and is preserved for inspection.

If initialization fails midway (for example, disk exhaustion), preserve the volume
and ask the maintainers to inspect it. Setup intentionally refuses to resume a
partial load. It never resets data or repairs an uncertain state. If an interrupted
process leaves `data/.jury-up.lock`, first confirm that no jury setup or initializer
is still running, then remove **only that empty lock directory** with
`rmdir data/.jury-up.lock` and retry. Never remove or prune database volumes.

## Developer setup and alternate loaders

For Python-only development, create a virtual environment with Python 3.12 and
install `requirements.lock`. Configure an explicit `SERENEDB_DSN` only for direct
Python commands. Compose deliberately ignores external DSNs. `.env`, `.venv`,
and raw data remain ignored by Git and Docker; the ownership CSV is tracked.

The existing Python streaming ingestion pipeline remains an optional separate
workflow: [full-ingestion documentation](docs/full_dataset_ingestion.md). Its
versioned `loghub_*` tables are not used by the jury SQL backend. The original
2,000-row sample backend is available for direct Python development with
`MCP_DATASET_BACKEND=sample`; see [sample backend setup](docs/sample_backend.md).
Do not mix these loaders into the jury database.

## MCP tools and Kiro

The five tools are `health_check`, `search_logs`, `summarize_logs` (including
component, severity, event_id, time and team), `get_nearby_events`, and
`analyze_vm_spawn_performance`. Search
returns at most 50 records; summaries at most 100 groups; nearby context at most
50 records on either side. Time filters are inclusive-start/exclusive-end ISO
timestamps without a timezone. All user filter values remain bound parameters.
Neither proximity nor log-record counts establish incident causation.

`analyze_vm_spawn_performance` computes exact OpenStack spawn-duration count,
mean, median, P95/P99, min/max and slow-event counts over the complete population.
It discovers slow events without a VM ID using an absolute threshold or the
default baseline P95 × 1.25, with hourly/daily trends and bounded evidence.
Stored anomaly labels are optional validation metadata and never drive detection.
See [the interface, full-corpus results, tests and benchmarks](docs/vm_spawn_performance.md).

`search_logs` accepts optional `match_mode`: `any` (default, existing OR full-text
behavior), `phrase` (adjacent ordered tokens anywhere in a message), `exact`
(equality with the entire stored message, including case and whitespace), or `all`
(all tokens, any order). Phrase matching uses the existing index dictionary;
it is not raw string equality. All modes retain filters, complete filtered counts,
limits, SQL/parameter provenance, and truncation reporting. See
[search mode semantics, verification, and timings](docs/search_match_modes.md).

For SQL-backed OpenStack, `search_logs` also accepts literal `request_id` and
`instance_id` filters, permits omitting `query` when either is supplied, and
returns service/request/VM/HTTP/anomaly metadata. Structured VM equality covers
compute records primarily; it does not reconstruct a complete lifecycle. See
[OpenStack tracing examples, limits, tests and Compose verification](docs/openstack_tracing_mcp.md).

`summarize_logs` accepts the same optional `query` and `match_mode` for pattern
counts, component rankings, fictional team attribution and time trends over all
matching records. Omitting `query` preserves the existing behavior. Time buckets
also support `week` and `month`; optional `sort_direction='desc'` shows newest
periods first. Totals remain independent of the returned-group limit. See
[the tool schema, live examples, verification, benchmarks and Windows MCP-only update instructions](docs/pattern_aggregation.md).

For OpenStack request/instance tracing, field coverage, tested SELECT examples
and the slow-boot demo scenario, see
[OpenStack incident tracing](docs/openstack_incident_tracing.md). VM lifecycle
correlation (UUID + request_id) is covered in
[OpenStack VM tracing](docs/openstack_vm_tracing.md).

After the database is ready, run the real read-only HTTP verifier:

```bash
.venv/bin/python -m scripts.verify_mcp_http --url http://127.0.0.1:18000/mcp
# After updating MCP with pattern aggregation support:
.venv/bin/python -m scripts.verify_mcp_http --url http://127.0.0.1:18000/mcp --patterns
# After updating MCP with all advanced analytics:
.venv/bin/python -m scripts.verify_mcp_http --url http://127.0.0.1:18000/mcp --patterns --tracing --performance
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
./scripts/jury-compose.sh config --quiet
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

To run the complete suite in Compose against the already-running jury database
using the existing MCP image and current source:

```bash
./scripts/test-compose.sh
# Optional pytest selection:
./scripts/test-compose.sh tests/test_jury_setup.py
```

The test runner mounts source read-only and shell fixtures on a separate
executable directory under ignored `artifacts/`. Plain pytest inside the MCP
service uses its production `/tmp`, which has `noexec` and cannot run temporary
shell scripts even with mode 0755. The runner enables the SELECT-only SQL and
temporary HTTP tests; it does not restart deployed services.

Legacy tests gated by `RUN_SERENEDB_INTEGRATION_TESTS` still require the separately
loaded sample/staging schema. The ownership reload test additionally requires
`ALLOW_SERENEDB_TEST_WRITES=1`; do not enable it on a read-only test endpoint.

The jury setup safety tests use fake catalogs and a recording Docker CLI; they
never initialize a real database. Implementation validation also runs Compose
configuration validation and SELECT-only checks against the existing complete
jury database. A fresh end-to-end initialization must still be rehearsed on an
explicitly authorized empty installation before distribution; it is intentionally
not run against existing volumes during development.
