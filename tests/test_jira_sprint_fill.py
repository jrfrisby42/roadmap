"""JIRA-SPRINT-FILL (Option B) - sprint membership overrides the TYPE exclusion; the floor always applies.
Covered: the candidate JQL shape (floor never overridable), the non-closed board-sprint resolver, the
config key round-trip + inert-when-unset default, and that an admitted Task/Bug construct to the right
Flow type while an unmapped Jira type is still skipped. Live proofs run in the acceptance.
"""
import json
import pytest
import server


def _set_cfg(team, key, val):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(val)))


# ── the candidate JQL (pure) - the floor is a separate top-level AND, never overridden ──────────────────
def test_jql_inert_without_sprint_ids():
    jql = server._jira_pull_jql("2026-08-01 00:00", ["Story", "Epic"], ["FRAZ"])
    assert 'created >= "2026-08-01 00:00"' in jql
    assert 'issuetype in ("Story", "Epic")' in jql
    assert "sprint in" not in jql and "OR sprint" not in jql   # today's behaviour, no widening


def test_jql_widens_with_sprint_ids_but_floor_holds():
    jql = server._jira_pull_jql("2026-08-01 00:00", ["Story"], ["FRAZ"], [1298, 1299])
    assert '(issuetype in ("Story") OR sprint in (1298, 1299))' in jql
    # the floor stays its OWN top-level AND clause - it is NOT inside the type/sprint OR
    assert 'created >= "2026-08-01 00:00"' in jql
    or_pos = jql.index("OR sprint in")
    assert jql[:or_pos].count(" AND ") >= 2                    # project AND created AND (type OR sprint)
    assert jql.count("created >=") == 1                        # floor appears once, not duplicated inside the OR


def test_jql_sprint_ids_are_ints_not_quoted():
    jql = server._jira_pull_jql("b", ["Story"], ["FRAZ"], ["1298", 1299])
    assert "sprint in (1298, 1299)" in jql                     # numeric, unquoted


# ── the non-closed board-sprint resolver (Jira mocked) ──────────────────────────────────────────────────
def test_board_open_sprint_ids_paginates(monkeypatch):
    pages = [
        {"values": [{"id": 1}, {"id": 2}], "isLast": False},
        {"values": [{"id": 3}], "isLast": True},
    ]
    calls = []
    def fake(method, path, *a, **k):
        calls.append(path)
        return pages[len(calls) - 1]
    monkeypatch.setattr(server, "_jira_req", fake)
    ids = server._board_open_sprint_ids("7")
    assert ids == [1, 2, 3]
    assert "state=active,future" in calls[0]                   # only non-closed sprints requested


# ── config key: registered, default "", presence-only, round-trips, in /api/all ────────────────────────
def test_sprint_board_id_registered_and_default_blank(team):
    with server.db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='jiraSprintBoardId'").fetchone()
    assert row is not None and json.loads(row["value"]) == ""   # seeded, blank = inert


def test_sprint_board_id_round_trips(client, team, admin_headers):
    r = client.put("/api/config/jiraSprintBoardId", json="1", headers=admin_headers)
    assert r.status_code == 200
    allr = client.get("/api/all", headers=admin_headers).json()
    assert allr["jiraSprintBoardId"] == "1"


def test_sprint_board_id_admin_gated(client, team, viewer_headers):
    assert client.put("/api/config/jiraSprintBoardId", json="1", headers=viewer_headers).status_code in (401, 403)


# ── _jira_compute_candidates: inert when unset, widened when the board resolves sprints ─────────────────
def _seed_pull(team, board=None):
    _set_cfg(team, "jiraPullFloor", "2026-08-01T00:00:00-06:00")
    _set_cfg(team, "jiraPullTypes", ["Story", "Epic"])
    _set_cfg(team, "jiraProjectMapping", {"Fraznet": "FRAZ"})
    if board is not None:
        _set_cfg(team, "jiraSprintBoardId", board)


def _capture_jql(monkeypatch, board_sprint_handler):
    captured = {}
    def fake_req(method, path, *a, **k):
        if "myself" in path:
            return {"timeZone": "America/Denver"}
        if "/sprint" in path:
            return board_sprint_handler(path)
        return {}
    def fake_search(jql, fields, cap=2000):
        captured["jql"] = jql
        return []
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_jira_req", fake_req)
    monkeypatch.setattr(server, "_jira_search_all", fake_search)
    return captured


def test_compute_inert_when_board_unset(team, monkeypatch):
    _seed_pull(team, board="")
    cap = _capture_jql(monkeypatch, lambda p: {"values": [], "isLast": True})
    core = server._jira_compute_candidates(team)
    assert core["ready"] and core["openSprintIds"] == []
    assert "sprint in" not in cap["jql"]                       # inert default = exactly today's query


def test_compute_widens_when_board_has_open_sprints(team, monkeypatch):
    _seed_pull(team, board="1")
    cap = _capture_jql(monkeypatch, lambda p: {"values": [{"id": 1298}, {"id": 1299}], "isLast": True})
    core = server._jira_compute_candidates(team)
    assert core["openSprintIds"] == [1298, 1299]
    assert "OR sprint in (1298, 1299)" in cap["jql"]
    assert 'created >=' in cap["jql"]                          # floor still applied


def test_compute_inert_when_board_errors(team, monkeypatch):
    # a kanban board 400s for sprints - the resolver raises, the caller falls back to type-only (inert).
    _seed_pull(team, board="11")
    def boom(path):
        raise server.HTTPException(400, "The board does not support sprints")
    cap = _capture_jql(monkeypatch, boom)
    core = server._jira_compute_candidates(team)
    assert core["openSprintIds"] == []
    assert "sprint in" not in cap["jql"]


# ── construction: an admitted Task/Bug map right; an unmapped Jira type is still skipped ────────────────
_CTX = {
    "type_rev": {"Story": "Story", "Epic": "Epic", "Task": "Task", "Bug": "Bug Fix"},   # reverse of jiraTypeMapping
    "status_eff": {}, "overlay": {}, "proj_rev": {"FRAZ": "Fraznet"}, "pods": [], "assignee_map": {},
}


def _iss(key, itype):
    return {"key": key, "fields": {"summary": key, "issuetype": {"name": itype}, "status": {"name": "To Do"},
                                   "project": {"key": "FRAZ"}}}


def test_admitted_task_and_bug_map_to_flow_types():
    task, _ = server._construct_pull_item(_iss("FRAZ-1", "Task"), _CTX, "admin")
    bug, _ = server._construct_pull_item(_iss("FRAZ-2", "Bug"), _CTX, "admin")
    assert task["type"] == "Task" and task["jiraSource"] == "pull"
    assert bug["type"] == "Bug Fix"


def test_unmapped_jira_type_still_skipped():
    item, meta = server._construct_pull_item(_iss("FRAZ-3", "Additional Work"), _CTX, "admin")
    assert item is None and meta["skip"] == "unmapped-type" and meta["jiraType"] == "Additional Work"
