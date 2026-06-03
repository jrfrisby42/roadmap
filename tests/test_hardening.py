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
