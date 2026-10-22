"""MIGRATE-QUIET-1: the boot-time config migration only writes (and logs) when a value actually changes.

An empty setting whose default is also empty (statusIsDefault {}, departments []) used to be rewritten and
logged as "Seeded" on every restart. A missing key, or an empty one with a non-empty default, still seeds."""
import json

import server


def _set(team, key, value):
    with server.db(team) as c:
        c.execute("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)", (key, json.dumps(value)))


def _run(team):
    server._migrated_teams.discard(team)   # the migration runs once per team per process; force a fresh run
    server._migrate_config_keys(team)


def _get(team, key):
    with server.db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def test_empty_value_equal_to_empty_default_is_not_reseeded(team, capsys):
    _set(team, "statusIsDefault", {})
    _set(team, "departments", [])
    capsys.readouterr()
    _run(team)
    out = capsys.readouterr().out
    assert "'statusIsDefault'" not in out and "'departments'" not in out
    assert _get(team, "statusIsDefault") == {} and _get(team, "departments") == []


def test_missing_key_is_still_seeded(team, capsys):
    with server.db(team) as c:
        c.execute("DELETE FROM config WHERE key='statusIsDefault'")
    capsys.readouterr()
    _run(team)
    assert "Seeded config key 'statusIsDefault'" in capsys.readouterr().out
    assert _get(team, "statusIsDefault") == {}


def test_empty_value_with_a_real_default_is_still_filled(team):
    _set(team, "metricLabel", "")
    _run(team)
    assert _get(team, "metricLabel") == "Story Points"


def test_second_run_is_silent(team, capsys):
    _run(team)
    capsys.readouterr()
    _run(team)
    assert "Seeded config key" not in capsys.readouterr().out
