"""JIRA-SPRINT-1 Stage 2 - mirror the sprint ENTITIES (read-only, Jira-owned). Entities only, no item
sprintId (Stage 3). Never touches a Flow-made sprint. Jira Agile API mocked."""
import json
import pytest
import server


def _set_cfg(team, key, val):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(val)))


def _sprints(team):
    return server._read_sprints(team)


def _js(jid, state, name=None, start="2026-09-21T00:00:00.000-06:00", end="2026-10-02T00:00:00.000-06:00"):
    return {"id": jid, "name": name or f"S{jid}", "state": state, "startDate": start, "endDate": end}


def _arm(team, monkeypatch, jira_sprints, board="1"):
    _set_cfg(team, "jiraEnabled", True)
    _set_cfg(team, "jiraSprintBoardId", board)
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_board_open_sprints", lambda bid: list(jira_sprints))


# ── pure helpers ────────────────────────────────────────────────────────────────────────────────────────
def test_state_map():
    assert server._jira_state_to_flow("active") == "Active"
    assert server._jira_state_to_flow("future") == "Planned"
    assert server._jira_state_to_flow("closed") == "Completed"
    assert server._jira_state_to_flow("") == "Completed"


def test_flow_id_scheme():
    assert server._mirror_sprint_flow_id(1298) == "jira-1298"


# ── inert when no board ─────────────────────────────────────────────────────────────────────────────────
def test_inert_without_board(team, monkeypatch):
    _set_cfg(team, "jiraEnabled", True)
    _set_cfg(team, "jiraSprintBoardId", "")
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    r = server._mirror_sprints(team, "admin")
    assert r["mirrored"] == 0 and _sprints(team) == []


# ── the mirror creates the entity ───────────────────────────────────────────────────────────────────────
def test_mirror_creates_active_sprint(team, monkeypatch):
    _arm(team, monkeypatch, [_js(1298, "active", "Sept 21 - Oct 02")])
    r = server._mirror_sprints(team, "admin")
    assert r["mirrored"] == 1 and r["created"] == 1 and r["activeSprintId"] == 1298
    sp = _sprints(team)
    assert len(sp) == 1
    m = sp[0]
    assert m["id"] == "jira-1298" and m["jiraSource"] == "jira" and m["jiraSprintId"] == 1298
    assert m["state"] == "Active" and m["name"] == "Sept 21 - Oct 02"
    assert m["startDate"] == "2026-09-21" and m["endDate"] == "2026-10-02"   # sliced to YYYY-MM-DD


def test_mirror_idempotent(team, monkeypatch):
    _arm(team, monkeypatch, [_js(1298, "active")])
    server._mirror_sprints(team, "admin")
    r2 = server._mirror_sprints(team, "admin")
    assert r2["created"] == 0 and r2["updated"] == 0 and len(_sprints(team)) == 1


# ── never touches a Flow-made sprint ────────────────────────────────────────────────────────────────────
def test_flow_sprints_untouched(team, monkeypatch):
    flow = [{"id": "s1", "name": "Sprint 1", "state": "Completed"},
            {"id": "s2", "name": "Sprint 2", "state": "Completed"}]
    _set_cfg(team, "sprints", flow)
    _arm(team, monkeypatch, [_js(1298, "active")])
    server._mirror_sprints(team, "admin")
    sp = _sprints(team)
    flow_after = [s for s in sp if not s.get("jiraSource")]
    assert flow_after == flow                         # byte-identical, order preserved
    assert any(s.get("jiraSource") == "jira" for s in sp)


# ── one-Active tiebreak: last-created (highest id) wins ─────────────────────────────────────────────────
def test_two_active_tiebreak_last_created_wins(team, monkeypatch):
    _arm(team, monkeypatch, [_js(1298, "active"), _js(1301, "active")])
    server._mirror_sprints(team, "admin")
    sp = {s["jiraSprintId"]: s for s in _sprints(team)}
    assert sp[1301]["state"] == "Active"
    assert sp[1298]["state"] == "Planned" and sp[1298].get("jiraState") == "active"
    assert sum(1 for s in _sprints(team) if s["state"] == "Active") == 1


# ── Flow-Active collision: never demote the Flow sprint; mirror written Planned + reported ─────────────
def test_flow_active_collision_keeps_flow_active(team, monkeypatch):
    _set_cfg(team, "sprints", [{"id": "s1", "name": "Flow Active", "state": "Active"}])
    _arm(team, monkeypatch, [_js(1298, "active")])
    r = server._mirror_sprints(team, "admin")
    assert r["flowActiveCollision"] == 1 and r["activeSprintId"] is None
    sp = _sprints(team)
    flow = [s for s in sp if s["id"] == "s1"][0]
    mir = [s for s in sp if s.get("jiraSource") == "jira"][0]
    assert flow["state"] == "Active"                  # Flow sprint NOT demoted
    assert mir["state"] == "Planned" and mir.get("jiraState") == "active"
    assert sum(1 for s in sp if s["state"] == "Active") == 1


# ── a mirrored sprint that leaves the non-closed window becomes Completed ──────────────────────────────
def test_completed_on_absence(team, monkeypatch):
    _arm(team, monkeypatch, [_js(1298, "active"), _js(1299, "future")])
    server._mirror_sprints(team, "admin")
    # next run: 1299 no longer returned (it closed / moved out)
    monkeypatch.setattr(server, "_board_open_sprints", lambda bid: [_js(1298, "active")])
    r = server._mirror_sprints(team, "admin")
    assert r["completedOnAbsence"] == 1
    sp = {s["jiraSprintId"]: s for s in _sprints(team)}
    assert sp[1299]["state"] == "Completed" and sp[1299]["jiraSource"] == "jira"   # kept as history
    assert sp[1298]["state"] == "Active"


# ── abort on an Agile error, zero writes ────────────────────────────────────────────────────────────────
def test_abort_on_agile_error_zero_writes(team, monkeypatch):
    _set_cfg(team, "sprints", [{"id": "s1", "name": "Sprint 1", "state": "Completed"}])
    _set_cfg(team, "jiraEnabled", True)
    _set_cfg(team, "jiraSprintBoardId", "11")
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    def boom(bid):
        raise server.HTTPException(400, "The board does not support sprints")
    monkeypatch.setattr(server, "_board_open_sprints", boom)
    r = server._mirror_sprints(team, "admin")
    assert r.get("aborted") is True and r["mirrored"] == 0
    assert _sprints(team) == [{"id": "s1", "name": "Sprint 1", "state": "Completed"}]   # untouched


# ── entities only: no item touched, no notify/activity ──────────────────────────────────────────────────
def test_no_item_or_notification_side_effects(team, monkeypatch):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES('statuses',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(["New"]),))
        item = {"name": "I", "status": "New", "sprintId": "old"}
        server._assign_item_key(c, item); pid = server._insert_project(c, item)
        before = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"]
        n0 = c.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        a0 = c.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    _arm(team, monkeypatch, [_js(1298, "active")])
    server._mirror_sprints(team, "admin")
    with server.db(team) as c:
        after = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"]
        n1 = c.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        a1 = c.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    assert after == before and n1 == n0 and a1 == a0


# ── endpoint gating ─────────────────────────────────────────────────────────────────────────────────────
def test_mirror_endpoint_admin_gated(client, team, viewer_headers):
    assert client.post("/api/jira/mirror-sprints", headers=viewer_headers).status_code in (401, 403)
