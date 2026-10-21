"""JIRA-REPORTER-1: a Jira issue created from Flow names its reporter.

Reporter = the item's requester (portal email, or the Flow user who created the item) when they have a
Jira account, else the signed-in user who launched the create, else none (Jira records the integration
account, as before). The description opens with who created it from Flow and who asked. A project that
refuses the reporter field is retried without it. Jira mocked throughout."""
import json

from fastapi import HTTPException

import server
from urllib.parse import unquote

ACCOUNTS = {"amy@x.com": "acc-amy", "admin@x.com": "acc-admin"}


def _setup(team, monkeypatch, reject_reporter=False):
    calls = []
    monkeypatch.setattr(server, "_JIRA_ACCOUNT_CACHE", {})
    monkeypatch.setattr(server, "jira_configured", lambda: True)

    def fake(method, path, payload=None, *a, **k):
        calls.append((method, path, json.loads(json.dumps(payload)) if payload else None))
        if path.startswith("/rest/api/3/user/search"):
            email = unquote(path.split("query=")[1])
            acc = ACCOUNTS.get(email)
            return [{"accountId": acc, "emailAddress": email, "accountType": "atlassian"}] if acc else []
        if path == "/rest/api/3/issue":
            if reject_reporter and "reporter" in payload["fields"]:
                raise HTTPException(400, '{"errors":{"reporter":"Field \'reporter\' cannot be set."}}')
            return {"key": "FRAZ-1"}
        return {}
    monkeypatch.setattr(server, "_jira_req", fake)
    users = [{"username": "admin", "name": "Admin Person", "role": "admin", "email": "admin@x.com"},
             {"username": "amy", "name": "Amy Ames", "role": "editor", "email": "amy@x.com"},
             {"username": "bob", "name": "Bob Burns", "role": "editor", "email": "bob@x.com"}]
    with server.db(team) as c:
        c.execute("INSERT OR REPLACE INTO config(key,value) VALUES('users',?)", (json.dumps(users),))
    return calls


def _item(team, **f):
    with server.db(team) as c:
        return server._insert_project(c, {"name": "Thing", "status": "New", **f})


def _create(client, h, pid):
    r = client.post("/api/jira/create-issue", json={"item_id": pid, "item_name": "Thing", "description": "body",
                                                     "project_key": "FRAZ", "issue_type": "Story", "username": "spoofed"},
                    headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def _issue_posts(calls):
    return [p for (m, path, p) in calls if m == "POST" and path == "/rest/api/3/issue"]


def _desc(payload):
    return payload["fields"]["description"]["content"][0]["content"][0]["text"]


def test_flow_requester_with_jira_account_is_the_reporter(client, team, admin_headers, monkeypatch):
    calls = _setup(team, monkeypatch)
    _create(client, admin_headers, _item(team, reporter="amy"))
    p = _issue_posts(calls)[-1]
    assert p["fields"]["reporter"] == {"id": "acc-amy"}
    assert _desc(p) == "Created from Flow by Admin Person\nRequested by Amy Ames (amy@x.com)"


def test_requester_without_jira_account_falls_back_to_the_launcher(client, team, admin_headers, monkeypatch):
    calls = _setup(team, monkeypatch)
    _create(client, admin_headers, _item(team, reporter="Pat Outside", reporterEmail="pat@outside.com", source="portal"))
    p = _issue_posts(calls)[-1]
    assert p["fields"]["reporter"] == {"id": "acc-admin"}
    assert "Requested by Pat Outside (pat@outside.com)" in _desc(p)


def test_own_item_has_no_requested_by_line(client, team, admin_headers, monkeypatch):
    calls = _setup(team, monkeypatch)
    _create(client, admin_headers, _item(team, reporter="admin"))
    p = _issue_posts(calls)[-1]
    assert p["fields"]["reporter"] == {"id": "acc-admin"} and _desc(p) == "Created from Flow by Admin Person"


def test_nobody_in_jira_sends_no_reporter(client, team, editor_headers, monkeypatch):
    calls = _setup(team, monkeypatch)
    _create(client, editor_headers, _item(team, reporter="bob"))   # bob + editor1: no Jira accounts
    p = _issue_posts(calls)[-1]
    assert "reporter" not in p["fields"]


def test_refused_reporter_is_retried_without_it(client, team, admin_headers, monkeypatch):
    calls = _setup(team, monkeypatch, reject_reporter=True)
    out = _create(client, admin_headers, _item(team, reporter="amy"))
    posts = _issue_posts(calls)
    assert out["key"] == "FRAZ-1" and len(posts) == 2
    assert "reporter" in posts[0]["fields"] and "reporter" not in posts[1]["fields"]


def test_account_lookup_is_cached(client, team, admin_headers, monkeypatch):
    calls = _setup(team, monkeypatch)
    _create(client, admin_headers, _item(team, reporter="amy"))
    _create(client, admin_headers, _item(team, reporter="amy"))
    assert sum(1 for (m, path, _) in calls if "user/search" in path and "amy" in path) == 1
