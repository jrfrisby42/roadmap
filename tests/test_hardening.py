"""Stability/perf hardening:
  #1 SQLite busy_timeout + synchronous pragmas
  #2 500 handler hides tracebacks unless DEBUG_TRACEBACKS is enabled
  #3 TOKEN_SECRET resolution persists (no per-worker drift / restart loss)
  #4 _migrate_config_keys runs at most once per team per process
"""
import asyncio
import json

from starlette.requests import Request

import server


# ── #1: connection pragmas ────────────────────────────────────────────────────
def test_busy_timeout_and_synchronous(team):
    with server.db(team) as c:
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert c.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


# ── #2: 500 handler ────────────────────────────────────────────────────────────
def _invoke_handler():
    scope = {"type": "http", "method": "GET", "path": "/boom",
             "headers": [], "query_string": b""}
    req = Request(scope)
    resp = asyncio.run(server._unhandled_exception_handler(req, ValueError("leaky internal detail")))
    return resp, json.loads(resp.body)


def test_500_hides_traceback_by_default():
    old = server._DEBUG_TRACEBACKS
    server._DEBUG_TRACEBACKS = False
    try:
        resp, body = _invoke_handler()
        assert resp.status_code == 500
        assert body == {"detail": "Internal server error"}
        assert "traceback" not in body
        assert "leaky internal detail" not in json.dumps(body)
    finally:
        server._DEBUG_TRACEBACKS = old


def test_500_exposes_traceback_when_enabled():
    old = server._DEBUG_TRACEBACKS
    server._DEBUG_TRACEBACKS = True
    try:
        resp, body = _invoke_handler()
        assert "traceback" in body
    finally:
        server._DEBUG_TRACEBACKS = old


# ── #3: token secret resolution ────────────────────────────────────────────────
def test_token_secret_prefers_env(monkeypatch):
    monkeypatch.setenv("TOKEN_SECRET", "explicit-secret")
    assert server._load_token_secret() == "explicit-secret"


def test_token_secret_persists_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("TOKEN_SECRET", raising=False)
    monkeypatch.setattr(server, "BASE", str(tmp_path))
    s1 = server._load_token_secret()
    s2 = server._load_token_secret()
    assert s1 == s2                                   # stable across calls (== across workers)
    assert (tmp_path / ".token_secret").exists()      # survives a restart
    assert len(s1) >= 32


# ── #4: migrate-once guard ──────────────────────────────────────────────────────
def test_migrate_config_keys_runs_once(team):
    # The team fixture initialised the DB, which migrates and records the team.
    assert team in server._migrated_teams

    # Delete a previously-migrated key directly.
    with server.db(team) as c:
        c.execute("DELETE FROM config WHERE key='statusIsReleased'")

    # Guarded call should short-circuit and NOT restore it.
    server._migrate_config_keys(team)
    with server.db(team) as c:
        assert c.execute("SELECT value FROM config WHERE key='statusIsReleased'").fetchone() is None

    # With the guard cleared, migration restores the key (logic still works).
    server._migrated_teams.discard(team)
    server._migrate_config_keys(team)
    with server.db(team) as c:
        assert c.execute("SELECT value FROM config WHERE key='statusIsReleased'").fetchone() is not None


# ── Security batch (post-review MEDIUM fixes) ──────────────────────────────────
# Fixtures `client`, `team`, `admin_headers`, `editor_headers`, `viewer_headers`
# come from conftest.py.

def test_verify_password_endpoint_removed(client, viewer_headers):
    # M3: the password-oracle endpoint is gone (was: any authed user could test any
    # user's password). FastAPI returns 404/405 for an undefined route.
    r = client.post("/api/verify-password",
                    json={"username": "admin", "password": "frazil123"}, headers=viewer_headers)
    assert r.status_code in (404, 405)


def test_hash_password_requires_admin(client, team, editor_headers):
    # M4: was unauthenticated (bcrypt DoS oracle). Now admin-only.
    assert client.post("/api/hash-password", json={"password": "secret1"}).status_code == 401
    assert client.post("/api/hash-password", json={"password": "secret1"},
                       headers=editor_headers).status_code == 403
    admin = {"Authorization": f"Bearer {server.create_token(team, 'admin', 'admin')}", "X-Team": team}
    ok = client.post("/api/hash-password", json={"password": "secret1"}, headers=admin)
    assert ok.status_code == 200 and server.is_hashed(ok.json()["hashed"])


def test_update_project_cannot_forge_server_owned_fields(client, team, admin_headers, editor_headers):
    # M1: reporter identity / source / sync-gate fields are server-owned. A client PUT
    # must not be able to rewrite them (e.g. redirect reporter emails via reporterEmail).
    created = client.post("/api/projects",
                          json={"name": "Owned", "reporter": "Pat", "reporterEmail": "real@x.com",
                                "source": "portal", "dueWeeks": 4, "testWeeks": 1},
                          headers=admin_headers)
    assert created.status_code == 200, created.text
    pid = created.json()["id"]
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Owned edited", "reporterEmail": "attacker@evil.com",
                         "source": "internal", "sprintHistory": [{"x": 1}],
                         "jiraSyncSkipped": {"FRAZ-1": "Done"}},
                   headers=editor_headers)
    assert r.status_code == 200, r.text
    it = next(p for p in client.get("/api/all", headers=admin_headers).json()["projects"] if p["id"] == pid)
    assert it["name"] == "Owned edited"              # editable field applied
    assert it["reporterEmail"] == "real@x.com"       # server-owned field preserved
    assert it["source"] == "portal"
    assert it.get("sprintHistory") in (None, [])     # not forgeable
    assert not it.get("jiraSyncSkipped")
