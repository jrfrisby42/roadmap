"""
Tests for the Litestream backup-config generator (server.sync_litestream_config).

These exercise the pure config generation + env-gating only. They never call S3 or
systemctl: `do_reload=False` skips the reload command entirely, and no LITESTREAM_*
env is set unless the test sets it (so the feature is a no-op elsewhere).
"""
import os
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
