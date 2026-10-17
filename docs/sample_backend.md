# Optional sample backend (retained MCP workflow)

This is the legacy, explicitly opt-in workflow, not the integrated default.
Set `MCP_DATASET_BACKEND=sample` and supply a connection to a separate database
containing the sample tables before using it. Never run these loaders on an
unapproved database. For the integrated SQL backend and Docker setup, start with
the [repository README](../README.md).

## Status

This repository currently contains the implementation scaffold for the hackathon MVP.
The `search_logs`, `summarize_logs`, and `get_nearby_events` MCP tools are implemented
against the public Loghub 2,000-row structured BGL and OpenStack samples.
`summarize_logs` also supports team aggregation through a versioned, explicitly
fictional component-ownership mapping. The sample schemas and ownership assignments
are hackathon fixtures, not authoritative production data.

## Architecture

- `server.py` is the stable application entry point.
- `log_insights/mcp_server.py` creates and starts the MCP server.
- `log_insights/tools.py` owns MCP tool registration and tool descriptions.
- `log_insights/db.py` owns SereneDB connections.
- `log_insights/queries.py` owns parameterized query execution.
- `log_insights/policies.py` defines role and evidence requirements.
- `log_insights/ingestion.py` validates and loads the two public Loghub samples.
- `log_insights/ownership.py` validates and loads fictional component ownership.
- `scripts/load_data.py` is the sample-loader command-line entry point.
- `scripts/load_ownership.py` is the idempotent ownership-loader entry point.
- `log_insights/full_ingestion.py` streams the official raw archives through COPY.
- `scripts/load_full_staging.py` runs the bounded full-data dry run.
- `scripts/load_full_data.py` is the explicitly gated complete-load command.
- `manifests/full_dataset_sources_v1.json` pins archive and member checksums.
- `ownership/fictional_component_ownership_v1.csv` is the reviewed mapping source.
- `tests/` contains unit tests and opt-in live SereneDB integration tests.

The intended runtime is one Python MCP service connected to the organizer-provided
SereneDB instance. No separate web application or custom AI agent is planned.

## Local setup

Prerequisites: Python 3.11 or newer and access to the supplied SereneDB instance.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock
cp .env.example .env
```

Fill `SERENEDB_DSN` in `.env` after the organizers provide the connection string.
Also set `MCP_DATASET_BACKEND=sample`. Set `SERENEDB_DOCKER_DSN` to the equivalent container-to-host connection string.
On macOS this normally uses `host.docker.internal` instead of `127.0.0.1`.
Docker Compose reads `.env` automatically. For a direct Python run, export the
variables from that file in your shell before running:

```bash
python server.py
```

The server exposes a database-independent `health_check`, full-text `search_logs`,
database-aggregated `summarize_logs`, and chronological `get_nearby_events`.

## MCP tools

`search_logs` accepts:

- `dataset`: `bgl` or `openstack` (case-insensitive)
- `query`: a non-empty full-text search string
- `start_time`: optional inclusive ISO 8601 timestamp without a timezone
- `end_time`: optional exclusive ISO 8601 timestamp without a timezone
- `component`: optional exact component match
- `severity`: optional exact severity match
- `limit`: defaults to 20 and must be between 1 and 50

Example MCP arguments:

```json
{
  "dataset": "bgl",
  "query": "core",
  "start_time": "2005-06-05T00:00:00",
  "end_time": "2005-06-06T00:00:00",
  "component": "KERNEL",
  "severity": "INFO",
  "limit": 3
}
```

The structured response contains `records`, `returned_record_count`,
`total_corpus_match_count`, `truncated`, and `provenance`. Provenance reports the
canonical dataset, applied filters, parameterized record and count SQL, and their
parameters. `record_id` is the source `LineId`; `event_id` is the Loghub template
identifier and may repeat across records.

`summarize_logs` accepts:

- `dataset`: `bgl` or `openstack` (case-insensitive)
- `group_by`: `component`, `team`, `severity`, `event_id`, or `time`
- `interval`: required for `group_by: time`; either `hour` or `day`
- `start_time`: optional inclusive ISO 8601 timestamp without a timezone
- `end_time`: optional exclusive ISO 8601 timestamp without a timezone
- `component`: optional exact component match
- `severity`: optional exact severity match
- `limit`: maximum returned groups, defaults to 20 and must be between 1 and 100

Example component summary:

```json
{
  "dataset": "bgl",
  "group_by": "component",
  "severity": "ERROR",
  "limit": 10
}
```

Example fictional team summary:

```json
{
  "dataset": "openstack",
  "group_by": "team",
  "severity": "ERROR",
  "limit": 20
}
```

Example hourly trend:

```json
{
  "dataset": "bgl",
  "group_by": "time",
  "interval": "hour",
  "start_time": "2005-06-03T00:00:00",
  "end_time": "2005-06-04T00:00:00",
  "limit": 24
}
```

Aggregation happens in SereneDB over the complete filtered sample table.
`total_matching_record_count` is therefore unaffected by the returned-group limit.
Responses label the source as a 2,000-row development sample and define counts as
log records, not distinct incidents. Team summaries use a `LEFT JOIN`; components
without a mapping are retained under the `UNMAPPED` team. Their responses add
`mapping_coverage` with mapped and unmapped record/component counts and percentages.
Provenance includes all executed aggregation, total-count, and coverage SQL and
parameters, applied filters, truncation, the mapping file/table/version, and an
explicit warning that ownership is fictional and non-authoritative.

## Fictional component ownership

The v1 mapping covers the 15 component names confirmed in the checked-in BGL and
OpenStack samples (5 BGL and 10 OpenStack). It intentionally uses invented team
assignments for the hackathon demonstration. Do not interpret these assignments as
real IBM, OpenStack, Nova, or operator ownership.

Load or refresh only the project-owned mapping table:

```bash
python -m scripts.load_ownership
```

The loader validates the exact 15 `(dataset, component)` keys before executing SQL,
then transactionally replaces `fictional_component_ownership_v1` and verifies its
row count. It is safe to rerun and never alters the BGL/OpenStack tables or indexes.
The source of truth is
`ownership/fictional_component_ownership_v1.csv`; a future mapping must use a new
versioned file/table rather than silently changing v1 semantics.

`get_nearby_events` accepts:

- `dataset`: `bgl` or `openstack` (case-insensitive)
- `record_id`: positive source `LineId`; this is not the repeating `EventId`
- `before`: number of earlier events, defaults to 10 and must be between 0 and 50
- `after`: number of later events, defaults to 10 and must be between 0 and 50
- `same_component`: when true, restrict neighbors to the anchor's component

Example:

```json
{
  "dataset": "openstack",
  "record_id": 7,
  "before": 3,
  "after": 3,
  "same_component": true
}
```

The response keeps the anchor separate from `before_events` and `after_events`.
Events are ordered by timestamp and then `LineId`. Every returned event includes a
signed `seconds_from_anchor` and a `gap_from_previous_seconds`; a zero gap is valid
when records share a timestamp. Fewer events may be returned at a sample boundary or
when component filtering excludes records. Temporal proximity shows sequence only
and does not establish causation.

## Public Loghub sample ingestion

Download only these two structured 2,000-row samples into the gitignored
`data/raw/` directory:

```bash
mkdir -p data/raw
curl -fL -o data/raw/BGL_2k.log_structured.csv \
  https://raw.githubusercontent.com/logpai/loghub/master/BGL/BGL_2k.log_structured.csv
curl -fL -o data/raw/OpenStack_2k.log_structured.csv \
  https://raw.githubusercontent.com/logpai/loghub/master/OpenStack/OpenStack_2k.log_structured.csv
```

With the local `.env` configured, load both files from the repository root:

```bash
python -m scripts.load_data
```

The command validates the exact CSV headers, requires exactly 2,000 rows per file,
rejects duplicate `LineId` values, parses timestamps, replaces both project-owned
sample tables in one transaction, rebuilds their full-text indexes, and prints a
non-secret validation report. It is safe to rerun: it only clears these two tables:

- `loghub_bgl_sample_v1`
- `loghub_openstack_sample_v1`

The full-text index relations are `loghub_bgl_sample_v1_fts` and
`loghub_openstack_sample_v1_fts`. Queries use SereneDB's index-relation syntax, for
example:

```sql
SELECT line_id, event_time, component, severity, message
FROM loghub_bgl_sample_v1_fts
WHERE message @@ 'core'
ORDER BY line_id
LIMIT 5;
```

Run unit tests without a database, then opt into the live integration suite after
loading the samples and ownership mapping:

```bash
python -m pytest -q
python -m scripts.load_ownership
MCP_DATASET_BACKEND=sample RUN_SERENEDB_INTEGRATION_TESTS=1 python -m pytest -q
```

The ownership reload test additionally requires `ALLOW_SERENEDB_TEST_WRITES=1`;
leave it unset for read-only verification. The staging tests need preloaded
staging tables too. These historical live prerequisites are not bundled in a
fresh checkout or supplied by the default SQL backend.

## Full original dataset ingestion

The scalable loader and completed bounded dry-run are documented in
[`full_dataset_ingestion.md`](full_dataset_ingestion.md). Raw archives and
generated large files stay under the gitignored `data/` directory. The bounded
command loads only the separate `*_full_v1_staging` tables:

```bash
python -m scripts.inspect_full_archives
python -m scripts.load_full_staging
python -m scripts.report_full_staging
```

With `MCP_DATASET_BACKEND=sample`, the MCP server uses the 2,000-row sample tables. Loading either
`*_full_v1` production table requires a separate explicit review step; it is not
performed by these commands.

## Docker

Build and run only after creating `.env` and filling `SERENEDB_DOCKER_DSN` with
the supplied connection string adjusted for container networking:

```bash
docker compose up --build -d --no-deps mcp-server
docker compose ps
curl --fail http://127.0.0.1:18000/healthz
python -m scripts.verify_mcp_http
```

The Streamable HTTP MCP endpoint is `http://127.0.0.1:18000/mcp`. Compose publishes
only to host loopback by default, while the process listens on container port 8000.
`MCP_PUBLISHED_PORT` can change the host port. The container runs as a non-root
user with a read-only filesystem, reports health through `/healthz`, and restarts
unless explicitly stopped. Docker installs the exact tested dependency set from
`requirements.lock`.

For Kiro on the same computer, use the safe example in
[`examples/kiro/mcp.local.example.json`](../examples/kiro/mcp.local.example.json).
For another computer, keep the loopback binding and use an SSH local-forward;
never publish this unauthenticated endpoint directly. Exact Kiro configuration,
local and remote instructions, and the requirements for an authenticated HTTPS
gateway are in [`kiro_mcp_integration.md`](kiro_mcp_integration.md).

Run the network-boundary integration test while the container is running:

```bash
RUN_MCP_HTTP_INTEGRATION_TESTS=1 python -m pytest \
  tests/test_mcp_http_integration.py -q
```

## Inputs still required

- SereneDB DSN and credentials, if credentials are required
- Authoritative production component-to-team ownership (the current v1 map is
  fictional)
- Confirmation of the exact MCP host used for judging

The public Loghub schemas and fictional ownership mapping are represented only for
the sample-backed hackathon workflow. Do not treat them as the organizer's final
schema, add guessed dataset columns, or commit credentials.
