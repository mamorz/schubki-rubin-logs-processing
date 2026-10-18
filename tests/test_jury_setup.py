"""Safety regressions use catalog doubles and a fake Docker CLI, never a database."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from scripts import jury_setup as jury

ROOT = Path(__file__).resolve().parents[1]


class Catalog:
    def __init__(self, *, ready=False):
        self.schemas = ["public", "pg_catalog", "information_schema", "sdb_docs"]
        self.relations = [("sdb_docs", name, kind) for name, kind in jury.BUNDLED_RELATIONS]
        self.dictionaries = [("sdb_docs", "tokenizer")]
        self.functions = [("sdb_docs", name) for name in ("read", "reference", "search", "sections")]
        self.counts = dict(jury.TABLE_COUNTS)
        self.sources = dict(jury.SOURCE_COUNTS)
        self.executed = []
        self.rows = []
        if ready:
            self.relations += [("public", name, kind) for name, kind in jury.EXPECTED_RELATIONS]
            self.dictionaries += [("public", "log_dict")]

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, **kwargs):
        self.executed.append(sql)
        assert sql.startswith("SELECT"), "State inspection attempted a write"
        if "FROM pg_catalog.pg_namespace" in sql:
            self.rows = [dict(nspname=s) for s in self.schemas]
        elif "FROM pg_catalog.pg_class" in sql:
            self.rows = [dict(nspname=s, relname=n, relkind=k) for s, n, k in self.relations]
        elif "FROM pg_catalog.pg_ts_dict" in sql:
            self.rows = [dict(nspname=s, dictname=n) for s, n in self.dictionaries]
        elif "FROM pg_catalog.pg_proc" in sql:
            self.rows = [dict(nspname=s, proname=n) for s, n in self.functions]
        elif "GROUP BY source_file" in sql:
            self.rows = [dict(source_file=s, n=n) for s, n in self.sources.items()]
        else:
            self.rows = [dict(n=self.counts[sql.split("public.")[1]])]

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0]


def test_empty_check_is_select_only():
    assert jury.setup(Catalog()) == "empty"


def test_existing_complete_database_never_loads_sql(monkeypatch):
    checked = []
    monkeypatch.setattr(jury, "check_sql_backend", lambda c: checked.append(c))
    monkeypatch.setattr(jury, "load_sql", lambda: pytest.fail("Existing database reached loader"))
    db = Catalog(ready=True)
    assert jury.setup(db, initialize_empty=True) == "ready"
    assert checked == [db]


@pytest.mark.parametrize("damage", ["table", "dictionary", "index", "count", "sources",
                                    "schema", "unknown_table", "function", "bundled_extra"])
def test_partial_or_unknown_state_never_reaches_loader(monkeypatch, damage):
    monkeypatch.setattr(jury, "load_sql", lambda: pytest.fail("Unsafe database reached loader"))
    db = Catalog(ready=True)
    if damage == "table":
        db.relations.remove(("public", "bgl_logs", "r"))
    elif damage == "dictionary":
        db.dictionaries.remove(("public", "log_dict"))
    elif damage == "index":
        db.relations.remove(("public", "openstack_logs_idx", "i"))
    elif damage == "count":
        db.counts["bgl_logs"] -= 1
    elif damage == "sources":
        db.sources["unknown"] = db.sources.pop("openstack_normal1")
    elif damage == "schema":
        db.schemas.append("other_project")
    elif damage == "unknown_table":
        db.relations.append(("public", "unrelated", "r"))
    elif damage == "function":
        db.functions.append(("public", "custom"))
    else:
        db.relations.append(("sdb_docs", "unrelated", "r"))
    with pytest.raises(jury.UnsafeState):
        jury.setup(db, initialize_empty=True)


def test_catalog_error_and_index_failure_do_not_initialize(monkeypatch):
    def fail(*args):
        raise RuntimeError("query failed")
    monkeypatch.setattr(jury, "load_sql", lambda: pytest.fail("Failure reached loader"))
    db = Catalog()
    db.execute = fail
    with pytest.raises(RuntimeError, match="query failed"):
        jury.setup(db, initialize_empty=True)
    monkeypatch.setattr(jury, "check_sql_backend", fail)
    with pytest.raises(RuntimeError, match="query failed"):
        jury.setup(Catalog(ready=True), initialize_empty=True)


def test_create_only_sql_and_destructive_regression(monkeypatch, tmp_path):
    scripts = jury.load_sql()
    assert [name for name, _ in scripts] == list(jury.SQL_FILES)
    for name, sql in scripts:
        (tmp_path / f"{name}.sql").write_text(sql)
    monkeypatch.setattr(jury, "SQL_DIR", tmp_path)
    with (tmp_path / "01_schema.sql").open("a") as file:
        file.write("\nDROP TABLE bgl_logs;\n")
    with pytest.raises(jury.UnsafeState, match="Destructive"):
        jury.load_sql()


def test_initializer_order_and_stop_on_first_failure(monkeypatch):
    states = iter(["empty", "empty", "ready"])
    monkeypatch.setattr(jury, "inspect_state", lambda c: next(states))

    class Recorder(Catalog):
        fail = False

        def execute(self, sql, **kwargs):
            self.executed.append(sql)
            if self.fail:
                raise RuntimeError("Concurrent CREATE or SQL failure")

        def nextset(self):
            return None

    db = Recorder()
    assert jury.setup(db, initialize_empty=True) == "ready"
    assert db.executed == [sql for _, sql in jury.load_sql()]
    # This records strings only; no SQL is executed by these tests.
    states = iter(["empty", "empty"])
    db = Recorder()
    db.fail = True
    with pytest.raises(RuntimeError, match="Concurrent CREATE"):
        jury.setup(db, initialize_empty=True)
    assert len(db.executed) == 1


def test_state_change_before_create_refuses_all_writes(monkeypatch):
    states = iter(["empty", "ready"])
    monkeypatch.setattr(jury, "inspect_state", lambda c: next(states))
    db = Catalog()
    with pytest.raises(jury.UnsafeState, match="changed"):
        jury.setup(db, initialize_empty=True)
    assert not db.executed


def test_connection_ignores_external_environment(monkeypatch):
    monkeypatch.setenv("SERENEDB_DSN", "host=external")
    monkeypatch.setenv("PGHOST", "external")
    calls = []
    db = Catalog()
    monkeypatch.setattr(jury.psycopg, "connect", lambda dsn, **kw: calls.append((dsn, kw)) or db)
    db.execute = lambda sql: None
    assert jury.wait_for_database() is db
    assert calls[0][0] == jury.JURY_DSN
    assert "host=serenedb " in calls[0][0]


@pytest.mark.parametrize("failure", ["", "config", "download", "setup", "health", "verify"])
def test_shell_workflow_isolated_and_stops_on_failure(tmp_path, failure):
    # Run a copied entrypoint against a recording CLI, never real Docker.
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("jury-up.sh", "jury-compose.sh"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    (scripts / "download.sh").write_text(
        '#!/bin/sh\necho download >> "$CALL_LOG"\n[ "$FAIL_AT" != download ]\n'
    )
    (scripts / "download.sh").chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as f:
    f.write(json.dumps({'args': args, 'dsn': os.getenv('SERENEDB_DSN'),
                        'docker_dsn': os.getenv('SERENEDB_DOCKER_DSN'),
                        'port': os.getenv('MCP_PUBLISHED_PORT')}) + '\\n')
failure = os.environ['FAIL_AT']
if ((failure == 'config' and 'config' in args) or
    (failure == 'setup' and 'scripts.jury_setup' in args) or
    (failure == 'health' and '--wait' in args) or
    (failure == 'verify' and 'scripts.verify_mcp_http' in args)):
    sys.exit(1)
''')
    docker.chmod(0o755)
    log = tmp_path / "calls"
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", CALL_LOG=str(log),
               FAIL_AT=failure, SERENEDB_DSN="host=external", SERENEDB_DOCKER_DSN="host=external",
               COMPOSE_PROJECT_NAME="wrong", COMPOSE_FILE="wrong.yml", MCP_PUBLISHED_PORT="9999")
    result = subprocess.run([str(scripts / "jury-up.sh")], env=env, capture_output=True, text=True)
    assert (result.returncode == 0) == (not failure), result.stderr
    lines = log.read_text().splitlines()
    calls = [json.loads(line) for line in lines if line != "download"]
    for call in calls:
        args = call["args"]
        if args[0] == "compose":
            assert args[args.index("--project-name") + 1] == "schubki-jury"
            assert args[args.index("--env-file") + 1] == "/dev/null"
            assert args[args.index("-f") + 1] == str(tmp_path / "docker-compose.yml")
            assert call["dsn"] is None and call["docker_dsn"] is None
            assert call["port"] == "18000"
        assert not set(args) & {"down", "prune", "-v"}
    command_text = log.read_text()
    if failure in {"config", "download"}:
        assert '"up"' not in command_text
    if failure in {"config", "download", "setup"}:
        assert '"--wait"' not in command_text
    if failure:
        assert "Jury setup verified" not in result.stdout
    else:
        assert "scripts.verify_mcp_http" in command_text
    assert not (tmp_path / "data/.jury-up.lock").exists()


def test_download_checksums_match_source_manifest():
    manifest = json.loads((ROOT / "manifests/full_dataset_sources_v1.json").read_text())
    script = (ROOT / "scripts/download.sh").read_text()
    for source in manifest["sources"].values():
        assert source["archive_sha256"] in script
        for member in source["members"]:
            if member["role"] != "documentation":
                assert member["sha256"] in script


@pytest.fixture
def download_fixture(tmp_path):
    import hashlib
    import io
    import tarfile
    import zipfile

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    archives = tmp_path / "archives"
    archives.mkdir()
    manifest = json.loads((ROOT / "manifests/full_dataset_sources_v1.json").read_text())
    script = (ROOT / "scripts/download.sh").read_text()
    members = {}
    for dataset, source in manifest["sources"].items():
        archive_path = archives / Path(source["archive_path"]).name
        content = {}
        for member in source["members"]:
            if member["role"] == "documentation":
                continue
            data = (member["name"] + " fixture\n").encode()
            content[member["name"]] = data
            path = member["name"] if dataset == "bgl" else "openstack/" + member["name"]
            members[path] = data
            script = script.replace(member["sha256"], hashlib.sha256(data).hexdigest())
        if dataset == "bgl":
            with zipfile.ZipFile(archive_path, "w") as archive:
                for name, data in content.items():
                    archive.writestr(name, data)
        else:
            with tarfile.open(archive_path, "w:gz") as archive:
                for name, data in content.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
        script = script.replace(source["archive_sha256"], hashlib.sha256(archive_path.read_bytes()).hexdigest())
    downloader = scripts / "download.sh"
    downloader.write_text(script)
    downloader.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text('''#!/usr/bin/env python3
import os, pathlib, shutil, sys
args = sys.argv[1:]
target = pathlib.Path(args[args.index('--output') + 1])
with open(os.environ['CALL_LOG'], 'a') as f:
    f.write(args[-1] + '\\n')
if os.getenv('FAIL_DOWNLOAD'):
    target.write_bytes(b'partial response')
    sys.exit(18)
name = args[-1].split('/')[-1].split('?')[0]
shutil.copyfile(pathlib.Path(os.environ['ARCHIVES']) / name, target)
''')
    curl.chmod(0o755)
    # Downloading runs on the host, not in the slim MCP runtime. Supply only
    # the unzip -p operation used by this fixture, extracting real ZIP bytes
    # with the standard library instead of requiring a production OS package.
    unzip = bin_dir / "unzip"
    unzip.write_text('''#!/usr/bin/env python3
import sys, zipfile
if len(sys.argv) != 4 or sys.argv[1] != '-p':
    sys.exit('fixture unzip supports only: unzip -p ARCHIVE MEMBER')
with zipfile.ZipFile(sys.argv[2]) as archive:
    sys.stdout.buffer.write(archive.read(sys.argv[3]))
''')
    unzip.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", ARCHIVES=str(archives),
               CALL_LOG=str(tmp_path / "curl-calls"))
    return downloader, env, tmp_path / "data/raw", members


def test_download_fresh_repeat_and_missing_member(download_fixture):
    downloader, env, raw, members = download_fixture
    for attempt in range(3):
        if attempt == 2:
            # Existing OpenStack normal2 must not mask an absent anomaly file.
            (raw / "openstack/anomaly_labels.txt").unlink()
        result = subprocess.run([str(downloader)], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert {name: (raw / name).read_bytes() for name in members} == members
    assert len(Path(env["CALL_LOG"]).read_text().splitlines()) == 2


def test_download_interruption_never_publishes_archive_or_member(download_fixture):
    downloader, env, raw, _ = download_fixture
    result = subprocess.run([str(downloader)], env=dict(env, FAIL_DOWNLOAD="1"), capture_output=True)
    assert result.returncode != 0
    assert Path(env["CALL_LOG"]).exists(), "Fixture must reach curl, not fail a prerequisite check"
    assert not (raw / "BGL.zip").exists()
    assert not (raw / "BGL.log").exists()
    assert not list(raw.glob(".download.*"))


def test_corrupt_existing_source_is_preserved(download_fixture):
    downloader, env, raw, _ = download_fixture
    raw.mkdir(parents=True)
    (raw / "BGL.log").write_bytes(b"existing corrupt file")
    result = subprocess.run([str(downloader)], env=env, capture_output=True, text=True)
    assert result.returncode != 0 and "Checksum mismatch" in result.stderr
    assert (raw / "BGL.log").read_bytes() == b"existing corrupt file"
    assert not Path(env["CALL_LOG"]).exists()


def test_download_missing_unzip_stops_before_fetch_or_publish(download_fixture, tmp_path):
    downloader, env, raw, _ = download_fixture
    # Isolate PATH so a host-installed unzip cannot mask the missing dependency.
    # Only Bash, dirname and curl are needed before the script checks for unzip.
    bin_dir = tmp_path / "missing-unzip-bin"
    bin_dir.mkdir()
    for tool in ("bash", "dirname", "curl"):
        (bin_dir / tool).symlink_to(shutil.which(tool, path=env["PATH"]))
    result = subprocess.run([str(downloader)], env=dict(env, PATH=str(bin_dir)), capture_output=True, text=True)
    assert result.returncode != 0
    assert "Install unzip first." in result.stderr
    assert not raw.exists()
    assert not Path(env["CALL_LOG"]).exists()
