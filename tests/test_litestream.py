"""
Tests for the Litestream backup-config generator (server.sync_litestream_config).

These exercise the pure config generation + env-gating only. They never call S3 or
systemctl: `do_reload=False` skips the reload command entirely, and no LITESTREAM_*
env is set unless the test sets it (so the feature is a no-op elsewhere).
"""
import os
import sqlite3

import server


def _mk_team(root, name):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "roadmap.db"), "w").close()


def test_noop_when_unconfigured(tmp_path, monkeypatch):
    # No LITESTREAM_FLOW_CONFIG -> no-op (dev/local/tests are unaffected).
    monkeypatch.delenv("LITESTREAM_FLOW_CONFIG", raising=False)
    assert server.sync_litestream_config(tenants_dir=str(tmp_path), do_reload=False) is False


def test_skips_without_bucket(tmp_path, monkeypatch):
    cfg = tmp_path / "ls.yml"
    monkeypatch.setenv("LITESTREAM_FLOW_CONFIG", str(cfg))
    monkeypatch.delenv("LITESTREAM_S3_BUCKET", raising=False)
    # Config path set but no bucket -> nothing usable to write; skip, don't create a file.
    assert server.sync_litestream_config(tenants_dir=str(tmp_path), do_reload=False) is False
    assert not cfg.exists()


def test_generates_config_one_entry_per_team(tmp_path, monkeypatch):
    tenants = tmp_path / "tenants"
    tenants.mkdir()
    _mk_team(str(tenants), "acme")
    _mk_team(str(tenants), "globex")
    # a stray dir without a roadmap.db must NOT produce an entry
    (tenants / "empty").mkdir()

    cfg = tmp_path / "litestream-flow.yml"
    monkeypatch.setenv("LITESTREAM_FLOW_CONFIG", str(cfg))
    monkeypatch.setenv("LITESTREAM_S3_BUCKET", "frazil-flow-backups")
    monkeypatch.setenv("LITESTREAM_S3_PREFIX", "flow")
    monkeypatch.setenv("AWS_REGION", "us-west-2")

    ok = server.sync_litestream_config(tenants_dir=str(tenants), do_reload=False)
    assert ok is True

    text = cfg.read_text()
    assert text.count("- path:") == 2                    # exactly the two DBs
    assert os.path.join(str(tenants), "acme", "roadmap.db") in text
    assert os.path.join(str(tenants), "globex", "roadmap.db") in text
    assert "bucket: frazil-flow-backups" in text
    assert "path: flow/acme" in text
    assert "path: flow/globex" in text
    assert "region: us-west-2" in text
    assert "empty" not in text                            # no DB -> not enumerated
    # Instance-role auth only: no credentials ever written into the config file.
    assert "access-key" not in text.lower()
    assert "secret" not in text.lower()


def test_prefix_defaults_to_flow(tmp_path, monkeypatch):
    tenants = tmp_path / "tenants"
    tenants.mkdir()
    _mk_team(str(tenants), "acme")
    cfg = tmp_path / "ls.yml"
    monkeypatch.setenv("LITESTREAM_FLOW_CONFIG", str(cfg))
    monkeypatch.setenv("LITESTREAM_S3_BUCKET", "b")
    monkeypatch.delenv("LITESTREAM_S3_PREFIX", raising=False)
    assert server.sync_litestream_config(tenants_dir=str(tenants), do_reload=False) is True
    assert "path: flow/acme" in cfg.read_text()


def test_sse_kms_emitted_only_when_configured(tmp_path, monkeypatch):
    """The KMS-enforced backup bucket denies un-encrypted PUTs, so the replica must carry sse +
    sse-kms-key-id when configured - and nothing when not (backward-compatible)."""
    tenants = tmp_path / "tenants"; tenants.mkdir()
    _mk_team(str(tenants), "acme")
    cfg = tmp_path / "ls.yml"
    monkeypatch.setenv("LITESTREAM_FLOW_CONFIG", str(cfg))
    monkeypatch.setenv("LITESTREAM_S3_BUCKET", "b")

    # Unset -> no encryption directives (unchanged behaviour).
    monkeypatch.delenv("LITESTREAM_S3_SSE", raising=False)
    monkeypatch.delenv("LITESTREAM_S3_SSE_KMS_KEY_ID", raising=False)
    assert server.sync_litestream_config(tenants_dir=str(tenants), do_reload=False) is True
    t0 = cfg.read_text()
    assert "sse:" not in t0 and "sse-kms-key-id:" not in t0

    # Set -> both directives appear per replica; still no credentials in the file.
    monkeypatch.setenv("LITESTREAM_S3_SSE", "aws:kms")
    monkeypatch.setenv("LITESTREAM_S3_SSE_KMS_KEY_ID", "arn:aws:kms:us-west-2:1:key/abc")
    assert server.sync_litestream_config(tenants_dir=str(tenants), do_reload=False) is True
    t1 = cfg.read_text()
    assert "sse: aws:kms" in t1
    assert "sse-kms-key-id: arn:aws:kms:us-west-2:1:key/abc" in t1
    assert "access-key" not in t1.lower()


def test_metrics_addr_emitted_only_when_configured(tmp_path, monkeypatch):
    """SYS-STATUS-1 (A1): a top-level `addr:` (the Litestream Prometheus endpoint the status page
    scrapes for freshness) appears iff LITESTREAM_METRICS_ADDR is set, and NEVER collides with dbs."""
    tenants = tmp_path / "tenants"; tenants.mkdir()
    _mk_team(str(tenants), "acme")
    cfg = tmp_path / "ls.yml"
    monkeypatch.setenv("LITESTREAM_FLOW_CONFIG", str(cfg))
    monkeypatch.setenv("LITESTREAM_S3_BUCKET", "b")

    # Unset -> no addr line (unchanged behaviour).
    monkeypatch.delenv("LITESTREAM_METRICS_ADDR", raising=False)
    assert server.sync_litestream_config(tenants_dir=str(tenants), do_reload=False) is True
    assert "addr:" not in cfg.read_text()

    # Set -> a single top-level `addr:` line, above `dbs:`.
    monkeypatch.setenv("LITESTREAM_METRICS_ADDR", ":9091")
    assert server.sync_litestream_config(tenants_dir=str(tenants), do_reload=False) is True
    t = cfg.read_text()
    assert "addr: :9091" in t
    assert t.index("addr: :9091") < t.index("dbs:")     # top-level, sibling of dbs and before it
    assert t.count("addr:") == 1


def test_new_team_db_is_enumerable_before_first_use(tmp_path):
    """NEWTEAM-BACKUP-1: --new-team now creates a valid WAL roadmap.db up front (Option B), so the
    Litestream generator enumerates the team IMMEDIATELY, not only after its first HTTP request
    created the DB lazily. Mirror the fix's exact file creation and assert the generator includes
    it. (The 2026-08-31 finance gap was this file NOT existing when the generator globbed.)"""
    tenants = tmp_path / "tenants"; tenants.mkdir()
    tdir = tenants / "newco"; tdir.mkdir()
    dbp = tdir / "roadmap.db"
    con = sqlite3.connect(str(dbp)); con.execute("PRAGMA journal_mode=WAL"); con.close()
    yaml, n = server._litestream_flow_yaml(str(tenants), "bucket", "flow", "us-west-2")
    assert n == 1
    assert str(dbp) in yaml


def test_init_team_db_accepts_a_precreated_wal_file(team):
    """NEWTEAM-BACKUP-1 assumption 1.3.1: schema init (all CREATE TABLE IF NOT EXISTS) works cleanly
    against a roadmap.db that --new-team pre-created empty. Pre-create the WAL file, then init and
    confirm the schema landed - so the Option B ordering (empty DB first, schema on first use) holds."""
    slug = team + "pre"
    d = os.path.join(server.TENANTS_DIR, slug); os.makedirs(d, exist_ok=True)
    dbp = os.path.join(d, "roadmap.db")
    con = sqlite3.connect(dbp); con.execute("PRAGMA journal_mode=WAL"); con.close()
    server.init_team_db(slug)                    # must not raise against the existing empty file
    with server.db(slug) as c:
        tbls = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        mode = c.execute("PRAGMA journal_mode").fetchone()[0]
    assert "projects" in tbls and "config" in tbls   # schema landed onto the pre-created file
    assert mode.lower() == "wal"                      # WAL mode preserved for the replica
