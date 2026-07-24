"""Notes + Resolution details (4.59.0): fixed-schema free-text item fields.

Persistence is the generic merge-patch (no whitelist), so these tests pin the
two behaviors this feature actually added on the server: the audit log records
a compact "updated" marker for notes/resolution changes - never the text
bodies - and no marker at all when the fields ride along unchanged in a
full-blob PUT.
"""
import json

import server


def _create(client, headers, **fields):
    body = {"name": "Item", "status": "Planned", **fields}
    return client.post("/api/projects", json=body, headers=headers)


def _last_update_changes(team, pid):
    with server.db(team) as c:
        row = c.execute(
            "SELECT changes FROM audit_log WHERE action='update' AND project_id=? "
            "ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    return json.loads(row["changes"]) if row and row["changes"] else {}


def test_notes_and_resolution_persist(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "Planned",
                         "notes": "line one\nline two",
                         "resolution": "rebooted the frobnicator"},
                   headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["notes"] == "line one\nline two"
    assert r.json()["resolution"] == "rebooted the frobnicator"
    # Durable in the stored blob, and preserved when a later edit omits them.
    r2 = client.put(f"/api/projects/{pid}",
                    json={"name": "Renamed", "status": "Planned"}, headers=admin_headers)
    assert r2.status_code == 200
    assert r2.json()["notes"] == "line one\nline two"
    assert r2.json()["resolution"] == "rebooted the frobnicator"
    with server.db(team) as c:
        stored = json.loads(c.execute(
            "SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])
    assert stored["notes"] == "line one\nline two"
    assert stored["resolution"] == "rebooted the frobnicator"


def test_audit_logs_compact_marker_not_body(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    secret = "SENSITIVE-NOTE-BODY-DO-NOT-AUDIT"
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "Planned",
                         "notes": secret, "resolution": secret + "-RES"},
                   headers=admin_headers)
    assert r.status_code == 200
    changes = _last_update_changes(team, pid)
    assert changes.get("notes") == "updated"
    assert changes.get("resolution") == "updated"
    assert secret not in json.dumps(changes)


def test_unchanged_notes_produce_no_audit_marker(client, team, admin_headers):
    pid = _create(client, admin_headers, notes="stable", resolution="fixed").json()["id"]
    # Full-blob PUT resends both fields unchanged (how the inline save path works).
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "Planned",
                         "notes": "stable", "resolution": "fixed", "priority": 2},
                   headers=admin_headers)
    assert r.status_code == 200
    changes = _last_update_changes(team, pid)
    assert "notes" not in changes
    assert "resolution" not in changes
    assert "priority" in changes   # the real change still audits normally
