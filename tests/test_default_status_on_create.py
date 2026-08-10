"""Default status on create (5.4.5).

`_insert_project` is the single creation chokepoint. It now assigns a config-resolved default
status when the incoming item has none, closing the hole where `create_project` (and any
programmatic path such as intake or the AssetHub handoff) could store an item with no status -
invisible to status-filtered views, unbucketed in Reports, never terminal.

The default is resolved through the `statusIsDefault` flag map, never a literal, via
`_resolve_default_status` with a defensive config-edge ladder (Part 4). Set only when absent,
so paths that legitimately supply a status (client edit, Jira mapping, import, planning) win.
"""
import json
import logging

import pytest

import server


def _set_cfg(team, key, val):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(val)))


def _stored_status(team, pid):
    with server.db(team) as c:
        d = json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])
    return d.get("status")


# ── The invariant: assert at the insert level, not through one endpoint ──────────

def test_insert_project_always_assigns_a_status(team):
    _set_cfg(team, "statusIsDefault", {"Planned": True})
    with server.db(team) as c:
        pid = server._insert_project(c, {"name": "no status here"})
    assert _stored_status(team, pid) == "Planned"


def test_empty_and_whitespace_status_treated_as_absent(team):
    _set_cfg(team, "statusIsDefault", {"Planned": True})
    with server.db(team) as c:
        pid_empty = server._insert_project(c, {"name": "a", "status": ""})
        pid_ws    = server._insert_project(c, {"name": "b", "status": "   "})
    assert _stored_status(team, pid_empty) == "Planned"
    assert _stored_status(team, pid_ws) == "Planned"


# ── The endpoint case observed live ──────────────────────────────────────────────

def test_create_project_no_status_gets_default(client, team, admin_headers):
    _set_cfg(team, "statusIsDefault", {"Planned": True})
    r = client.post("/api/projects", json={"name": "x"}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "Planned"
    assert _stored_status(team, r.json()["id"]) == "Planned"


def test_create_project_preserves_supplied_status(client, team, admin_headers):
    _set_cfg(team, "statusIsDefault", {"Planned": True})
    r = client.post("/api/projects", json={"name": "x", "status": "In Testing"}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "In Testing"          # supplied status wins, not overridden
    assert _stored_status(team, r.json()["id"]) == "In Testing"


def test_default_status_resolves_per_team_not_hardcoded(client, team, admin_headers):
    _set_cfg(team, "statuses", ["Alpha", "Beta", "Gamma"])
    _set_cfg(team, "statusIsDefault", {"Gamma": True})
    r = client.post("/api/projects", json={"name": "x"}, headers=admin_headers)
    assert r.json()["status"] == "Gamma"               # this team's own default, no literal


# ── Other creation paths ─────────────────────────────────────────────────────────

def test_intake_creates_item_with_status(client, team):
    _set_cfg(team, "intakeEnabled", True)
    _set_cfg(team, "statusIsDefault", {"Planned": True})
    # A type is now required when several are offered (no silent coercion) - send one.
    r = client.post(f"/api/intake/{team}",
                    json={"title": "Need a thing", "type": "Feature",
                          "email": "user@example.com", "name": "U"})
    assert r.status_code == 200, r.text
    assert _stored_status(team, r.json()["id"])        # non-empty


def test_recurrence_child_gets_configured_default_status(client, team, admin_headers):
    _set_cfg(team, "statusIsDefault", {"Planned": True})
    pid = client.post("/api/projects",
                      json={"name": "r", "recurrence": "weekly", "start": "2026-01-01", "dueWeeks": 2},
                      headers=admin_headers).json()["id"]
    child = client.post(f"/api/projects/{pid}/recur", json={}, headers=admin_headers).json()
    assert child["status"] == "Planned"                # configured default, not a literal


# ── Part 4 config-edge ladder (assert at the resolver) ───────────────────────────

def test_rung1_exactly_one_flagged(team):
    _set_cfg(team, "statuses", ["A", "B", "C"])
    _set_cfg(team, "statusIsDefault", {"B": True})
    with server.db(team) as c:
        assert server._resolve_default_status(c) == "B"


def test_rung2_several_flagged_uses_first_in_order_and_warns(team, caplog):
    _set_cfg(team, "statuses", ["A", "B", "C"])
    _set_cfg(team, "statusIsDefault", {"C": True, "B": True})
    with caplog.at_level(logging.WARNING):
        with server.db(team) as c:
            assert server._resolve_default_status(c) == "B"   # first in statuses order
    assert any("multiple statusIsDefault" in r.message for r in caplog.records)


def test_rung3_none_flagged_uses_first_status_and_warns(team, caplog):
    _set_cfg(team, "statuses", ["A", "B", "C"])
    _set_cfg(team, "statusIsDefault", {})
    with caplog.at_level(logging.WARNING):
        with server.db(team) as c:
            assert server._resolve_default_status(c) == "A"
    assert any("no valid statusIsDefault" in r.message for r in caplog.records)


def test_rung4_empty_statuses_fails_the_create(team):
    _set_cfg(team, "statuses", [])
    _set_cfg(team, "statusIsDefault", {})
    with server.db(team) as c:
        with pytest.raises(server.HTTPException):
            server._resolve_default_status(c)


def test_stale_default_flag_not_in_statuses_falls_to_first(team, caplog):
    """A statusIsDefault flag pointing at a status no longer in `statuses` is treated as none
    flagged (rung 3), not honored."""
    _set_cfg(team, "statuses", ["A", "B", "C"])
    _set_cfg(team, "statusIsDefault", {"Zombie": True})
    with caplog.at_level(logging.WARNING):
        with server.db(team) as c:
            assert server._resolve_default_status(c) == "A"
