"""Contributor role, Phase A (5.0.0): server-side per-item write authorization.

The Contributor is Editor's ACTIONS restricted to the user's own items, enforced
here (the UI is not the gate). These tests exercise the enforcement directly via
the API - in-scope allow, out-of-scope 403, the field subset, the status subset,
and the excluded (admin/editor-only) endpoints. Reads are unscoped in Phase A, so
there is no read-scoping test here (that is Phase B).
"""
import json

import server


def _mk(client, admin_headers, **fields):
    """Create an item as admin; return its id."""
    body = {"name": "Item", "status": "Planned", **fields}
    r = client.post("/api/projects", json=body, headers=admin_headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _seed_status_flow(client, admin_headers):
    """A deterministic ordered flow + the terminal/Released flags used by the subset."""
    client.put("/api/config/statuses",
               json=["Planned", "In Progress", "In Testing", "Released"], headers=admin_headers)
    client.put("/api/config/statusIsTerminal", json={"Released": True}, headers=admin_headers)
    client.put("/api/config/statusIsReleased", json={"Released": True}, headers=admin_headers)


def _seed_status_flow_with_offflow(client, admin_headers):
    """The live-shaped flow: Blocked is a mid-list rank AND flagged off-flow; Inactive is
    ranked at position 0 and NOT off-flow (shelving stays an editor decision)."""
    client.put("/api/config/statuses",
               json=["Inactive", "New", "In Progress", "Blocked", "In Testing", "Released"],
               headers=admin_headers)
    client.put("/api/config/statusIsTerminal", json={"Released": True}, headers=admin_headers)
    client.put("/api/config/statusIsReleased", json={"Released": True}, headers=admin_headers)
    client.put("/api/config/statusIsOffFlow", json={"Blocked": True}, headers=admin_headers)


def _seed_contrib_pod(client, admin_headers, pod):
    """Give contrib1 an ownerFilter (pod) so pod-based scope resolves server-side."""
    client.put("/api/config/users",
               json=[{"username": "contrib1", "role": "contributor", "ownerFilter": pod}],
               headers=admin_headers)


# ── Scope: assignee ───────────────────────────────────────────────────────────
def test_contributor_assignee_in_scope_allowed(client, team, admin_headers, contributor_headers):
    pid = _mk(client, admin_headers, assignee="contrib1")
    r = client.put(f"/api/projects/{pid}", json={"description": "by the contributor"},
                   headers=contributor_headers)
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "by the contributor"


def test_contributor_out_of_scope_403(client, team, admin_headers, contributor_headers):
    pid = _mk(client, admin_headers, assignee="someone_else", dev="OtherPod")
    r = client.put(f"/api/projects/{pid}", json={"description": "should be blocked"},
                   headers=contributor_headers)
    assert r.status_code == 403
    # And the write did not land.
    with server.db(team) as c:
        stored = json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])
    assert stored.get("description", "") != "should be blocked"


# ── Scope: ownerFilter (pod) ────────────────────────────────────────────────────
def test_contributor_pod_in_scope_allowed(client, team, admin_headers, contributor_headers):
    _seed_contrib_pod(client, admin_headers, "PodA")
    pid = _mk(client, admin_headers, dev="PodA", assignee="")   # scope via pod, not assignee
    r = client.put(f"/api/projects/{pid}", json={"notes": "pod note"}, headers=contributor_headers)
    assert r.status_code == 200, r.text
    other = _mk(client, admin_headers, dev="PodB", assignee="")
    r2 = client.put(f"/api/projects/{other}", json={"notes": "nope"}, headers=contributor_headers)
    assert r2.status_code == 403


# ── Field subset ────────────────────────────────────────────────────────────────
def test_contributor_field_subset_allows_text_fields(client, team, admin_headers, contributor_headers):
    pid = _mk(client, admin_headers, assignee="contrib1")
    r = client.put(f"/api/projects/{pid}",
                   json={"description": "d", "notes": "n", "resolution": "r"},
                   headers=contributor_headers)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["description"] == "d" and got["notes"] == "n" and got["resolution"] == "r"


def test_contributor_field_subset_rejects_other_fields(client, team, admin_headers, contributor_headers):
    pid = _mk(client, admin_headers, assignee="contrib1", priority=3)
    # priority is not in CONTRIBUTOR_EDITABLE_FIELDS.
    r = client.put(f"/api/projects/{pid}", json={"priority": 1}, headers=contributor_headers)
    assert r.status_code == 403
    # owner reassignment (a scope-grab vector) is also blocked by the field subset.
    r2 = client.put(f"/api/projects/{pid}", json={"dev": "PodX"}, headers=contributor_headers)
    assert r2.status_code == 403


# ── Status subset ─────────────────────────────────────────────────────────────
def test_contributor_status_forward_allowed(client, team, admin_headers, contributor_headers):
    _seed_status_flow(client, admin_headers)
    pid = _mk(client, admin_headers, assignee="contrib1", status="In Progress")
    r = client.put(f"/api/projects/{pid}", json={"status": "In Testing"}, headers=contributor_headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "In Testing"


def test_contributor_status_backward_rejected(client, team, admin_headers, contributor_headers):
    _seed_status_flow(client, admin_headers)
    pid = _mk(client, admin_headers, assignee="contrib1", status="In Testing")
    r = client.put(f"/api/projects/{pid}", json={"status": "Planned"}, headers=contributor_headers)
    assert r.status_code == 403


def test_contributor_status_released_and_terminal_rejected(client, team, admin_headers, contributor_headers):
    _seed_status_flow(client, admin_headers)
    pid = _mk(client, admin_headers, assignee="contrib1", status="In Testing")
    # Released is both the released trigger and terminal here - forbidden either way.
    r = client.put(f"/api/projects/{pid}", json={"status": "Released"}, headers=contributor_headers)
    assert r.status_code == 403


# ── Off-flow (Blocked) - the 5.0.1 one-way-door fix ─────────────────────────────
def test_contributor_can_enter_offflow_status(client, team, admin_headers, contributor_headers):
    _seed_status_flow_with_offflow(client, admin_headers)
    pid = _mk(client, admin_headers, assignee="contrib1", status="In Progress")
    # In Progress (rank 2) -> Blocked (rank 3, off-flow): allowed as a flag.
    r = client.put(f"/api/projects/{pid}", json={"status": "Blocked"}, headers=contributor_headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Blocked"


def test_contributor_can_leave_offflow_status_backward(client, team, admin_headers, contributor_headers):
    _seed_status_flow_with_offflow(client, admin_headers)
    pid = _mk(client, admin_headers, assignee="contrib1", status="Blocked")
    # Blocked (rank 3) -> In Progress (rank 2): a backward rank, but allowed because the
    # current status is off-flow (returning from a flag is rank-exempt). This is the bug fix.
    r = client.put(f"/api/projects/{pid}", json={"status": "In Progress"}, headers=contributor_headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "In Progress"


def test_contributor_offflow_still_cannot_reach_terminal(client, team, admin_headers, contributor_headers):
    _seed_status_flow_with_offflow(client, admin_headers)
    pid = _mk(client, admin_headers, assignee="contrib1", status="Blocked")
    # Off-flow does not open a path to Released/terminal.
    r = client.put(f"/api/projects/{pid}", json={"status": "Released"}, headers=contributor_headers)
    assert r.status_code == 403


def test_contributor_cannot_move_to_inactive(client, team, admin_headers, contributor_headers):
    _seed_status_flow_with_offflow(client, admin_headers)
    pid = _mk(client, admin_headers, assignee="contrib1", status="In Progress")
    # Inactive is ranked at position 0 and NOT off-flow, so it is always a backward move.
    r = client.put(f"/api/projects/{pid}", json={"status": "Inactive"}, headers=contributor_headers)
    assert r.status_code == 403


def test_contributor_can_move_out_of_inactive(client, team, admin_headers, contributor_headers):
    _seed_status_flow_with_offflow(client, admin_headers)
    pid = _mk(client, admin_headers, assignee="contrib1", status="Inactive")
    # Inactive (rank 0) -> New (rank 1): a normal forward move, allowed.
    r = client.put(f"/api/projects/{pid}", json={"status": "New"}, headers=contributor_headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "New"


# ── statusIsOffFlow config SEED (5.0.2) - assert the value ARRIVES, not just the rule ──
def test_offflow_seed_derives_from_blocked_on_existing_team(client, team, admin_headers):
    """The migration path 5.0.1 shipped inert: an existing team with a blocked status but
    an empty statusIsOffFlow (and no seed marker) must have the flag derived on migration.
    This is the case the 5.0.1 suite never covered (its fixtures set the flag explicitly)."""
    # Simulate a pre-5.0.2 team: blocked status flagged, statusIsOffFlow empty (the bug),
    # seed marker absent.
    client.put("/api/config/statusIsBlocked", json={"Blocked": True}, headers=admin_headers)
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES('statusIsOffFlow',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps({}),))
        c.execute("DELETE FROM config WHERE key='statusIsOffFlowSeeded'")
    # Re-run the migration as a fresh-process restart would.
    server._migrated_teams.discard(team)
    server._migrate_config_keys(team)
    cfg = client.get("/api/all", headers=admin_headers).json()
    assert cfg["statusIsOffFlow"] == {"Blocked": True}   # derived from statusIsBlocked, no hardcoded name


def test_offflow_seed_empty_and_marked_on_fresh_team(client, team, admin_headers):
    """A freshly created team (the fixture ran init + migration): the default status set has
    no blocked status, so the derived off-flow map is empty AND the seed marker is set."""
    with server.db(team) as c:
        off  = c.execute("SELECT value FROM config WHERE key='statusIsOffFlow'").fetchone()
        mark = c.execute("SELECT value FROM config WHERE key='statusIsOffFlowSeeded'").fetchone()
    assert off is not None and json.loads(off["value"]) == {}
    assert mark is not None and json.loads(mark["value"]) is True


def test_offflow_not_resurrected_after_admin_clears(client, team, admin_headers):
    """Guardrail: once seeded (marker set), an admin who clears every off-flow status is not
    resurrected on the next restart - the marker, not emptiness, guards the one-time seed."""
    client.put("/api/config/statusIsBlocked", json={"Blocked": True}, headers=admin_headers)
    # Marker is already set by the fixture's init migration. Admin clears the map.
    client.put("/api/config/statusIsOffFlow", json={}, headers=admin_headers)
    # A restart re-runs migration; with the marker present it must NOT re-derive.
    server._migrated_teams.discard(team)
    server._migrate_config_keys(team)
    with server.db(team) as c:
        off = c.execute("SELECT value FROM config WHERE key='statusIsOffFlow'").fetchone()
    assert json.loads(off["value"]) == {}   # stayed empty; Blocked was NOT resurrected


# ── Excluded endpoints (admin/editor only; contributor 403) ─────────────────────
def test_contributor_excluded_endpoints_403(client, team, admin_headers, contributor_headers):
    pid = _mk(client, admin_headers, assignee="contrib1")
    # create
    assert client.post("/api/projects", json={"name": "X", "status": "Planned"},
                       headers=contributor_headers).status_code == 403
    # delete
    assert client.delete(f"/api/projects/{pid}", headers=contributor_headers).status_code == 403
    # config
    assert client.put("/api/config/statuses", json=["A"], headers=contributor_headers).status_code == 403
    # planning session create
    assert client.post("/api/planning-sessions", json={"type": "Sprint"},
                       headers=contributor_headers).status_code == 403
    # collection mutations (also A0-gated)
    assert client.put("/api/boards", json={"boards": []}, headers=contributor_headers).status_code == 403
    assert client.put("/api/sprints", json={"sprints": []}, headers=contributor_headers).status_code == 403
    assert client.put("/api/releases", json={"releases": []}, headers=contributor_headers).status_code == 403


# ── Comments: scoped ────────────────────────────────────────────────────────────
def test_contributor_comment_in_scope_allowed(client, team, admin_headers, contributor_headers):
    pid = _mk(client, admin_headers, assignee="contrib1")
    r = client.post("/api/comments", json={"item_id": pid, "body": "hi"}, headers=contributor_headers)
    assert r.status_code == 200, r.text


def test_contributor_comment_out_of_scope_403(client, team, admin_headers, contributor_headers):
    pid = _mk(client, admin_headers, assignee="someone_else", dev="OtherPod")
    r = client.post("/api/comments", json={"item_id": pid, "body": "hi"}, headers=contributor_headers)
    assert r.status_code == 403


# ── Attachment presign: out-of-scope refused before any S3 work ─────────────────
def test_contributor_presign_out_of_scope_403(client, team, admin_headers, contributor_headers):
    pid = _mk(client, admin_headers, assignee="someone_else", dev="OtherPod")
    r = client.post(f"/api/items/{pid}/attachments/presign",
                    json={"filename": "a.png", "contentType": "image/png", "size": 10},
                    headers=contributor_headers)
    assert r.status_code == 403   # scope is enforced before the presign is generated


# ── Regression: editor is unaffected by the contributor gates ───────────────────
def test_editor_still_edits_any_field_any_item(client, team, admin_headers, editor_headers):
    pid = _mk(client, admin_headers, assignee="someone_else", dev="OtherPod")
    r = client.put(f"/api/projects/{pid}", json={"priority": 1, "description": "editor edit"},
                   headers=editor_headers)
    assert r.status_code == 200, r.text
    assert r.json()["priority"] == 1
