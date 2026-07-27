"""Ship C, Stage C1 (5.3.0): derived service-desk measurement timestamps.

createdAt (every creation path), completedAt (first entry into a terminal status), and
firstResponseAt (first non-reporter comment, or first assignment - whichever fires first).
All server-owned, set-once, never overwritten, never backfilled, not user-editable.
"""
import json

import server


def _mk(client, admin_headers, **fields):
    body = {"name": "Ticket", "status": "New", **fields}
    r = client.post("/api/projects", json=body, headers=admin_headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _blob(team, pid):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])


def _seed_terminal(client, admin_headers):
    client.put("/api/config/statuses",
               json=["New", "In Progress", "Resolved", "Closed"], headers=admin_headers)
    # Two terminal statuses (resolution types are expressed as terminal statuses, L4).
    client.put("/api/config/statusIsTerminal", json={"Resolved": True, "Closed": True}, headers=admin_headers)


# ── createdAt: every creation path ──────────────────────────────────────────────
def test_created_at_set_on_manual_create(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    assert _blob(team, pid).get("createdAt")


def test_created_at_set_on_import(client, team, admin_headers):
    r = client.post("/api/import", json={"projects": [{"name": "Imported", "status": "New"}]},
                    headers=admin_headers)
    assert r.status_code == 200, r.text
    allr = client.get("/api/all", headers=admin_headers).json()
    got = next(p for p in allr["projects"] if p["name"] == "Imported")
    assert got.get("createdAt")   # _insert_project is the shared chokepoint for import/recur/children too


def test_created_at_preserved_and_not_changed_on_update(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    orig = _blob(team, pid)["createdAt"]
    client.put(f"/api/projects/{pid}", json={"name": "Renamed", "status": "New"}, headers=admin_headers)
    assert _blob(team, pid)["createdAt"] == orig   # server-owned, immutable after creation


# ── completedAt: first terminal entry, set-once ─────────────────────────────────
def test_completed_at_set_on_terminal(client, team, admin_headers):
    _seed_terminal(client, admin_headers)
    pid = _mk(client, admin_headers, status="In Progress")
    assert not _blob(team, pid).get("completedAt")
    client.put(f"/api/projects/{pid}", json={"status": "Resolved"}, headers=admin_headers)
    assert _blob(team, pid).get("completedAt")


def test_completed_at_not_overwritten(client, team, admin_headers):
    _seed_terminal(client, admin_headers)
    pid = _mk(client, admin_headers, status="In Progress")
    client.put(f"/api/projects/{pid}", json={"status": "Resolved"}, headers=admin_headers)
    first = _blob(team, pid)["completedAt"]
    # Move to a DIFFERENT terminal status - completion must not be overwritten (first wins).
    client.put(f"/api/projects/{pid}", json={"status": "Closed"}, headers=admin_headers)
    assert _blob(team, pid)["completedAt"] == first


def test_completed_at_blank_while_open(client, team, admin_headers):
    _seed_terminal(client, admin_headers)
    pid = _mk(client, admin_headers, status="New")
    client.put(f"/api/projects/{pid}", json={"status": "In Progress"}, headers=admin_headers)
    assert not _blob(team, pid).get("completedAt")   # non-terminal, so blank


# ── firstResponseAt: assignment branch ──────────────────────────────────────────
def test_first_response_on_assignment(client, team, admin_headers):
    pid = _mk(client, admin_headers, assignee="")
    assert not _blob(team, pid).get("firstResponseAt")
    client.put(f"/api/projects/{pid}", json={"assignee": "tech1"}, headers=admin_headers)
    fr = _blob(team, pid).get("firstResponseAt")
    assert fr
    # Reassigning does not overwrite it.
    client.put(f"/api/projects/{pid}", json={"assignee": "tech2"}, headers=admin_headers)
    assert _blob(team, pid)["firstResponseAt"] == fr


# ── firstResponseAt: comment branch (non-reporter only) ─────────────────────────
def test_first_response_on_non_reporter_comment(client, team, admin_headers, editor_headers):
    pid = _mk(client, admin_headers, reporter="reporter_x")   # reporter != commenter
    r = client.post("/api/comments", json={"item_id": pid, "body": "on it"}, headers=editor_headers)
    assert r.status_code == 200
    fr = _blob(team, pid).get("firstResponseAt")
    assert fr
    # A second comment does not overwrite it.
    client.post("/api/comments", json={"item_id": pid, "body": "more"}, headers=editor_headers)
    assert _blob(team, pid)["firstResponseAt"] == fr


def test_reporter_own_comment_is_not_a_response(client, team, admin_headers):
    # reporter defaults to the creator (admin); a comment BY the reporter is not a response.
    pid = _mk(client, admin_headers)
    client.post("/api/comments", json={"item_id": pid, "body": "reporter note"}, headers=admin_headers)
    assert not _blob(team, pid).get("firstResponseAt")


# ── Not user-editable: client cannot forge/clear the derived fields ─────────────
def test_client_cannot_forge_measurement_fields(client, team, admin_headers):
    _seed_terminal(client, admin_headers)
    pid = _mk(client, admin_headers, status="In Progress")
    # Client tries to inject a completion + response time directly - must be ignored.
    client.put(f"/api/projects/{pid}",
               json={"status": "In Progress", "completedAt": "1999-01-01T00:00:00+00:00",
                     "firstResponseAt": "1999-01-01T00:00:00+00:00"}, headers=admin_headers)
    b = _blob(team, pid)
    assert b.get("completedAt") in (None, "")        # still open, not the forged value
    assert b.get("firstResponseAt") in (None, "")    # no assignment/comment yet


def test_fields_absent_from_contributor_editable_set(client):
    assert "completedAt" not in server.CONTRIBUTOR_EDITABLE_FIELDS
    assert "firstResponseAt" not in server.CONTRIBUTOR_EDITABLE_FIELDS
    assert "createdAt" not in server.CONTRIBUTOR_EDITABLE_FIELDS


# ── No existing date field disturbed when a measurement stamp fires ─────────────
def test_existing_date_fields_untouched_on_completion(client, team, admin_headers):
    _seed_terminal(client, admin_headers)
    pid = _mk(client, admin_headers, status="In Progress", start="2026-07-01",
              due="2026-08-01", revised="2026-08-05", releaseDate="2026-08-10")
    before = _blob(team, pid)
    client.put(f"/api/projects/{pid}", json={"status": "Resolved"}, headers=admin_headers)
    after = _blob(team, pid)
    for f in ("start", "due", "revised", "releaseDate"):
        assert after.get(f) == before.get(f), f
    assert after.get("completedAt")   # the stamp did fire, without touching the dates
