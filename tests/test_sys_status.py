"""SYS-STATUS-1 - admin System-status endpoint (backup coverage first).

GET /api/system/status is admin-role AND operator-allowlist gated (Addendum B): the token's
username must be in SYS_STATUS_USERS, fail-closed (empty => nobody). It returns box observability
with NO secrets, every probe independently guarded (a single failure degrades that field, never
500s), the Litestream coverage row goes RED when a team DB is absent from the config, and the
schema-drift row flags a column present on one team DB but missing on another.
"""
import os
import sqlite3

import pytest

import server


def _hdr(team, user, role="admin"):
    return {"Authorization": f"Bearer {server.create_token(team, user, role)}", "X-Team": team}


@pytest.fixture
def allow_admin(monkeypatch):
    """Put username 'admin' on the operator allowlist for the duration of a test."""
    monkeypatch.setattr(server, "SYS_STATUS_USERS", frozenset({"admin"}))


# ── The gate (Addendum B) ─────────────────────────────────────────────────────

def test_listed_admin_gets_200(client, team, allow_admin):
    r = client.get("/api/system/status", headers=_hdr(team, "admin"))
    assert r.status_code == 200
    assert r.json()["version"] == server.APP_VERSION


def test_unlisted_admin_gets_403(client, team, monkeypatch):
    # An admin NOT on the allowlist is refused - the admin role alone is insufficient.
    monkeypatch.setattr(server, "SYS_STATUS_USERS", frozenset({"someone.else"}))
    r = client.get("/api/system/status", headers=_hdr(team, "admin"))
    assert r.status_code == 403


def test_removing_the_check_would_admit_that_admin(client, team, monkeypatch):
    # The falsifiable other half: the SAME admin that 403s above gets 200 once listed - proving the
    # allowlist (not the role) is what refused them.
    monkeypatch.setattr(server, "SYS_STATUS_USERS", frozenset({"admin"}))
    r = client.get("/api/system/status", headers=_hdr(team, "admin"))
    assert r.status_code == 200


def test_fail_closed_empty_allowlist(client, team, monkeypatch):
    # Empty/unset variable => NOBODY, even a user who would otherwise be listed.
    monkeypatch.setattr(server, "SYS_STATUS_USERS", frozenset())
    r = client.get("/api/system/status", headers=_hdr(team, "admin"))
    assert r.status_code == 403


def test_non_admin_role_refused(client, team, allow_admin):
    # require_role("admin") still gates first: an editor whose name is even on the list is refused.
    r = client.get("/api/system/status", headers=_hdr(team, "admin", role="editor"))
    assert r.status_code == 403


# ── No secrets in the response ────────────────────────────────────────────────

def test_no_secrets_in_response(client, team, allow_admin):
    r = client.get("/api/system/status", headers=_hdr(team, "admin"))
    assert r.status_code == 200
    body = r.text
    # The pinned test token secret (conftest) must never appear; nor any password/key field.
    assert "test-secret-do-not-use-in-prod" not in body
    d = r.json()
    for k in ("version", "uptimeSeconds", "teams", "coverage", "freshness",
              "services", "schema", "disk", "dataMount", "awsBackup", "gatheredAt"):
        assert k in d
    # AWS Backup is never a green check - it is explicitly not observable here.
    assert d["awsBackup"]["status"] == "not_observable"


# ── Coverage RED on drift (the headline) ──────────────────────────────────────

def test_coverage_red_when_team_absent_from_config(client, team, allow_admin, monkeypatch, tmp_path):
    # Two synthetic team DBs; the config references only teamA - teamB is the 2026-08-21 drift.
    teamA = {"team": "aaa", "path": str(tmp_path / "aaa" / "roadmap.db"), "sizeBytes": 1, "mtime": None}
    teamB = {"team": "bbb", "path": str(tmp_path / "bbb" / "roadmap.db"), "sizeBytes": 1, "mtime": None}
    monkeypatch.setattr(server, "_sys_teams_on_disk", lambda: [teamA, teamB])
    cfg = tmp_path / "litestream-flow.yml"
    cfg.write_text("dbs:\n  - path: " + teamA["path"] + "\n    replicas: []\n")
    monkeypatch.setenv("LITESTREAM_FLOW_CONFIG", str(cfg))
    r = client.get("/api/system/status", headers=_hdr(team, "admin"))
    assert r.status_code == 200
    cov = r.json()["coverage"]
    assert cov["status"] == "configured"
    assert cov["byTeam"]["aaa"] is True          # covered -> green
    assert cov["byTeam"]["bbb"] is False         # absent from config -> RED


def test_coverage_not_configured_is_neutral(client, team, allow_admin, monkeypatch):
    # No LITESTREAM_FLOW_CONFIG (e.g. a dev box) reads as neutral, never red.
    monkeypatch.delenv("LITESTREAM_FLOW_CONFIG", raising=False)
    r = client.get("/api/system/status", headers=_hdr(team, "admin"))
    assert r.json()["coverage"]["status"] == "not_configured"


# ── Schema drift synthetic RED (Addendum A4) ──────────────────────────────────

def test_schema_drift_flags_missing_column(client, team, allow_admin, monkeypatch):
    # teamA gains a column teamB lacks -> the fleet union has it -> teamB is flagged, named.
    a = f"{team}_schemaA"
    b = f"{team}_schemaB"
    os.makedirs(os.path.join(server.TENANTS_DIR, a), exist_ok=True)
    os.makedirs(os.path.join(server.TENANTS_DIR, b), exist_ok=True)
    server.init_team_db(a)
    server.init_team_db(b)
    with server.db(a) as c:
        c.execute("ALTER TABLE todos ADD COLUMN zzz_probe TEXT")
    pathA = os.path.join(server.TENANTS_DIR, a, "roadmap.db")
    pathB = os.path.join(server.TENANTS_DIR, b, "roadmap.db")
    monkeypatch.setattr(server, "_sys_teams_on_disk", lambda: [
        {"team": a, "path": pathA, "sizeBytes": 1, "mtime": None},
        {"team": b, "path": pathB, "sizeBytes": 1, "mtime": None},
    ])
    r = client.get("/api/system/status", headers=_hdr(team, "admin"))
    assert r.status_code == 200
    sc = r.json()["schema"]
    assert sc["consistent"] is False
    hit = [x for x in sc["drift"] if x["team"] == b and x["table"] == "todos"]
    assert hit and "zzz_probe" in hit[0]["missing"]


def test_schema_ignores_litestream_internal_tables(client, team, allow_admin, monkeypatch):
    # Live prod false-positive: Litestream creates _litestream_seq/_litestream_lock only in a DB it
    # has replicated, so a not-yet-replicated team "lacks" them - that is a BACKUP signal (coverage
    # row), NOT app-schema drift. The schema check must skip them and stay consistent.
    a = f"{team}_lsA"
    b = f"{team}_lsB"
    os.makedirs(os.path.join(server.TENANTS_DIR, a), exist_ok=True)
    os.makedirs(os.path.join(server.TENANTS_DIR, b), exist_ok=True)
    server.init_team_db(a)
    server.init_team_db(b)
    with server.db(a) as c:   # teamA looks "replicated"; teamB does not
        c.execute("CREATE TABLE IF NOT EXISTS _litestream_seq (id INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS _litestream_lock (id INTEGER)")
    monkeypatch.setattr(server, "_sys_teams_on_disk", lambda: [
        {"team": a, "path": os.path.join(server.TENANTS_DIR, a, "roadmap.db"), "sizeBytes": 1, "mtime": None},
        {"team": b, "path": os.path.join(server.TENANTS_DIR, b, "roadmap.db"), "sizeBytes": 1, "mtime": None},
    ])
    r = client.get("/api/system/status", headers=_hdr(team, "admin"))
    sc = r.json()["schema"]
    assert sc["consistent"] is True                                   # the _litestream_* diff is ignored
    assert not any("litestream" in (x.get("table") or "") for x in sc["drift"])


def test_schema_consistent_when_uniform(client, team, allow_admin, monkeypatch):
    a = f"{team}_uniA"
    b = f"{team}_uniB"
    os.makedirs(os.path.join(server.TENANTS_DIR, a), exist_ok=True)
    os.makedirs(os.path.join(server.TENANTS_DIR, b), exist_ok=True)
    server.init_team_db(a)
    server.init_team_db(b)
    monkeypatch.setattr(server, "_sys_teams_on_disk", lambda: [
        {"team": a, "path": os.path.join(server.TENANTS_DIR, a, "roadmap.db"), "sizeBytes": 1, "mtime": None},
        {"team": b, "path": os.path.join(server.TENANTS_DIR, b, "roadmap.db"), "sizeBytes": 1, "mtime": None},
    ])
    r = client.get("/api/system/status", headers=_hdr(team, "admin"))
    assert r.json()["schema"]["consistent"] is True


# ── Degrade, never 500 (Part 2.2 / acceptance 5.1.4) ──────────────────────────

def test_one_probe_fails_degrades_not_500(client, team, allow_admin, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("forced disk probe failure")
    monkeypatch.setattr(server, "_sys_disk", _boom)
    r = client.get("/api/system/status", headers=_hdr(team, "admin"))
    assert r.status_code == 200                       # the page survived a probe failure
    assert r.json()["disk"]["status"] == "unknown"    # that ONE field degraded


def test_freshness_not_wired_when_no_metrics_addr(client, team, allow_admin, monkeypatch):
    monkeypatch.delenv("LITESTREAM_METRICS_ADDR", raising=False)
    r = client.get("/api/system/status", headers=_hdr(team, "admin"))
    d = r.json()
    assert d["freshness"]["status"] == "not_wired"
    assert d["metricsWired"] is False


# ── LITESTREAM-FRESHNESS-1: never fabricate an age from a non-timestamp gauge ──

# A real trimmed sample of Litestream v0.3.13's :9091/metrics (captured from prod 2026-09-01).
# It carries rich per-DB gauges but NO per-DB replication timestamp. Note the traps:
#   - litestream_sync_seconds: NAME matches the freshness heuristic (sync + second) but the value
#     is a cumulative DURATION (222.68), NOT a unix epoch - must be rejected by the >1e9 gate.
#   - litestream_replica_wal_offset / db_size: large per-DB numbers that are a byte offset / size,
#     never a time - must never be read as an age.
_V0313_METRICS = (
    'litestream_db_size{db="/data/tenants/dev/roadmap.db"} 4.698112e+06\n'
    'litestream_replica_wal_offset{db="/data/tenants/dev/roadmap.db",name="s3"} 498552\n'
    'litestream_replica_wal_index{db="/data/tenants/dev/roadmap.db",name="s3"} 1080\n'
    'litestream_sync_count{db="/data/tenants/dev/roadmap.db"} 51657\n'
    'litestream_sync_seconds{db="/data/tenants/dev/roadmap.db"} 222.68076454999832\n'
    'litestream_sync_error_count{db="/data/tenants/dev/roadmap.db"} 0\n'
    'litestream_replica_operation_total{operation="PUT",replica_type="s3"} 21422\n'
    'process_start_time_seconds 1.78821964511e+09\n'
)


def test_freshness_never_fabricates_age_on_real_v0313_metrics(monkeypatch):
    class _Resp:
        def read(self): return _V0313_METRICS.encode()
    monkeypatch.setattr(server, "urlopen", lambda *a, **k: _Resp())
    monkeypatch.setenv("LITESTREAM_METRICS_ADDR", ":9091")
    fr = server._sys_litestream_freshness([{"team": "dev", "path": "/data/tenants/dev/roadmap.db"}])
    assert fr["status"] == "reachable"                       # the endpoint answered
    assert fr["byTeam"]["dev"]["ageSeconds"] is None         # but NO age fabricated from offset/size/duration
    assert fr["byTeam"]["dev"]["lastTs"] is None


def test_freshness_unreachable_degrades(monkeypatch):
    def _boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(server, "urlopen", _boom)
    monkeypatch.setenv("LITESTREAM_METRICS_ADDR", ":9091")
    fr = server._sys_litestream_freshness([{"team": "dev", "path": "/data/tenants/dev/roadmap.db"}])
    assert fr["status"] == "unreachable"                     # degrade, never a red backup failure


# ── The client boolean carrier (Addendum B1.3) ────────────────────────────────

def test_all_carries_sysstatusvisible_true_for_listed(client, team, monkeypatch):
    monkeypatch.setattr(server, "SYS_STATUS_USERS", frozenset({"admin"}))
    r = client.get("/api/all", headers=_hdr(team, "admin"))
    assert r.json()["sysStatusVisible"] is True


def test_all_hides_flag_for_unlisted_and_never_leaks_list(client, team, monkeypatch):
    monkeypatch.setattr(server, "SYS_STATUS_USERS", frozenset({"secret.operator"}))
    r = client.get("/api/all", headers=_hdr(team, "admin"))
    body = r.text
    assert r.json()["sysStatusVisible"] is False
    # the allowlist itself must never reach the client - only the boolean does
    assert "secret.operator" not in body
    assert "SYS_STATUS_USERS" not in body
