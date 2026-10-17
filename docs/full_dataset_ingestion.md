# Optional Python full-dataset ingestion v1 (feature-branch baseline)

This document records the feature branch's observed archive contents, parsing
contract, and historical bounded dry run. Those results have not been rerun in
the integration worktree. The integrated default now uses main's `sql/` loaders
and `bgl_logs`/`openstack_logs`; see the [README](../README.md). This Python loader
is retained as an explicit alternate pipeline with separate versioned tables.
It does not initialize the default SQL backend or change MCP routing. All loading
commands below require separate approval of their database target.

## Immutable sources

The machine-readable source of truth is
`manifests/full_dataset_sources_v1.json`.

| Dataset | Official archive | Archive bytes | SHA-256 |
| --- | --- | ---: | --- |
| BGL | `BGL.zip` | 57,489,019 | `d67fd82a711aea0157a9b83175892c6ee60e384a2ddf5bc51f39118453816da8` |
| OpenStack | `OpenStack.tar.gz` | 5,394,691 | `87c98c5ed03262e05cdb7a6f3717033df76d88fda0f7d2db23bd9fa4200f1879` |

Both archives were integrity-tested before use. Their member names are flat, with
no absolute paths, `..` traversal, or links. The loader streams named members
directly and does not extract them to disk.

## Actual contents

BGL contains `BGL.log` (743,185,031 bytes) and `README.md`. `BGL.log` has
4,747,963 physical raw-log records. There is no structured CSV, EventId column,
or event-template file.

OpenStack contains `openstack_abnormal.log` (18,434 records),
`openstack_normal1.log` (52,312), `openstack_normal2.log` (137,074), and
`anomaly_labels.txt`. The label file names four VM instance UUIDs associated with
the abnormal log. There is no structured CSV, EventId column, or event-template
file.

## Parsing contract

BGL begins with label, epoch, date, node, and precise timestamp. The remainder is
split by the observed `RAS|NULL`, component, and severity boundary. Node-repeat is
nullable because some valid records omit it, while a few records contain irregular
free text in that position. The message is everything after severity. The label is
preserved verbatim and `is_anomaly` is true when it is not `-`.

OpenStack begins with source-log name, date, precise time, PID, severity, and
component. A leading bracketed request context is extracted when present. A
`req-...` identifier is extracted only from that context, and an instance UUID is
extracted only from an explicit `[instance: ...]` marker in the message. Traceback
records without bracketed context retain the entire remainder as their message.
The abnormal/normal classification comes from the archive member name. When an
explicit instance marker is present, a nullable boolean records whether its UUID is
one of the four IDs from `anomaly_labels.txt`; records without an instance marker
remain unknown rather than being assigned a label.

Both tables retain the complete `raw_line`, source member and line number, archive
checksum, and exact extracted message. Stable IDs are the BGL physical line number
and, for OpenStack, `file_ordinal * 1,000,000,000 + physical_line_number` in the
documented archive-member order. EventId and EventTemplate are stored as SQL NULL;
they are never invented.

## Observed ambiguities

The complete streaming inspection rejected zero records. It found 34,470 BGL and
8 OpenStack records with an empty message. OpenStack has 184 traceback-style
records without request context. These are retained as valid records. BGL has both
`RAS` and `NULL` log-type markers, nullable node-repeat values, and several
irregular node-repeat strings; locating the explicit type/component/severity
boundary prevents field shifting.

## Loading and staging

`python -m scripts.load_full_staging` verifies archive size/checksum, streams a
deterministic representative subset through `COPY ... FROM STDIN`, creates the
inverted indexes after COPY, and runs `VACUUM (REFRESH_TABLE)` before search probes.
The bounded targets are:

- `loghub_bgl_full_v1_staging`
- `loghub_openstack_full_v1_staging`

The production targets `loghub_bgl_full_v1` and `loghub_openstack_full_v1` are
allowlisted in code but are not loaded by the bounded command. The sample tables
and indexes are separate and remain unchanged.

## Bounded dry-run results

The finalized idempotent run loaded 4,757 BGL records and 2,932 OpenStack records,
with zero rejects and zero parsing errors. Selection uses a fixed stride plus the
first occurrence of every component, so the subsets cover all 10 BGL components
and all 12 OpenStack components found by the complete scan.

| Dataset | COPY + validation | Index create + refresh | Full-text probe |
| --- | ---: | ---: | --- |
| BGL | 8.418 s | 0.031 s | `cache`: 126 matches |
| OpenStack | 0.356 s | 0.034 s | `instance`: 982 matches |

Peak process RSS was 43,712,512 bytes (about 41.7 MiB). The staging tables have
unique stable IDs and valid timestamps/components/severities. Every staged row has
NULL EventId and EventTemplate. The OpenStack subset contains 6 records explicitly
linked to a UUID in the anomaly-label file, 822 explicitly linked to other instance
UUIDs, and 2,104 with no instance marker.

The sample tables remain at 2,000 rows each. MCP search, component/team summaries,
and nearby-event retrieval all passed against the staging table/index injection;
the feature-branch server selected sample tables. In the integrated project, only
`MCP_DATASET_BACKEND=sample` selects those samples; the default is main's SQL schema.

The full scan found five BGL components absent from fictional ownership
(`BGLMASTER`, `CMCS`, `LINKCARD`, `MONITOR`, `SERV_NET`) and two OpenStack components
(`keystonemiddleware.auth_token`, `oslo_service.periodic_task`). The staging team
summary preserves them as `UNMAPPED`.

## Provisional full-load requirements

The complete load is 4,955,783 rows and 804,627,113 bytes of expanded raw log text,
before parsed columns, table metadata, the primary-key index, and inverted indexes.
Python memory should remain below 64 MiB based on the bounded run; allocate at least
256 MiB to the loader process. Reserve 8–12 GiB of free SereneDB storage for table,
index, transaction, and rebuild headroom. This disk estimate is deliberately
conservative because no complete table or index has been created yet.

Allow roughly 15–45 minutes for the first full COPY plus both index builds on the
current local machine. This is an estimate, not a benchmark: the bounded run proves
streaming behavior and SQL compatibility, while full index cost is non-linear.

Before a complete import:

1. Review the seven newly unmapped components and decide whether fictional mapping
   v2 should cover them.
2. Confirm at least 12 GiB of free disk and take any desired SereneDB snapshot.
3. Review the NULL event-template contract and the BGL/OpenStack ambiguity counts.
4. Explicitly approve the complete load; then run
   `python -m scripts.load_full_data --dataset both --confirm-full-load`.
5. Validate full row counts, distinct IDs, index visibility, ownership coverage,
   and MCP results before changing any MCP dataset routing.
