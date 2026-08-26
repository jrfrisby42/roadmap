"""BLOCK-REASON-1: coded Blocked reasons.

`blockedReasons` is a per-team config list (like changeReasons/deferReasons). When non-empty,
the Flag-Issue modal offers a required coded reason on the Blocked type; the chosen reason +
optional note land on the item as `blockedReason` / `blockedNote`, which makes stalls
filterable, groupable and reportable.

Server surface exercised here:
  * config: blockedReasons in VALID_KEYS, seeds [] on a new team, returned by /api/all,
    and - crucially - is NOT re-seeded on a restart (an emptied/populated list survives).
  * item fields cleared SERVER-SIDE at the Blocked binding (update_project) whenever an item
    LEAVES the blocked status, so no stale qualifier survives an un-block by the picker OR the
    ordinary status control. This is the SAME route (and the same boundary) as preBlockStatus.
  * dual-list: blockedReason/blockedNote follow deferReason - NOT server-owned, dropped on
    recurrence spawn (a new occurrence is not blocked).

The bulk/planning boundary is asserted too: like preBlockStatus, the clear lives in
update_project, so a status change via /api/items/bulk (which bypasses update_project) does
NOT clear it. That is the pre-existing behavior for preBlockStatus, matched deliberately.
"""
import json

import server


# ── Config key ────────────────────────────────────────────────────────────────

def test_blocked_reasons_in_valid_keys():
    assert "blockedReasons" in server.VALID_KEYS


def test_new_team_seeds_blocked_reasons_empty(client, team, admin_headers):
    # Empty = feature off = today's free-text Blocked behaviour (the compatibility rule).
    assert client.get("/api/all", headers=admin_headers).json().get("blockedReasons") == []


def test_put_persists_blocked_reasons(client, team, admin_headers):
    r = client.put("/api/config/blockedReasons",
                   json=["Vendor", "User", "Approval", "Hardware"], headers=admin_headers)
    assert r.status_code == 200
    assert client.get("/api/all", headers=admin_headers).json()["blockedReasons"] == \
        ["Vendor", "User", "Approval", "Hardware"]


def test_put_blocked_reasons_requires_admin(client, team, editor_headers):
    assert client.put("/api/config/blockedReasons", json=["Vendor"],
                      headers=editor_headers).status_code == 403


# ── Re-seed trap: an emptied list survives a restart (the spec's Part 1.1 hazard) ──

def test_populated_blocked_reasons_survive_restart(client, team, admin_headers):
    client.put("/api/config/blockedReasons", json=["Vendor", "User"], headers=admin_headers)
    server._migrated_teams.discard(team)   # simulate a boot
    server._migrate_config_keys(team)
    assert client.get("/api/all", headers=admin_headers).json()["blockedReasons"] == ["Vendor", "User"]


def test_emptied_blocked_reasons_survive_restart(client, team, admin_headers):
    # Admin turns the feature OFF by emptying the list; a restart must NOT resurrect anything.
    # blockedReasons is absent from _migrate_config_keys entirely, so it is never touched.
    client.put("/api/config/blockedReasons", json=[], headers=admin_headers)
    server._migrated_teams.discard(team)
    server._migrate_config_keys(team)
    assert client.get("/api/all", headers=admin_headers).json()["blockedReasons"] == []


def test_blocked_reasons_absent_from_migration_dict(client, team, admin_headers):
    # A restart on a team that NEVER set the key leaves no config row; /api/all still defaults [].
    with server.db(team) as c:
        c.execute("DELETE FROM config WHERE key='blockedReasons'")
    server._migrated_teams.discard(team)
    server._migrate_config_keys(team)
    with server.db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='blockedReasons'").fetchone()
    assert row is None                                                     # not seeded by migration
    assert client.get("/api/all", headers=admin_headers).json()["blockedReasons"] == []   # defaulted


# ── The server-side clear at the Blocked binding (update_project) ───────────────

def _mk(client, headers, **f):
    return client.post("/api/projects", json={"name": "Item", "status": "Planned", **f},
                       headers=headers).json()


def _stored(team, pid):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])


def _enable_blocked(client, headers):
    client.put("/api/config/statusIsBlocked", json={"Blocked": True}, headers=headers)


def test_ordinary_status_change_clears_reason_and_note(client, team, admin_headers):
    """Moving out of Blocked via the ordinary status control clears the coded reason + note,
    EVEN when the client PUT still carries them (the server binding is the sole guard)."""
    _enable_blocked(client, admin_headers)
    pid = _mk(client, admin_headers, status="Blocked", preBlockStatus="In Progress",
              blockedReason="Vendor", blockedNote="Dell support ticket 123")["id"]
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "In Progress",
                         "blockedReason": "Vendor", "blockedNote": "Dell support ticket 123"},
                   headers=admin_headers)
    assert r.status_code == 200
    assert "blockedReason" not in r.json()
    assert "blockedNote" not in r.json()
    stored = _stored(team, pid)
    assert "blockedReason" not in stored and "blockedNote" not in stored


def test_unblock_picker_path_clears_reason_and_note(client, team, admin_headers):
    """The un-block picker restores the stashed status (status == preBlockStatus). Same PUT
    route as above, so the reason + note clear here too."""
    _enable_blocked(client, admin_headers)
    pid = _mk(client, admin_headers, status="Blocked", preBlockStatus="Planned",
              blockedReason="Approval", blockedNote="Waiting on CFO")["id"]
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "Planned", "preBlockStatus": "Planned",
                         "blockedReason": "Approval", "blockedNote": "Waiting on CFO"},
                   headers=admin_headers)
    assert r.status_code == 200
    assert "blockedReason" not in r.json() and "blockedNote" not in r.json()


def test_staying_blocked_keeps_reason_and_note(client, team, admin_headers):
    """A save that keeps the item Blocked (e.g. a rename) must not wipe the qualifier."""
    _enable_blocked(client, admin_headers)
    pid = _mk(client, admin_headers, status="Blocked",
              blockedReason="Hardware", blockedNote="RMA in flight")["id"]
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Renamed", "status": "Blocked",
                         "blockedReason": "Hardware", "blockedNote": "RMA in flight"},
                   headers=admin_headers)
    assert r.json().get("blockedReason") == "Hardware"
    assert r.json().get("blockedNote") == "RMA in flight"


def test_feature_off_never_touches_the_fields(client, team, admin_headers):
    """statusIsBlocked empty (default): moving status is an ordinary edit and must not strip
    fields that merely happen to be named blockedReason/blockedNote."""
    pid = _mk(client, admin_headers, status="Blocked",
              blockedReason="stale", blockedNote="stale")["id"]
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "In Progress",
                         "blockedReason": "stale", "blockedNote": "stale"},
                   headers=admin_headers)
    # Feature off -> no Blocked status defined -> the binding never runs -> fields pass through.
    assert r.json().get("blockedReason") == "stale"
    assert r.json().get("blockedNote") == "stale"


def test_bulk_status_change_does_not_clear_matching_preblockstatus_boundary(client, team, admin_headers):
    """The clear lives in update_project. /api/items/bulk writes status directly (it bypasses
    update_project), so it clears NEITHER blockedReason NOR preBlockStatus - the pre-existing
    boundary, matched deliberately (blockedReason is exactly as covered as preBlockStatus)."""
    _enable_blocked(client, admin_headers)
    pid = _mk(client, admin_headers, status="Blocked", preBlockStatus="Planned",
              blockedReason="Vendor", blockedNote="n")["id"]
    r = client.post("/api/items/bulk",
                    json={"ids": [pid], "patch": {"status": "In Progress"}}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["updated"] == 1
    stored = _stored(team, pid)
    # Both survive the bulk move - identical (non-)handling. This documents the boundary; it is
    # NOT a regression (preBlockStatus already behaved this way before this stage).
    assert stored.get("blockedReason") == "Vendor"
    assert stored.get("preBlockStatus") == "Planned"


# ── Dual-list: recurrence spawn drops the fields (a new occurrence is not blocked) ──

def test_blocked_reason_not_inherited_on_recurrence_spawn(client, team, admin_headers):
    pid = _mk(client, admin_headers, recurrence="weekly", start="2026-01-01", dueWeeks=2,
              blockedReason="Vendor", blockedNote="ticket")["id"]
    child = client.post(f"/api/projects/{pid}/recur", json={}, headers=admin_headers).json()
    stored = _stored(team, child["id"])
    assert "blockedReason" not in stored
    assert "blockedNote" not in stored


def test_blocked_reason_fields_are_in_recurrence_skip_keys():
    assert "blockedReason" in server.RECURRENCE_SKIP_KEYS
    assert "blockedNote" in server.RECURRENCE_SKIP_KEYS


def test_blocked_reason_fields_are_not_server_owned():
    # Follows the deferReason precedent: NOT server-owned (the client legitimately sets them on
    # the flag PUT), dropped on recurrence spawn instead.
    assert "blockedReason" not in server.SERVER_OWNED_FIELDS
    assert "blockedNote" not in server.SERVER_OWNED_FIELDS
    assert "deferReason" not in server.SERVER_OWNED_FIELDS   # the precedent it mirrors
