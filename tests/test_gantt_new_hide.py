"""GANTT-NEW-HIDE-1 (untouched New requests stay off the Gantt) and GANTT-SPRINT-GUESS-1 (a start Flow guessed,
`startAuto`, yields to the item's sprint in its project's bar). Server: the marker's lifecycle. Client: source
guards (behaviour verified headlessly)."""
import json
import os
import pathlib
import sqlite3
import subprocess
import sys

import server

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "roadmap.html").read_text(encoding="utf-8", errors="replace")


def _blob(team, pid):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])


def test_stamp_marks_the_start_as_flows_guess():
    p = {"status": "In Progress", "dueWeeks": 2}
    assert server._stamp_start_on_activate(p, {"In Progress": True}, False)
    assert p["startAuto"] is True and p["start"]


def test_marker_survives_unrelated_saves_and_clears_when_the_start_changes(client, team, admin_headers):
    with server.db(team) as c:
        pid = server._insert_project(c, {"name": "x", "status": "New", "start": "2026-11-11", "due": "2026-11-25", "startAuto": True})
    it = next(p for p in client.get("/api/all", headers=admin_headers).json()["projects"] if p["id"] == pid)
    it["name"] = "renamed"
    client.put(f"/api/projects/{pid}", json=it, headers=admin_headers)
    assert _blob(team, pid).get("startAuto") is True                      # untouched start keeps it
    it = next(p for p in client.get("/api/all", headers=admin_headers).json()["projects"] if p["id"] == pid)
    it["start"] = "2026-11-16"
    client.put(f"/api/projects/{pid}", json=it, headers=admin_headers)
    assert "startAuto" not in _blob(team, pid)                            # a person set the start


def test_client_cannot_set_the_marker(client, team, admin_headers):
    r = client.post("/api/projects", json={"name": "y", "status": "New", "start": "2026-11-11", "startAuto": True}, headers=admin_headers)
    pid = r.json()["id"]
    assert "startAuto" not in _blob(team, pid)
    it = r.json(); it["startAuto"] = True
    client.put(f"/api/projects/{pid}", json=it, headers=admin_headers)
    assert "startAuto" not in _blob(team, pid)
    assert "startAuto" in server.RECURRENCE_SKIP_KEYS


def test_backfill_tool_marks_only_unchanged_auto_starts(team):
    with server.db(team) as c:
        a = server._insert_project(c, {"name": "a", "status": "In Progress", "start": "2026-11-11"})
        b = server._insert_project(c, {"name": "b", "status": "In Progress", "start": "2026-11-20"})   # changed since
        for pid, d in ((a, "2026-11-11"), (b, "2026-11-11")):
            c.execute("INSERT INTO audit_log(ts,username,action,project_id,project_name,changes) VALUES(?,?,?,?,?,?)",
                      ("2026-11-11 17:00:00 UTC", "jira-sync", "jira:pull", pid, "", json.dumps({"details": [f"Start date set to {d} (moved to an active status)"]})))
    tool = str(ROOT / "tools" / "backfill_start_auto.py")
    env = dict(os.environ)
    dry = subprocess.run([sys.executable, tool, "--team", team, "--tenants", server.TENANTS_DIR], capture_output=True, text=True, env=env)
    assert dry.returncode == 0 and '"toMark": 1' in dry.stdout and "startAuto" not in _blob(team, a)
    run = subprocess.run([sys.executable, tool, "--team", team, "--tenants", server.TENANTS_DIR, "--commit"], capture_output=True, text=True, env=env)
    assert run.returncode == 0, run.stderr
    assert _blob(team, a).get("startAuto") is True and "startAuto" not in _blob(team, b)
    assert any(f.startswith("roadmap.db.startauto-bak-") for f in os.listdir(os.path.join(server.TENANTS_DIR, team)))


def test_gantt_hides_untouched_new_requests_and_prefers_sprint_over_a_guess():
    assert "if(!statusIsDefault[p.status] || p.assignee || p.start || p.due || p.revised || p.expected) return false;" in HTML
    assert "(!_hideNewOn || !_isNewRequest(p) || (_newHidden++, false))" in HTML
    assert "new request${_newHidden!==1?'s':''} not shown" in HTML and "window._frzShowNewRequests = function(){" in HTML
    assert "const _sb = c => (c.revised || ((c.start || c.due) && !c.startAuto)) ? null : _sprintSpanOf(c);" in HTML
