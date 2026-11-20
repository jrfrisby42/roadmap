"""GANTT-BASELINE-1 (J.R. 2026-11-20: "do the baseline"). A baseline is a frozen copy of each open project's start + due,
taken automatically once per quarter (the first time baselines are read in it) or by an admin; the Gantt compares
against it (grey planned bar, slip badge, summary card). Server: snapshot rules + endpoints + roles. Client: source
guards (behaviour verified headless). Tool: the audit-log rebuild of a past quarter's baseline."""
import json
import pathlib

import server

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = (ROOT / "roadmap.html").read_text(encoding="utf-8", errors="replace")


def _put(team, pid, **d):
    d.setdefault("id", pid)
    with server.db(team) as c:
        c.execute("INSERT OR REPLACE INTO projects(id, data) VALUES(?, ?)", (pid, json.dumps(d)))


def _terminal(team):
    with server.db(team) as c:
        r = c.execute("SELECT value FROM config WHERE key='statusIsTerminal'").fetchone()
    return [k for k, v in json.loads(r["value"]).items() if v][0]


def test_snapshot_takes_open_projects_own_dates_or_their_work():
    rows = {
        1: {"id": 1, "type": "Roadmap Item", "status": "Planned", "name": "Own", "start": "2026-10-01", "due": "2026-11-01", "revised": "2026-11-15", "dev": "A"},
        2: {"id": 2, "type": "Roadmap Item", "status": "Planned", "name": "Rolled"},
        3: {"id": 3, "type": "Story", "parent": 2, "start": "2026-10-05", "due": "2026-10-20"},
        4: {"id": 4, "type": "Story", "parent": 3, "start": "2026-10-10", "due": "2026-12-01"},
        5: {"id": 5, "type": "Story", "parent": 2, "sprintId": "s1"},                      # undated -> lends its sprint
        6: {"id": 6, "type": "Story", "status": "Planned", "name": "Not a project", "start": "2026-10-01", "due": "2026-10-09"},
        7: {"id": 7, "type": "Roadmap Item", "status": "Released", "name": "Done", "start": "2026-09-01", "due": "2026-09-30"},
        8: {"id": 8, "type": "Roadmap Item", "status": "Planned", "name": "Undated"},
        9: {"id": 9, "type": "Epic", "status": "Planned", "name": "Archived", "start": "2026-10-01", "due": "2026-10-09", "archived": True},
    }
    cfg = {"projectTypes": ["Roadmap Item", "Epic"], "statusIsTerminal": {"Released": True},
           "sprints": [{"id": "s1", "startDate": "2026-12-07", "endDate": "2026-12-18", "state": "Planned"}]}
    out = server._baseline_items(rows, cfg)
    assert set(out) == {"1", "2"}
    assert out["1"] == {"s": "2026-10-01", "d": "2026-11-15", "n": "Own", "t": "A"}       # revised wins over due
    assert out["2"]["s"] == "2026-10-05" and out["2"]["d"] == "2026-12-18"              # grandchild + sprint backstop


def test_first_read_in_a_quarter_takes_the_automatic_baseline_once(client, team, admin_headers, viewer_headers):
    _put(team, 501, type="Roadmap Item", status="Planned", name="P1", start="2026-10-01", due="2026-12-01")
    b1 = client.get("/api/baselines", headers=viewer_headers).json()["baselines"]
    b2 = client.get("/api/baselines", headers=admin_headers).json()["baselines"]
    assert len(b1) == 1 and len(b2) == 1 and b1[0]["auto"] and b1[0]["id"] == b2[0]["id"]
    key, name = server._quarter_key(server.datetime.now(server._MT_ZONE).date())
    assert b1[0]["name"] == name and b1[0]["count"] == 1
    got = client.get(f"/api/baselines/{b1[0]['id']}", headers=viewer_headers).json()
    assert got["items"]["501"]["d"] == "2026-12-01"


def test_admin_takes_and_deletes_a_baseline_others_cannot(client, team, admin_headers, editor_headers):
    _put(team, 511, type="Roadmap Item", status="Planned", name="P", start="2026-10-01", due="2026-12-01")
    assert client.post("/api/baselines", json={"name": "Nope"}, headers=editor_headers).status_code == 403
    assert client.post("/api/baselines", json={"name": "x" * 61}, headers=admin_headers).status_code == 422
    r = client.post("/api/baselines", json={"name": "Planning meeting"}, headers=admin_headers).json()
    assert r["name"] == "Planning meeting" and not r["auto"] and r["count"] == 1 and r["takenBy"]
    lst = client.get("/api/baselines", headers=admin_headers).json()["baselines"]
    assert [b["name"] for b in lst][0] != "Planning meeting"        # the quarter's automatic one, taken on this first read, is newer
    assert "Planning meeting" in [b["name"] for b in lst]
    auto = [b for b in lst if b["auto"]][0]
    assert client.delete(f"/api/baselines/{auto['id']}", headers=admin_headers).status_code == 400
    assert client.delete(f"/api/baselines/{r['id']}", headers=editor_headers).status_code == 403
    assert client.delete(f"/api/baselines/{r['id']}", headers=admin_headers).status_code == 200
    assert client.get(f"/api/baselines/{r['id']}", headers=admin_headers).status_code == 404
    with server.db(team) as c:
        acts = [x["action"] for x in c.execute("SELECT action FROM audit_log WHERE action LIKE 'baseline:%'")]
    assert {"baseline:auto", "baseline:take", "baseline:delete"} <= set(acts)


def test_finished_projects_are_left_out(client, team, admin_headers):
    _put(team, 521, type="Roadmap Item", status=_terminal(team), name="Done", start="2026-10-01", due="2026-11-01")
    r = client.post("/api/baselines", json={}, headers=admin_headers).json()
    assert r["count"] == 0 and r["name"].startswith("Baseline ")


def test_rebuild_tool_undoes_changes_made_after_the_date():
    import importlib.util
    spec = importlib.util.spec_from_file_location("bb", ROOT / "tools" / "backfill_baseline.py")
    bb = importlib.util.module_from_spec(spec); spec.loader.exec_module(bb)
    assert bb._undo_values({"due": {"from": "2026-11-01", "to": "2026-12-01"}, "name": {"from": "a", "to": "b"}}) == {"due": "2026-11-01"}
    assert bb._undo_values({"jiraKey": "X-1", "fields": {"start": {"from": None, "to": "2026-10-05"}}}) == {"start": None}
    assert bb._undo_values({"changes": {"status": ["Planned", "In Progress"]}}) == {"status": "Planned"}
    assert bb._ts("2026-10-01 06:00:00 UTC").isoformat() == "2026-10-01T06:00:00+00:00"


def test_rebuild_tool_end_to_end(team, capsys, monkeypatch):
    import importlib.util, sys, datetime
    spec = importlib.util.spec_from_file_location("bb2", ROOT / "tools" / "backfill_baseline.py")
    bb = importlib.util.module_from_spec(spec); spec.loader.exec_module(bb)
    _put(team, 531, type="Roadmap Item", status="Planned", name="Moved", start="2026-10-01", due="2026-12-01")
    _put(team, 532, type="Roadmap Item", status="Planned", name="Brand new", start="2026-10-01", due="2026-12-01",
         createdAt=datetime.datetime.now(datetime.timezone.utc).isoformat())
    server.write_audit(team, "update", "admin", 531, "Moved", {"due": {"from": "2026-11-01", "to": "2026-12-01"}})
    server.write_audit(team, "jira:pull-refresh", "admin", 531, "Moved", {"jiraKey": "X-1", "fields": {"start": {"from": "2026-09-20", "to": "2026-10-01"}}})
    asof = (datetime.datetime.now(server._MT_ZONE).date() - datetime.timedelta(days=1)).isoformat()
    monkeypatch.setattr(sys, "argv", ["bb", "--team", team, "--as-of", asof])
    bb.main()
    rep = json.loads(capsys.readouterr().out.split("DRY RUN")[0])
    assert rep["projectsInBaseline"] == 1 and rep["itemsWithChangesUndone"] == 1 and rep["projectsWhoseDueMovedSince"] == 1
    with server.db(team) as c:
        assert c.execute("SELECT COUNT(*) n FROM baselines").fetchone()["n"] == 0          # dry run wrote nothing
    monkeypatch.setattr(sys, "argv", ["bb", "--team", team, "--as-of", asof, "--commit"])
    bb.main()
    with server.db(team) as c:
        r = c.execute("SELECT * FROM baselines").fetchone()
    items = json.loads(r["items"])
    assert set(items) == {"531"} and items["531"]["s"] == "2026-09-20" and items["531"]["d"] == "2026-11-01"
    assert r["quarter"] == server._quarter_key(datetime.date.fromisoformat(asof))[0] and r["taken_by"] == "rebuilt from history"


def test_client_compares_the_whole_project_and_draws_ghost_and_badge():
    assert "const _ruAll = _bl ? (_rollupAll(p) || _ru) : _ru;" in HTML
    assert "_bar += _bc.ghost;  // GANTT-BASELINE-1" in HTML
    assert "cH += _bc.ghost;   // GANTT-BASELINE-1" in HTML
    body = HTML.split("function _baseCmp(p, curEnd, finD, depth){", 1)[1].split("\n  }\n", 1)[0]
    assert "if(dd >= 4){ kind = 'slip'" in body and "else if(dd <= -4){ kind = 'early'" in body
    assert "cr > _blTaken" in body                                                 # "New" only after the baseline
    assert "setTimeout(() => _ganttLoadBase(), 0)" in HTML and "!_ganttBaseTried" in HTML   # load once, never loop


def test_view_menu_has_the_switch_picker_and_admin_controls():
    assert "sw('base', 'Compare to baseline'" in HTML
    body = HTML.split("function _frzBaseControls(pop, focusKey){", 1)[1].split("\n  }\n", 1)[0]
    assert "_val('currentRole','')==='admin'" in body
    assert "'_ganttSetBase'" in body and "'_ganttTakeBase'" in body and "'_ganttDeleteBase'" in body
    assert "isAdmin && cur && !cur.auto" in body                                    # the automatic one can't be deleted
    assert "changed.push('Compare to baseline on')" in HTML
    assert "localStorage.getItem('frazil_gantt_base') === '1'" in HTML             # default off
