"""JIRA-ISSUETYPE-1: "Create Jira issue" resolves the mapped issue type against the types the Jira
project actually allows and creates by id.

The admin type picker lists every type on the Jira site, so a Flow type could be mapped to one the
project's scheme lacks (development: Enhancement -> Improvement, which FRAZ does not allow), and Jira
rejected the create with a raw "Specify a valid issue type". Now: a matching type is sent by id (also
immune to same-named project-scoped types), a missing one is a plain 422 naming the mapping and the
allowed types, and a failed lookup falls back to the old by-name create. Jira mocked throughout."""
import json

from fastapi import HTTPException

import server

FRAZ_TYPES = [{"id": "10000", "name": "Epic"}, {"id": "10001", "name": "Story"},
              {"id": "10002", "name": "Task"}, {"id": "10017", "name": "Feature Request", "subtask": True}]


def _setup(monkeypatch, types=FRAZ_TYPES, meta_fails=False):
    calls = []
    monkeypatch.setattr(server, "_JIRA_ACCOUNT_CACHE", {})
    monkeypatch.setattr(server, "jira_configured", lambda: True)

    def fake(method, path, payload=None, *a, **k):
        calls.append((method, path, json.loads(json.dumps(payload)) if payload else None))
        if "/issue/createmeta/" in path:
            if meta_fails:
                raise HTTPException(503, "Jira down")
            key = path.split("/createmeta/")[1].split("/")[0]
            return {"issueTypes": types if key == "FRAZ" else [{"id": "1", "name": "Task"}]}
        if path == "/rest/api/3/issue":
            return {"key": "FRAZ-1"}
        return {}
    monkeypatch.setattr(server, "_jira_req", fake)
    return calls


def _item(team, **f):
    with server.db(team) as c:
        return server._insert_project(c, {"name": "Thing", "status": "New", **f})


def _create(client, h, pid, issue_type):
    return client.post("/api/jira/create-issue", headers=h,
                       json={"item_id": pid, "item_name": "Thing", "project_key": "FRAZ", "issue_type": issue_type})


def _issue_posts(calls):
    return [p for (m, path, p) in calls if m == "POST" and path == "/rest/api/3/issue"]


def test_allowed_type_is_created_by_id(client, team, admin_headers, monkeypatch):
    calls = _setup(monkeypatch)
    r = _create(client, admin_headers, _item(team, type="Enhancement"), "Story")
    assert r.status_code == 200, r.text
    p = _issue_posts(calls)[-1]["fields"]
    assert p["issuetype"] == {"id": "10001"} and "parent" not in p


def test_match_ignores_case_and_whitespace(client, team, admin_headers, monkeypatch):
    calls = _setup(monkeypatch)
    r = _create(client, admin_headers, _item(team), " task ")
    assert r.status_code == 200, r.text
    assert _issue_posts(calls)[-1]["fields"]["issuetype"] == {"id": "10002"}


def test_type_the_project_lacks_is_a_plain_422_and_nothing_is_created(client, team, admin_headers, monkeypatch):
    calls = _setup(monkeypatch)
    pid = _item(team, type="Enhancement")
    r = _create(client, admin_headers, pid, "Improvement")
    assert r.status_code == 422
    d = r.json()["detail"]
    assert 'Flow type "Enhancement" maps to Jira type "Improvement"' in d
    assert "FRAZ allows: Epic, Story, Task, Feature Request" in d
    assert "Admin > Jira > Type mapping" in d
    assert _issue_posts(calls) == []
    with server.db(team) as c:
        assert json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()[0]).get("jiraTickets") in (None, [])


def test_failed_lookup_falls_back_to_create_by_name(client, team, admin_headers, monkeypatch):
    calls = _setup(monkeypatch, meta_fails=True)
    r = _create(client, admin_headers, _item(team), "Story")
    assert r.status_code == 200, r.text
    assert _issue_posts(calls)[-1]["fields"]["issuetype"] == {"name": "Story"}


def test_project_issuetypes_endpoint(client, team, admin_headers, editor_headers, monkeypatch):
    _setup(monkeypatch)
    r = client.get("/api/jira/project-issuetypes?keys=fraz, HS ,FRAZ", headers=admin_headers)
    assert r.status_code == 200
    assert r.json() == {"FRAZ": [{"name": "Epic", "subtask": False}, {"name": "Story", "subtask": False},
                                 {"name": "Task", "subtask": False}, {"name": "Feature Request", "subtask": True}],
                        "HS": [{"name": "Task", "subtask": False}]}
    assert client.get("/api/jira/project-issuetypes?keys=FRAZ", headers=editor_headers).status_code == 403


def test_project_issuetypes_endpoint_omits_a_failed_key(client, team, admin_headers, monkeypatch):
    _setup(monkeypatch, meta_fails=True)
    r = client.get("/api/jira/project-issuetypes?keys=FRAZ", headers=admin_headers)
    assert r.status_code == 200 and r.json() == {}


# ── JIRA-SUBTASK-1 ─────────────────────────────────────────────────────────────────────────────────────
# FRAZ's "Feature Request" is a sub-task-level type: allowed, but Jira only creates it under a parent.

def test_subtask_type_is_created_under_the_parent_items_jira_issue(client, team, admin_headers, monkeypatch):
    calls = _setup(monkeypatch)
    par = _item(team, name="Epic thing", jiraTickets=["HS-9", "FRAZ-500"])   # the same-project key is used
    r = _create(client, admin_headers, _item(team, type="Feature", parent=par), "Feature Request")
    assert r.status_code == 200, r.text
    p = _issue_posts(calls)[-1]["fields"]
    assert p["issuetype"] == {"id": "10017"} and p["parent"] == {"key": "FRAZ-500"}


def test_subtask_type_without_a_jira_parent_is_a_plain_422(client, team, admin_headers, monkeypatch):
    calls = _setup(monkeypatch)
    par = _item(team, name="Unlinked parent")                              # parent has no Jira issue
    for pid in (_item(team, type="Enhancement"), _item(team, type="Enhancement", parent=par)):
        r = _create(client, admin_headers, pid, "Feature Request")
        assert r.status_code == 422
        d = r.json()["detail"]
        assert 'Flow type "Enhancement" maps to Jira type "Feature Request", which is a sub-task type in FRAZ' in d
        assert "map it to a standard type (Epic, Story, Task)" in d
    assert _issue_posts(calls) == []


def test_reverse_type_map_prefers_the_same_named_flow_type():
    fwd = {"Feature": "Feature Request", "Enhancement": "Story", "Bug Fix": "Bug", "Maintenance": "Bug",
           "Task": " Task ", "Story": "Story", "Support": ""}
    rev = server._jira_type_reverse(fwd)
    assert rev["Story"] == "Story"                 # Enhancement also maps to Story - a pulled Story stays Story
    assert rev["Task"] == "Task"                   # whitespace on the Jira value is stripped
    assert rev["Bug"] == "Bug Fix"                 # no same-named Flow type -> the first mapping, as before
    assert rev["Feature Request"] == "Feature"
    assert "" not in rev


def test_pull_context_and_child_sync_use_the_same_reverse_map(team):
    fwd = {"Enhancement": "Story", "Story": "Story", "Feature": "Feature Request"}
    with server.db(team) as c:
        c.execute("INSERT OR REPLACE INTO config(key,value) VALUES('jiraTypeMapping',?)", (json.dumps(fwd),))
    assert server._pull_context(team)["type_rev"]["Story"] == "Story"
    assert server._jira_type_to_roadmap("Story", team) == "Story"
    assert server._jira_type_to_roadmap(" Feature Request ", team) == "Feature"
