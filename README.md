# RUBIN — Making Sense of Logs

RUBIN turns the BGL and OpenStack log corpora into evidence-backed answers for three roles:

- the **Engineer** asks what failed and what happened just before and after;
- the **Team lead** asks which problems recur, how often, and which teams are involved;
- the **Manager** asks what deserves attention first.

SereneDB does the searching and counting. A read-only **MCP server** exposes that as five tools,
and three **Kiro role agents** decide which tools to call and explain the results. Each answer
shows its evidence: tool calls, executed SQL, bound parameters, counts and truncation. A
**browser UI** runs the agents for you, so the only thing a judge needs besides Docker is a free
Kiro sign-in.

| Brief deliverable | Where |
|---|---|
| Setup, how to run it, workflow | [1. Setup from zero](#1-setup-from-zero) |
| Tools with descriptions, inputs and outputs | [2. What we built](#2-what-we-built) |
| One worked investigation with search and aggregation evidence | [3. Worked investigation](#3-worked-investigation) |
| Architecture | [4. Architecture](#4-architecture) |
| Known limitations | [5. Known limitations](#5-known-limitations) |

---

## 1. Setup from zero

> **Recommended for judges: use the RUBIN agent UI with Kiro** ([1.4](#14-connect-kiro--strongly-recommended-the-rubin-agent-ui)).
> It is one command, needs no local AI client configuration, runs our three role agents exactly as
> we tested them, and shows the SQL evidence behind every answer.

### 1.1 Prerequisites

| Needed | Notes |
|---|---|
| **Docker** Engine or Desktop with **Compose v2** (`up --wait` support) | The daemon must be running. On Windows use WSL2 with Docker integration. |
| **Bash, `curl`, `tar`, `unzip`, `sha256sum`** (or `shasum`) | Used by the setup scripts to download and verify the data. |
| **Internet access on the first run** | Docker images, Python and Node packages, and the corpus from Zenodo. |
| **Resources** | About 4 CPUs, 8 GB RAM for Docker and 15 GB free disk (raw corpus ~805 MB, plus database and full-text indexes). No GPU. |
| **For the UI: a Kiro account** | A free AWS Builder ID is enough; you sign in on the page, nothing is installed locally. |
| Optional, for direct MCP use | Claude Code, Codex, Kiro CLI/IDE or any client that supports MCP over Streamable HTTP. |

No host Python, Node or database, no `.env` file, no credentials and no tunnel are needed. All
ports are published on `127.0.0.1` only.

### 1.2 Get the code

```bash
git clone https://github.com/mamorz/schubki-rubin-logs-processing.git
cd schubki-rubin-logs-processing
```

### 1.3 Start the MCP server and connect an agent

```bash
./scripts/jury-up.sh
```

On the first run this:
- downloads BGL and OpenStack and checks them against the SHA-256 manifest;
- starts SereneDB 26.09.1.3;
- creates the schema, loads 4,747,963 BGL and 207,820 OpenStack log lines plus the ownership
  table, and builds the full-text indexes;
- starts the MCP server and checks every tool over HTTP.

Expect several minutes. Later runs only verify; nothing is reloaded. When it prints
`Jury setup verified`, the server is ready:

| Service | Endpoint |
|---|---|
| MCP (Streamable HTTP) | `http://localhost:18000/mcp` |
| MCP process health | `http://localhost:18000/healthz` |
| SereneDB (PostgreSQL protocol) | `127.0.0.1:17890` |
| Optional SQL web UI | `./scripts/jury-compose.sh --profile ui up -d serene-ui` → `http://localhost:16543` |

Connect any MCP client to `http://localhost:18000/mcp`. The server is named `rubin-log-insights`
and exposes `health_check`, `search_logs`, `summarize_logs`, `get_nearby_events` and
`analyze_vm_spawn_performance`.

```bash
# Claude Code
claude mcp add --transport http rubin http://localhost:18000/mcp

# Codex CLI (also configures the Codex IDE extension)
codex mcp add rubin --url http://localhost:18000/mcp
```

Cursor and other clients: add `http://localhost:18000/mcp` as a remote (HTTP) MCP server. A
quick check is to ask the client to call `health_check`, search BGL for `kernel panic`,
summarize OpenStack by `team`, and get nearby events for a record ID returned by the search.

These general-purpose clients get the tools but not our role agents. The role prompts live in
`.kiro/agents/*.json` (sources in `.kiro/agents/prompts/`) and can be reused as instructions.

### 1.4 Connect Kiro — strongly recommended: the RUBIN agent UI

**We strongly recommend judging RUBIN through the agent UI.** It starts everything above, plus a
container running Kiro CLI with our three role agents preconfigured against the MCP server:

```bash
./scripts/agent-ui-up.sh          # runs jury-up.sh, then builds and starts the UI
```

Open **http://localhost:18080** and:

1. Click **Sign in with Kiro**. The page shows a one-time code and a link. Confirm the code in
   any browser with your AWS Builder ID. The login stays in a local Docker volume.
2. Choose **Engineer**, **Team lead** or **Manager** in the dropdown next to the send button.
   Ask a question or click a suggested one.
3. Follow the **agent workflow** in the chat: reasoning steps and tool calls appear in the order
   the agent works. Each card shows the arguments, results (tables, charts, timelines), executed
   SQL with its parameters, and whether results were truncated.
4. Use the **evidence ledger** on the right: every query of the current request, earlier requests
   as folded reports, links back to each card, and a per-request **Export** of the full report
   (workflow, results, SQL, answer) as Markdown.

Chat history and agent context survive a page reload. Each answer shows the Kiro credits it used.
Details: [agent-ui/README.md](agent-ui/README.md).

**Alternative: Kiro CLI or IDE directly.** Install Kiro CLI (`curl -fsSL https://cli.kiro.dev/install | bash`)
and copy the MCP config into the workspace:

```bash
mkdir -p .kiro/settings
cp examples/kiro/mcp.local.example.json .kiro/settings/mcp.json   # rubin-log-insights → 127.0.0.1:18000
kiro-cli login
kiro-cli chat --agent engineer        # or team-lead, manager — run from the repository root
```

This uses the same agents but shows the evidence only as terminal text. Kiro IDE picks up
`.kiro/settings/mcp.json` the same way ([Kiro MCP guide](docs/kiro_mcp_integration.md)).

### 1.5 Stop, restart, recover

```bash
./scripts/jury-compose.sh ps                          # status
./scripts/jury-compose.sh --profile agent stop        # stop everything, keep data and login
./scripts/agent-ui-up.sh                              # start again (or jury-up.sh for MCP only)
```

The setup never deletes data. It refuses to continue on a half-loaded database rather than
guessing. Guarantees, recovery steps and the full test matrix are in
[docs/operations.md](docs/operations.md).

---

## 2. What we built

### 2.1 MCP server: five read-only tools

All tools are parameterised SELECTs against SereneDB. User values are always bound parameters,
never spliced into SQL. Every result contains the **executed SQL and its parameters**, the
**complete count** of matching records (independent of the returned limit), and a
**`truncated`** flag when rows were left out. Counts are **log records, not incidents**.

| Tool | Purpose | Key inputs | Returns |
|---|---|---|---|
| `search_logs` | Full-text evidence: find the actual log lines | `dataset` (`bgl`/`openstack`), `query`, `match_mode` (`any`, `all`, `phrase`, `exact`), `start_time`/`end_time`, `component`, `severity`, `limit` (≤ 50, default 20); OpenStack also `request_id`, `instance_id` | matching records (`record_id`, timestamp, component, severity, message; OpenStack adds request/instance/service/HTTP/latency/anomaly fields), `total_corpus_match_count`, `truncated`, SQL |
| `summarize_logs` | Aggregation over the **whole filtered corpus**: how often, where, who, when | `dataset`, `group_by` (`component`, `team`, `severity`, `event_id`, `time`), `interval` (`hour`/`day`/`week`/`month`), optional `query` + `match_mode` to count one specific pattern, time/component/severity filters, `sort_direction`, `limit` (≤ 100 groups) | groups with `record_count`, `total_matching_record_count`, scope, `truncated`, team **mapping coverage** for `group_by=team`, SQL |
| `get_nearby_events` | Chronological context around one record | `dataset`, `record_id` (from `search_logs`), `before`/`after` (≤ 50, default 10), `same_component` | anchor, events before/after with second offsets, counts, causation warning, SQL |
| `analyze_vm_spawn_performance` | OpenStack VM launch performance over the complete population | optional time range, `slow_threshold_seconds` or `baseline_multiplier` (default P95 × 1.25), `baseline_scope`, `interval` (hour/day), `top_limit`, `bucket_limit` | counts, mean/median/P95/P99/min/max, baseline, slow-event detection, slowest events, hourly/daily time series, warnings, SQL |
| `health_check` | Process readiness | — | status |

More detail: [search modes](docs/search_match_modes.md), [pattern aggregation](docs/pattern_aggregation.md),
[OpenStack tracing](docs/openstack_tracing_mcp.md), [VM spawn performance](docs/vm_spawn_performance.md).

### 2.2 Role agents: RUBIN v1.5 workflows

The three Kiro agents in `.kiro/agents/` share an evidence-calibrated core and differ by role.
They may only call the five rubin tools (no shell, file or web access).

**Shared core (all roles):**
- **Evidence integrity:** every number comes from a tool result in the session and is tagged E1,
  E2, … and labelled sample- or population-level. Observed facts, hypotheses and recommended
  checks are kept apart.
- **Recurrence and frequency gate:** "most common", "recurring", "first" or "all" are only
  claimed from complete, filtered aggregations, never from a handful of search results.
- **Causality gate:** timing, shared identifiers or FATAL labels alone never establish a root
  cause.
- Built-in knowledge of search accuracy, OpenStack request/VM tracing, VM spawn analytics and
  evidence-based prioritisation.

| Role | Focus | Typical tool flow | Answer shape |
|---|---|---|---|
| **Engineer** | Specific failures, affected nodes/instances/requests, event sequences | `search_logs` → pick real records → `get_nearby_events`; OpenStack pivots on real `request_id`/`instance_id`; spawn outliers via `analyze_vm_spawn_performance` | main finding, tagged evidence, plausible mechanism vs. uncertainty, 2–3 concrete diagnostic checks |
| **Team lead** | Recurrence of specific patterns, trends, owning teams, investigation order | `summarize_logs` with a pattern `query` and `group_by=time`/`component`/`team`; spawn trends via time buckets | pattern/trend finding, counts and scope, fictional ownership, coordinated next checks and ordering |
| **Manager** | Priorities and decisions under uncertainty | population-level `summarize_logs` and `analyze_vm_spawn_performance`, few raw lines | recommended priority, 2–3 evidence points, impact marked unverified unless established, one next action |

### 2.3 Agent UI

`agent-ui/` is a React + Fastify app that drives the official Kiro CLI over the Agent Client
Protocol. It provides:
- **Device-code sign-in**, the role dropdown and suggested questions per role.
- **Workflow timeline:** reasoning and tool calls in order, folding automatically after five calls.
- **Generic evidence cards** built from the shape of any tool result: tables, small-multiple
  time-series charts, timelines with gaps, stat groups, SQL with parameters, truncation and
  "not available" notes. New tools need no UI code.
- **Evidence ledger** with links into the workflow, and per-request Markdown export.
- **Persistent history:** chat history and Kiro sessions survive page reloads.

See [agent-ui/README.md](agent-ui/README.md).

---

## 3. Worked investigation

### 3.1 The fictional ownership model

The logs say *which component* emitted a line, not *which team* is responsible. To let the Team
lead and the Manager reason about teams, we **invented a small platform organisation** and mapped
every component that occurs in the corpus to one owning team:

- **The mapping:** `data/ownership.csv` (`dataset, component, team, area`), loaded into the
  `ownership` table by `sql/04_load_ownership.sql`. It has 22 assignments, 10 for BGL and 12
  for OpenStack.
- **How it's used:** `summarize_logs` with `group_by=team` LEFT JOINs the logs to this table on
  dataset + component. Unmapped components stay visible as `UNMAPPED` rather than disappearing.
  Each result reports **mapping coverage** (mapped vs. total records and components) and marks
  the mapping as fictional.
- **How the agents treat it:** they say "fictional owner" and attribute teams only through such
  verified `group_by=team` results. The Manager therefore gets "which team should act first" as
  real counts per team, without the data pretending to be an org chart.

| Team | Owns (component → area) |
|---|---|
| Compute Kernel | BGL `KERNEL`: compute-node kernel (memory/cache/TLB errors, interrupts) |
| HPC Applications | BGL `APP`: user jobs on compute nodes |
| Cluster Control | BGL `MMCS`, `BGLMASTER`, `CMCS`, `DISCOVERY`, `SERV_NET`: control system, job launch, discovery, service network |
| Hardware & Interconnect | BGL `HARDWARE`, `MONITOR`, `LINKCARD`: power, sensors, interconnect link cards |
| Cloud API | OpenStack `nova.osapi_compute.wsgi.server`, `nova.metadata.wsgi.server`, `…server_external_events`, `nova.api.openstack.wsgi`, `keystonemiddleware.auth_token` |
| Virtualization | OpenStack `nova.compute.manager`, `nova.virt.libvirt.driver`, `nova.virt.libvirt.imagecache`, `oslo_service.periodic_task` |
| Capacity & Scheduling | OpenStack `nova.compute.claims`, `nova.compute.resource_tracker`, `nova.scheduler.host_manager` |

Both corpora are fully covered: every component in the loaded data has an owner (100 % mapping
coverage in the verification run). The teams and areas are deliberately plausible, but they are
**not an authoritative organisational fact**.

### 3.2 Scenario

<!-- WORKED-INVESTIGATION: filled in from real agent runs through the RUBIN agent UI. -->

_To be completed from a real agent session: the questions per role, the tool calls, executed
queries and results, each role's answer and recommended action, and what the data does and does
not establish._

---

## 4. Architecture

```
                        ┌──────────────────── Docker Compose project "schubki-jury" ────────────────────┐
 Browser ──HTTP/WS──▶   │ agent-ui :18080   React + Fastify ──stdio ACP──▶ kiro-cli acp --agent <role>   │
 (judge)                │                                                     │ MCP (Streamable HTTP)    │
                        │                                                     ▼                          │
 Claude Code / Codex /  │ mcp-server :18000  server.py → tool handlers → parameterised QueryService      │
 Kiro CLI ──MCP HTTP──▶ │                                                     │ SQL (PostgreSQL protocol)│
                        │                                                     ▼                          │
                        │ serenedb :17890    bgl_logs(+_idx) · openstack_logs(+_idx) · ownership         │
                        └────────────────────────────────────────────────────────────────────────────────┘
```

| Service | What it is |
|---|---|
| `serenedb` | SereneDB 26.09.1.3 with the base corpus, full-text indexes and the ownership table. Data lives in the `schubki-jury_serenedb-data` volume. |
| `mcp-server` | Python MCP server (`server.py`, `log_insights/`). Read-only, non-root, read-only filesystem. Only fixed tables and parameterised queries. |
| `agent-ui` (profile `agent`) | Node 22 + Kiro CLI 2.32.0 (pinned and checksummed). Role agents are mounted read-only from `.kiro/agents`. Kiro may only use rubin tools; the Kiro login lives in the `kiro-home` volume. |
| `serene-ui` (profile `ui`), `psql` (profile `tools`) | Optional SQL browser and client for inspecting the database. |

**Data model:**

| MCP field / feature | SQL |
|---|---|
| BGL corpus / full-text search | `bgl_logs` / `bgl_logs_idx` |
| OpenStack corpus / full-text search | `openstack_logs` / `openstack_logs_idx` |
| timestamp / severity / component / message | `ts` (TIMESTAMP without zone) / `level` / `component` / `message` |
| BGL `record_id` | `line_no` |
| OpenStack `record_id` | source-file ordinal × 1,000,000,000 + `line_no` (1 = `openstack_abnormal`, 2 = `openstack_normal1`, 3 = `openstack_normal2`) |
| `event_id` | NULL: the raw logs have no EventId or template |
| team grouping | LEFT JOIN `ownership` on dataset + component; missing keys → `UNMAPPED` |

**Repository layout:**

| Path | Contents |
|---|---|
| `sql/` | Schema, loaders, ownership load and indexes (`01`–`05`) |
| `data/ownership.csv` | Fictional component → team mapping |
| `log_insights/`, `server.py` | MCP server: tools, query plans, SereneDB access |
| `.kiro/agents/` | RUBIN v1.5 role agents (`engineer`, `team-lead`, `manager`) and prompt sources |
| `agent-ui/` | Browser UI, ACP bridge, generic evidence cards, tests |
| `scripts/` | `jury-up.sh`, `agent-ui-up.sh`, `jury-compose.sh`, download, verification and benchmark scripts |
| `docs/` | Tool details, tracing and performance studies, [operations](docs/operations.md) |
| `tests/` | Python unit and integration tests |

---

## 5. Known limitations

- **Ownership is fictional.** Team results show where to look, not who is accountable.
- **Counts are log records, not incidents.** One failure can emit many lines; the agents say so.
- **No causation from logs alone.** Proximity in time and shared identifiers show sequence and
  association, not root cause. Answers label hypotheses.
- **No impact data.** The corpora contain no downtime, cost or customer information. The Manager
  agent marks operational impact as unverified instead of estimating it.
- **Data scope:** BGL covers 2005-06-03 to 2006-01-04; OpenStack covers about three days
  (2017-05-14 to 2017-05-17). BGL has no event IDs or templates (`event_id` is NULL). In
  OpenStack, `instance_id` is set mainly on compute records, and several fields (e.g.
  `latency_s`) are often empty. The UI lists such columns as "not available".
- **Spawn performance** measures the hypervisor's reported spawn duration, not full boot time or
  API latency. Bucket timestamps are completion times.
- **The agent UI needs a Kiro account** (free Builder ID) and internet access, and uses Kiro
  credits. It runs the official Kiro CLI over ACP and never calls Kiro's backend directly (Kiro's
  billing FAQ permits "ACP compatible IDEs" and forbids third-party harnesses that bypass Kiro's
  own interfaces). It has no authentication of its own and is bound to `127.0.0.1`; don't expose it.
- **Agent answers are model output.** The SQL and counts in the evidence cards are the ground
  truth to check them against.
- **Optional extensions** (on-demand indexing, larger remote datasets, row-level security) are
  not part of this submission.
