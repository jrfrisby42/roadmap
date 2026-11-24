"""JIRA-CHILD-SUBTASK-1 (J.R. 2026-11-24: "make Flow always create a child item as a Jira Sub-task under its
parent's ticket").

"Create Jira issue" on a Flow child looks up its Parent Item's Jira issue (same Jira project) and:
- under a Story/Task/Bug (level 0): creates the project's Sub-task type under it;
- under a sub-task (a Flow grandchild): a Sub-task of THAT issue's parent (Jira can't nest sub-tasks);
- under an Epic or higher: keeps the mapped type and places it inside the issue, retrying loose if Jira
  refuses that parent;
- parent with no Jira issue in the project, or its type unknown: as before (mapped type, loose).
The Flow Type is untouched. Jira mocked throughout."""
import json
import pathlib

from fastapi import HTTPException

import server

FRAZ_TYPES = [{"id": "10000", "name": "Epic"}, {"id": "10001", "name": "Story"},
              {"id": "10002", "name": "Task"}, {"id": "10017", "name": "Feature Request", "subtask": True},
              {"id": "10003", "name": "Sub-task", "subtask": True}, {"id": "10050", "name": "Roadmap Item"}]

ISSUES = {
    "FRAZ-10": {"issuetype": {"name": "Story", "hierarchyLevel": 0, "subtask": False}},
    "FRAZ-11": {"issuetype": {"name": "Sub-task", "hierarchyLevel": -1, "subtask": True}, "parent": {"key": "FRAZ-10"}},
    "FRAZ-20": {"issuetype": {"name": "Epic", "hierarchyLevel": 1, "subtask": False}},
    "FRAZ-30": {"issuetype": {"name": "Roadmap Item", "hierarchyLevel": 2, "subtask": False}},
}


def _setup(monkeypatch, issue_get_fails=False):
    calls = []
    monkeypatch.setattr(server, "_JIRA_ACCOUNT_CACHE", {})
    monkeypatch.setattr(server, "jira_configured", lambda: True)

    def fake(method, path, payload=None, *a, **k):
        calls.append((method, path, json.loads(json.dumps(payload)) if payload else None))
        if "/issue/createmeta/" in path:
            return {"issueTypes": FRAZ_TYPES}
        if method == "GET" and path.startswith("/rest/api/3/issue/") and "fields=issuetype,parent" in path:
            if issue_get_fails:
                raise HTTPException(503, "Jira down")
            return {"fields": ISSUES.get(path.split("/issue/")[1].split("?")[0], {})}
        if method == "POST" and path == "/rest/api/3/issue":
            f = payload["fields"]
            # Jira: only an Epic can sit under a Roadmap Item
            if (f.get("parent") or {}).get("key") == "FRAZ-30" and f["issuetype"] != {"id": "10000"}:
                raise HTTPException(400, "Given parent work item does not belong to appropriate hierarchy.")
            return {"key": "FRAZ-99"}
        return {}
    monkeypatch.setattr(server, "_jira_req", fake)
    return calls


def _item(team, **f):
    with server.db(team) as c:
        return server._insert_project(c, {"name": "Thing", "status": "New", **f})


def _create(client, h, pid, issue_type="Story"):
    return client.post("/api/jira/create-issue", headers=h,
                       json={"item_id": pid, "item_name": "Thing", "project_key": "FRAZ", "issue_type": issue_type})


def _posts(calls):
    return [p["fields"] for (m, path, p) in calls if m == "POST" and path == "/rest/api/3/issue"]


def _parent_gets(calls):
    return [path for (m, path, p) in calls if m == "GET" and "fields=issuetype,parent" in path]


def test_child_of_a_story_is_a_subtask_under_it(client, team, admin_headers, monkeypatch):
    calls = _setup(monkeypatch)
    par = _item(team, name="Parent", type="Enhancement", jiraTickets=["HS-1", "FRAZ-10"])
    kid = _item(team, type="Task", parent=par)
    r = _create(client, admin_headers, kid, "Task")
    assert r.status_code == 200, r.text
    f = _posts(calls)[-1]
    assert f["issuetype"] == {"id": "10003"} and f["parent"] == {"key": "FRAZ-10"}
    j = r.json()
    assert j["issueType"] == "Sub-task" and j["jiraParent"] == "FRAZ-10"
    assert j["placed"] == "as a Sub-task of FRAZ-10" and j["loose"] == ""
    with server.db(team) as c:
        p = json.loads(c.execute("SELECT data FROM projects WHERE id=?", (kid,)).fetchone()["data"])
        a = c.execute("SELECT changes FROM audit_log WHERE action='jira:create' AND project_id=?", (kid,)).fetchone()
    assert p["type"] == "Task" and p["jiraTickets"] == ["FRAZ-99"]      # the Flow Type is untouched
    assert json.loads(a["changes"])["jiraParent"] == "FRAZ-10"


def test_grandchild_is_flattened_to_a_subtask_of_the_story(client, team, admin_headers, monkeypatch):
    calls = _setup(monkeypatch)
    par = _item(team, name="Sub", jiraTickets=["FRAZ-11"])              # FRAZ-11 is itself a sub-task
    r = _create(client, admin_headers, _item(team, type="Task", parent=par), "Task")
    assert r.status_code == 200, r.text
    f = _posts(calls)[-1]
    assert f["issuetype"] == {"id": "10003"} and f["parent"] == {"key": "FRAZ-10"}
    assert "FRAZ-11 is itself a sub-task" in r.json()["placed"]


def test_child_of_an_epic_keeps_its_type_and_goes_inside_the_epic(client, team, admin_headers, monkeypatch):
    calls = _setup(monkeypatch)
    par = _item(team, name="Epic", jiraTickets=["FRAZ-20"])
    r = _create(client, admin_headers, _item(team, type="Enhancement", parent=par), "Story")
    assert r.status_code == 200, r.text
    f = _posts(calls)[-1]
    assert f["issuetype"] == {"id": "10001"} and f["parent"] == {"key": "FRAZ-20"}
    assert r.json()["placed"] == "in FRAZ-20"


def test_roadmap_item_parent_refused_by_jira_creates_it_loose(client, team, admin_headers, monkeypatch):
    calls = _setup(monkeypatch)
    par = _item(team, name="Roadmap", jiraTickets=["FRAZ-30"])
    r = _create(client, admin_headers, _item(team, type="Enhancement", parent=par), "Story")
    assert r.status_code == 200, r.text
    posts = _posts(calls)
    assert len(posts) == 2 and posts[0]["parent"] == {"key": "FRAZ-30"} and "parent" not in posts[1]
    j = r.json()
    assert j["jiraParent"] == "" and j["placed"] == "" and j["loose"] == "Jira would not place it in FRAZ-30"


def test_epic_under_a_roadmap_item_is_placed(client, team, admin_headers, monkeypatch):
    calls = _setup(monkeypatch)
    par = _item(team, name="Roadmap", jiraTickets=["FRAZ-30"])
    r = _create(client, admin_headers, _item(team, type="Epic", parent=par), "Epic")
    assert r.status_code == 200, r.text
    posts = _posts(calls)
    assert len(posts) == 1 and posts[0]["parent"] == {"key": "FRAZ-30"}


def test_parent_without_a_jira_issue_in_the_project_is_as_before(client, team, admin_headers, monkeypatch):
    calls = _setup(monkeypatch)
    par = _item(team, name="Parent", jiraTickets=["HS-1"])
    r = _create(client, admin_headers, _item(team, type="Task", parent=par), "Task")
    assert r.status_code == 200, r.text
    f = _posts(calls)[-1]
    assert f["issuetype"] == {"id": "10002"} and "parent" not in f
    assert _parent_gets(calls) == []
    assert r.json()["loose"] == "its parent has no FRAZ Jira issue"


def test_no_parent_is_unchanged(client, team, admin_headers, monkeypatch):
    calls = _setup(monkeypatch)
    r = _create(client, admin_headers, _item(team, type="Task"), "Task")
    assert r.status_code == 200, r.text
    f = _posts(calls)[-1]
    assert f["issuetype"] == {"id": "10002"} and "parent" not in f
    assert _parent_gets(calls) == [] and r.json()["loose"] == "" and r.json()["placed"] == ""


def test_failed_parent_lookup_creates_as_before(client, team, admin_headers, monkeypatch):
    calls = _setup(monkeypatch, issue_get_fails=True)
    par = _item(team, name="Parent", jiraTickets=["FRAZ-10"])
    r = _create(client, admin_headers, _item(team, type="Task", parent=par), "Task")
    assert r.status_code == 200, r.text
    f = _posts(calls)[-1]
    assert f["issuetype"] == {"id": "10002"} and "parent" not in f


def test_a_mapped_subtask_type_is_kept(client, team, admin_headers, monkeypatch):
    calls = _setup(monkeypatch)
    par = _item(team, name="Parent", jiraTickets=["FRAZ-10"])
    r = _create(client, admin_headers, _item(team, type="Feature", parent=par), "Feature Request")
    assert r.status_code == 200, r.text
    f = _posts(calls)[-1]
    assert f["issuetype"] == {"id": "10017"} and f["parent"] == {"key": "FRAZ-10"}


def test_subtask_type_prefers_the_one_named_sub_task():
    assert server._subtask_issuetype(FRAZ_TYPES)["name"] == "Sub-task"
    assert server._subtask_issuetype([{"id": "1", "name": "Feature Request", "subtask": True}])["name"] == "Feature Request"
    assert server._subtask_issuetype([{"id": "1", "name": "Story"}]) is None


HTML = (pathlib.Path(__file__).resolve().parent.parent / "roadmap.html").read_text(encoding="utf-8", errors="replace")


def test_client_says_where_it_went():
    assert "function _jiraCreateHint(p){" in HTML
    assert "showToast(`Jira issue ${result.key} created${_where} and linked${_why}`);" in HTML
    assert 'onclick="createJiraFromItemPage(this)" title="${escA(esc(_jiraCreateHint(p)))}"' in HTML
    assert HTML.count(".title = _jiraCreateHint(") == 2                         # modal button, both show sites
