"""JIRA-SPRINT-1 Stage 1 - the sprint jiraSource marker + the read-only gate. Ships inert (nothing
mirrors yet). Server: put_sprints makes a mirrored sprint read-only (no edit/remove/forge), and a Sprint
planning session is refused when the active sprint is mirrored. Client (source-asserted): the Sprints tab
drops edit/discard for a mirrored sprint, completeSprint + addToSprint guard, and the membership picker
excludes mirrored sprints.
"""
import json
import os
import pytest
import server

_HTML = None


def _html():
    global _HTML
    if _HTML is None:
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "roadmap.html")
        with open(p, encoding="utf-8") as f:
            _HTML = f.read()
    return _HTML


MIRROR = {"id": "jira-1298", "name": "Sept 21 - Oct 02", "state": "Active",
          "jiraSource": "jira", "jiraSprintId": 1298}
FLOW = {"id": "s1", "name": "Sprint 1", "state": "Completed"}


def _seed_sprints(team, sprints):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES('sprints',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(sprints),))


def _put(client, headers, sprints):
    return client.put("/api/sprints", json={"sprints": sprints}, headers=headers)


# ── put_sprints: a mirrored sprint is read-only ─────────────────────────────────────────────────────────
def test_flow_sprint_still_editable(client, team, admin_headers):
    _seed_sprints(team, [FLOW])
    r = _put(client, admin_headers, [{**FLOW, "name": "Renamed"}])
    assert r.status_code == 200


def test_mirrored_sprint_cannot_be_edited(client, team, admin_headers):
    _seed_sprints(team, [MIRROR, FLOW])
    r = _put(client, admin_headers, [{**MIRROR, "name": "Hacked"}, FLOW])
    assert r.status_code == 409


def test_mirrored_sprint_cannot_be_removed(client, team, admin_headers):
    _seed_sprints(team, [MIRROR, FLOW])
    r = _put(client, admin_headers, [FLOW])                 # mirror dropped
    assert r.status_code == 409


def test_cannot_forge_marker_on_flow_sprint(client, team, admin_headers):
    _seed_sprints(team, [FLOW])
    r = _put(client, admin_headers, [{**FLOW, "jiraSource": "jira"}])
    assert r.status_code == 409


def test_cannot_create_new_mirrored_sprint(client, team, admin_headers):
    _seed_sprints(team, [FLOW])
    r = _put(client, admin_headers, [FLOW, {"id": "x", "name": "New", "state": "Planned", "jiraSource": "jira"}])
    assert r.status_code == 409


def test_flow_edit_alongside_unchanged_mirror_ok(client, team, admin_headers):
    _seed_sprints(team, [MIRROR, FLOW])
    r = _put(client, admin_headers, [MIRROR, {**FLOW, "name": "Renamed"}])   # mirror byte-identical
    assert r.status_code == 200


# ── commit_planning_session: no Sprint session on a mirrored active sprint ──────────────────────────────
def test_sprint_session_refused_when_active_sprint_mirrored(client, team, admin_headers):
    _seed_sprints(team, [MIRROR])                            # active sprint is mirrored
    sid = client.post("/api/planning-sessions", json={"name": "S", "type": "Sprint"}, headers=admin_headers).json()["id"]
    r = client.post(f"/api/planning-sessions/{sid}/commit",
                    json={"name": "S", "type": "Sprint", "sprint_items": []}, headers=admin_headers)
    assert r.status_code == 409


def test_sprint_session_ok_when_active_sprint_is_flow(client, team, admin_headers):
    _seed_sprints(team, [{"id": "s1", "name": "Flow Active", "state": "Active"}])
    sid = client.post("/api/planning-sessions", json={"name": "S", "type": "Sprint"}, headers=admin_headers).json()["id"]
    r = client.post(f"/api/planning-sessions/{sid}/commit",
                    json={"name": "S", "type": "Sprint", "sprint_items": []}, headers=admin_headers)
    assert r.status_code == 200                              # a Flow active sprint is not gated


# ── client read-only gate (source-asserted, mirrors the test_item_composer style) ──────────────────────
def test_client_has_mirror_helper_and_gates():
    h = _html()
    assert "function _sprintIsMirrored(sp){ return !!(sp && sp.jiraSource); }" in h
    # Sprints tab suppresses edit + discard for a mirrored sprint
    assert "if(!mirrored && (sp.state==='Planned' || sp.state==='Active')) acts+=" in h
    assert "if(!mirrored && sp.state==='Planned') acts+=" in h
    # completeSprint + addToSprint guard on a mirrored current sprint (2 sites)
    assert h.count("if(_sprintIsMirrored(") >= 2
    # the item-page picker excludes mirrored sprints as add targets, and offers no "remove" for one
    assert "&& !_sprintIsMirrored(s)" in h
    assert "!_sprintIsMirrored(cur)" in h
