"""JIRA-DEDUPE-1 - the Jira-creating paths never create a second item for a key.

The development duplicates (2026-09-28 read) had these live causes, each guarded here:
  - two pulls racing across gunicorn workers both inserted FRAZ-11351 (FRZ-1033/1034) -> the pull decides
    what is new inside a BEGIN IMMEDIATE write txn, re-reading the DB;
  - a user unlinked FRAZ-11344 from a pulled item and the next pull re-created it (FRZ-1025 -> FRZ-1026)
    -> a key the pull has EVER created (its jira:pull-create audit rows) is never pulled again (J.R.:
    skip them, after an unlink or a delete);
  - sync-children only deduped against the parent's own children (the 2026-04-20 burst) -> it refuses a
    key ANY item holds;
  - create-issue linked the new key only via the client's follow-up PUT -> the server links it itself.
Jira is mocked throughout; no network.
"""
import json
import threading

import server


CTX = {
    "type_rev": {"Story": "Story"}, "status_eff": {"New": "New"}, "overlay": {},
    "proj_rev": {"FRAZ": "Fraznet"}, "pods": [], "assignee_map": {}, "released": set(),
}


def _iss(key, summary=None):
    return {"key": key, "fields": {"summary": summary or key, "issuetype": {"name": "Story"},
                                   "status": {"name": "New"}, "project": {"key": "FRAZ"}}}


def _auth(team):
    return {"team": team, "username": "admin"}


def _mock_plan(monkeypatch, issues, before_return=None):
    def plan(team):
        if before_return:
            before_return()
        return {"ready": True, "flowKeys": set()}, list(issues)   # a deliberately STALE plan
    monkeypatch.setattr(server, "_pull_plan_issues", plan)
    monkeypatch.setattr(server, "_pull_context", lambda team: CTX)
    monkeypatch.setattr(server, "_link_pull_parents", lambda *a, **k: None)


def _items_with_key(team, key):
    with server.db(team) as c:
        return [r["id"] for r in c.execute("SELECT id, data FROM projects").fetchall()
                if key in (json.loads(r["data"]).get("jiraTickets") or [])]


def _count(team):
    with server.db(team) as c:
        return c.execute("SELECT COUNT(*) FROM projects").fetchone()[0]


def _insert(team, **fields):
    item = {"name": "Item", "product": "Fraznet", "type": "Story", "status": "New", **fields}
    with server.db(team) as c:
        server._assign_item_key(c, item)
        return server._insert_project(c, item)


# ── the pull ───────────────────────────────────────────────────────────────────────────────────────────
def test_pull_skips_key_already_on_an_item_even_with_stale_plan(team, monkeypatch):
    _insert(team, name="Pushed", jiraTickets=["FRAZ-1"])
    _mock_plan(monkeypatch, [_iss("FRAZ-1"), _iss("FRAZ-2")])
    r = server.jira_pull(body={}, auth=_auth(team))
    assert [c["key"] for c in r["created"]] == ["FRAZ-2"]
    assert {"key": "FRAZ-1", "skip": "already-in-flow"} in r["skipped"]
    assert len(_items_with_key(team, "FRAZ-1")) == 1


def test_pull_dedupes_a_key_repeated_within_one_plan(team, monkeypatch):
    _mock_plan(monkeypatch, [_iss("FRAZ-3"), _iss("FRAZ-3")])
    r = server.jira_pull(body={}, auth=_auth(team))
    assert r["createdCount"] == 1 and len(_items_with_key(team, "FRAZ-3")) == 1


def test_pull_never_recreates_an_unlinked_pulled_item(team, monkeypatch):
    # FRZ-1025 -> FRZ-1026: pulled, then the key was unlinked, then the next pull re-created it.
    _mock_plan(monkeypatch, [_iss("FRAZ-4")])
    first = server.jira_pull(body={}, auth=_auth(team))
    pid = first["created"][0]["id"]
    with server.db(team) as c:
        p = json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])
        p["jiraTickets"] = []
        server._save_project(c, pid, p)
    before = _count(team)
    again = server.jira_pull(body={}, auth=_auth(team))
    assert again["createdCount"] == 0 and _count(team) == before
    assert _items_with_key(team, "FRAZ-4") == []


def test_pull_never_recreates_a_deleted_pulled_item(team, monkeypatch):
    _mock_plan(monkeypatch, [_iss("FRAZ-5")])
    pid = server.jira_pull(body={}, auth=_auth(team))["created"][0]["id"]
    with server.db(team) as c:
        c.execute("DELETE FROM projects WHERE id=?", (pid,))
    assert server.jira_pull(body={}, auth=_auth(team))["createdCount"] == 0
    assert _items_with_key(team, "FRAZ-5") == []


def test_pull_create_audit_commits_with_the_item(team, monkeypatch):
    _mock_plan(monkeypatch, [_iss("FRAZ-6")])
    server.jira_pull(body={}, auth=_auth(team))
    with server.db(team) as c:
        assert "FRAZ-6" in server._jira_keys_pulled_before(c)
        n = c.execute("SELECT COUNT(*) FROM audit_log WHERE action='jira:pull-create'").fetchone()[0]
    assert n == 1


def test_candidates_exclude_previously_pulled_keys(team, monkeypatch):
    _mock_plan(monkeypatch, [_iss("FRAZ-7")])
    server.jira_pull(body={}, auth=_auth(team))
    with server.db(team) as c:            # unlink it, so only the audit record remembers the key
        for r in c.execute("SELECT id, data FROM projects").fetchall():
            p = json.loads(r["data"]); p["jiraTickets"] = []; server._save_project(c, r["id"], p)
    monkeypatch.undo()
    for k, v in {"jiraPullFloor": "2026-01-01", "jiraPullTypes": ["Story"],
                 "jiraProjectMapping": {"Fraznet": "FRAZ"}}.items():
        with server.db(team) as c:
            c.execute("INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (k, json.dumps(v)))
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_jira_req", lambda *a, **k: {})
    monkeypatch.setattr(server, "_jira_search_all", lambda *a, **k: [_iss("FRAZ-7"), _iss("FRAZ-8")])
    core = server._jira_compute_candidates(team)
    assert [c["key"] for c in core["candidates"]] == ["FRAZ-8"] and core["already"] == 1


def test_two_concurrent_pulls_create_one_item(team, monkeypatch):
    # FRZ-1033/1034: two runs both computed their plan before either wrote. The barrier forces exactly
    # that interleaving; the BEGIN IMMEDIATE re-check must let only one insert.
    gate = threading.Barrier(2, timeout=10)
    _mock_plan(monkeypatch, [_iss("FRAZ-9")], before_return=gate.wait)
    results, errors = [], []
    def run():
        try:
            results.append(server.jira_pull(body={}, auth=_auth(team)))
        except Exception as e:           # surfaced below, never swallowed
            errors.append(e)
    ts = [threading.Thread(target=run) for _ in range(2)]
    for t in ts: t.start()
    for t in ts: t.join(30)
    assert not errors, errors
    assert sorted(r["createdCount"] for r in results) == [0, 1]
    assert len(_items_with_key(team, "FRAZ-9")) == 1


# ── sync-children ──────────────────────────────────────────────────────────────────────────────────────
def test_sync_children_refuses_a_key_held_by_another_item(team, monkeypatch):
    other = _insert(team, name="Pulled elsewhere", jiraTickets=["FRAZ-21"], jiraSource="pull")
    parent = {"name": "Recurring", "jiraTickets": ["FRAZ-20"], "start": "2026-09-01", "due": "2026-09-08"}
    pid = _insert(team, **parent)
    monkeypatch.setattr(server, "_get_jira_children", lambda t: [
        {"key": "FRAZ-21", "summary": "dup", "status": "New", "issueType": "Story"},
        {"key": "FRAZ-22", "summary": "new", "status": "New", "issueType": "Story"}])
    monkeypatch.setattr(server, "_jira_status_to_roadmap", lambda s, team: "New")
    monkeypatch.setattr(server, "_jira_type_to_roadmap", lambda t, team: "Story")
    r = server._do_sync_children(pid, {**parent, "id": pid}, team, "admin")
    assert r["created"] == 1 and r["skipped"] == 1
    assert _items_with_key(team, "FRAZ-21") == [other]
    assert len(_items_with_key(team, "FRAZ-22")) == 1


# ── create-issue ───────────────────────────────────────────────────────────────────────────────────────
def test_create_issue_links_the_key_server_side_preserving_updated_ts(client, team, admin_headers, monkeypatch):
    pid = _insert(team, name="Pushed item")
    with server.db(team) as c:
        ts_before = c.execute("SELECT updated_ts FROM projects WHERE id=?", (pid,)).fetchone()[0]
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_jira_req", lambda method, path, *a, **k:
                        {"key": "FRAZ-30"} if path == "/rest/api/3/issue" else {})
    r = client.post("/api/jira/create-issue", headers=admin_headers,
                    json={"item_id": pid, "item_name": "Pushed item", "project_key": "FRAZ"})
    assert r.status_code == 200 and r.json()["key"] == "FRAZ-30"
    # No client PUT follows here - the server alone must have linked it.
    assert _items_with_key(team, "FRAZ-30") == [pid]
    with server.db(team) as c:
        assert c.execute("SELECT updated_ts FROM projects WHERE id=?", (pid,)).fetchone()[0] == ts_before
