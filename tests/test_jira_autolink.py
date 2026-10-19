"""JIRA-AUTOLINK-1: Jira issues created by hand for an existing Flow request no longer become copies.

1. The pull links a new Jira issue to the ONE open Flow item its summary/description names ("FRZ - 889",
   "FRZ-889", a Flow item URL) instead of creating a second item; never re-imports it after an unlink.
2. Linking a key that the pull already copied into an UNTOUCHED item moves the key to the original and
   marks the copy a Duplicate of it (reversible); a copy someone has worked on is never merged (409).
Jira is mocked throughout; no network.
"""
import json

import server

CTX = {"type_rev": {"Story": "Story"}, "status_eff": {"New": "New"}, "overlay": {},
       "proj_rev": {"FRAZ": "Fraznet"}, "pods": [], "assignee_map": {}, "released": set(),
       "terminal": {"Released": True}}


def _iss(key, summary, description=None):
    f = {"summary": summary, "issuetype": {"name": "Story"}, "status": {"name": "New"}, "project": {"key": "FRAZ"}}
    if description is not None:
        f["description"] = description
    return {"key": key, "fields": f}


def _adf(text):
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def _mock_plan(monkeypatch, issues):
    monkeypatch.setattr(server, "_pull_plan_issues", lambda team: ({"ready": True}, list(issues)))
    monkeypatch.setattr(server, "_pull_context", lambda team: CTX)
    monkeypatch.setattr(server, "_link_pull_parents", lambda *a, **k: None)


def _insert(team, item_key=None, **fields):
    item = {"name": "Item", "product": "Fraznet", "type": "Story", "status": "New", **fields}
    with server.db(team) as c:
        pid = server._insert_project(c, item)
        if item_key:
            c.execute("UPDATE projects SET data=json_set(data,'$.itemKey',?) WHERE id=?", (item_key, pid))
    return pid


def _blob(team, pid):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])


def _count(team):
    with server.db(team) as c:
        return c.execute("SELECT COUNT(*) FROM projects").fetchone()[0]


def _auth(team):
    return {"team": team, "username": "admin"}


# ── 1. the pull links instead of copying ──────────────────────────────────────────────────────────────
def test_reference_in_summary_links_to_the_existing_item(team, monkeypatch):
    orig = _insert(team, "FRZ-889", name="Un-verify button")
    _mock_plan(monkeypatch, [_iss("FRAZ-11344", "Revert Verification for Logistics Jobs - FRZ - 889")])
    n = _count(team)
    r = server.jira_pull(body={}, auth=_auth(team))
    assert r["createdCount"] == 0 and r["autoLinked"] == [{"key": "FRAZ-11344", "linkedTo": "FRZ-889", "id": orig}]
    assert _count(team) == n and _blob(team, orig)["jiraTickets"] == ["FRAZ-11344"]
    with server.db(team) as c:
        a = c.execute("SELECT changes FROM audit_log WHERE action='jira:pull-autolink'").fetchone()
    assert json.loads(a["changes"]) == {"jiraKey": "FRAZ-11344", "reference": "FRZ-889"}


def test_reference_in_description_or_url_links(team, monkeypatch):
    orig = _insert(team, "FRZ-12")
    _mock_plan(monkeypatch, [_iss("FRAZ-1", "Some work", _adf("See https://flow.frazil.app/item/9?team=development&key=FRZ-12"))])
    r = server.jira_pull(body={}, auth=_auth(team))
    assert r["autoLinked"][0]["id"] == orig


def test_no_link_when_target_finished_ambiguous_or_unknown(team, monkeypatch):
    _insert(team, "FRZ-1", status="Released")                      # finished -> not a target
    _insert(team, "FRZ-2"); _insert(team, "FRZ-3")
    _mock_plan(monkeypatch, [_iss("FRAZ-10", "follow-up to FRZ-1"),
                             _iss("FRAZ-11", "covers FRZ-2 and FRZ-3"),   # two distinct refs -> ambiguous
                             _iss("FRAZ-12", "about FRZ-999")])           # no such item
    r = server.jira_pull(body={}, auth=_auth(team))
    assert r["autoLinked"] == [] and r["createdCount"] == 3


def test_jira_key_itself_is_not_mistaken_for_a_flow_reference(team, monkeypatch):
    _insert(team, "FRZ-5")
    _mock_plan(monkeypatch, [_iss("FRAZ-5", "FRAZ-5 duplicate of nothing")])
    r = server.jira_pull(body={}, auth=_auth(team))
    assert r["autoLinked"] == [] and r["createdCount"] == 1


def test_autolinked_key_is_never_reimported_after_unlink(team, monkeypatch):
    orig = _insert(team, "FRZ-7")
    _mock_plan(monkeypatch, [_iss("FRAZ-70", "Work for FRZ-7")])
    server.jira_pull(body={}, auth=_auth(team))
    with server.db(team) as c:
        c.execute("UPDATE projects SET data=json_set(data,'$.jiraTickets',json('[]')) WHERE id=?", (orig,))
    n = _count(team)
    r = server.jira_pull(body={}, auth=_auth(team))
    assert r["createdCount"] == 0 and _count(team) == n


def test_dry_run_reports_would_autolink(team, monkeypatch):
    orig = _insert(team, "FRZ-8")
    _mock_plan(monkeypatch, [_iss("FRAZ-80", "FRZ-8 follow-up"), _iss("FRAZ-81", "unrelated")])
    r = server.jira_pull(body={"dryRun": True}, auth=_auth(team))
    assert r["wouldAutoLink"] == [{"key": "FRAZ-80", "linkTo": "FRZ-8", "id": orig}] and r["wouldCreate"] == 1
    assert _blob(team, orig).get("jiraTickets") in (None, [])


# ── 2. linking merges an untouched pulled copy ────────────────────────────────────────────────────────
def _jira_up(monkeypatch):
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_jira_req", lambda method, path, *a, **k:
                        {"fields": {"summary": "S", "status": {"name": "To Do"}, "issuetype": {"name": "Story"},
                                    "project": {"name": "FRAZ"}}} if method == "GET" else {})


def _pulled_copy(team, key):
    pid = _insert(team, "FRZ-500", name="Copy", jiraSource="pull", jiraTickets=[key])
    server.write_audit(team, "jira:pull-create", "admin", pid, "Copy", changes={"jiraKey": key})
    return pid


def test_link_merges_an_untouched_pulled_copy(client, team, admin_headers, monkeypatch):
    _jira_up(monkeypatch)
    orig = _insert(team, "FRZ-889", name="Original")
    copy = _pulled_copy(team, "FRAZ-11344")
    r = client.post("/api/jira/link-issue", json={"ticket": "FRAZ-11344", "item_id": orig}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["mergedCopy"] == {"id": copy, "itemKey": "FRZ-500"}
    o, cp = _blob(team, orig), _blob(team, copy)
    assert o["jiraTickets"] == ["FRAZ-11344"]
    assert cp["jiraTickets"] == [] and cp["status"] == server.DUPLICATE_STATUS
    assert server._find_dup_ref(cp, "duplicate-of")["targetId"] == orig   # standard, reversible mark


def test_link_refuses_to_merge_a_copy_someone_edited(client, team, admin_headers, monkeypatch):
    _jira_up(monkeypatch)
    orig = _insert(team, "FRZ-889")
    copy = _pulled_copy(team, "FRAZ-2")
    server.write_audit(team, "update", "gopika", copy, "Copy", changes={"status": {"from": "New", "to": "Inactive"}})
    r = client.post("/api/jira/link-issue", json={"ticket": "FRAZ-2", "item_id": orig}, headers=admin_headers)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "duplicate_link"
    assert _blob(team, copy)["jiraTickets"] == ["FRAZ-2"] and _blob(team, copy)["status"] == "New"


def test_link_refuses_to_merge_a_copy_with_comments_or_children(client, team, admin_headers, monkeypatch):
    _jira_up(monkeypatch)
    orig = _insert(team, "FRZ-889")
    copy = _pulled_copy(team, "FRAZ-3")
    _insert(team, "FRZ-501", parent=copy)
    r = client.post("/api/jira/link-issue", json={"ticket": "FRAZ-3", "item_id": orig}, headers=admin_headers)
    assert r.status_code == 409


def test_link_still_409s_for_a_flow_created_holder(client, team, admin_headers, monkeypatch):
    _jira_up(monkeypatch)
    orig = _insert(team, "FRZ-889")
    _insert(team, "FRZ-600", jiraTickets=["FRAZ-4"])               # not a pulled copy
    r = client.post("/api/jira/link-issue", json={"ticket": "FRAZ-4", "item_id": orig}, headers=admin_headers)
    assert r.status_code == 409
