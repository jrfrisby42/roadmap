"""JIRA-COMPLETED-1: the Jira status writers stamp completedAt on a move INTO a terminal status.

jira_pull_sync, the pulled-item refresh and the recurrence child-release cascade all change status
without going through update_project, so Jira-finished items used to carry no completedAt and never aged
off the Gantt (GANTT-DECLUTTER-1 dates "finished" by releaseDate, else completedAt). Set-once, gated on a
transition, never on a reserved status. Jira mocked."""
import json
import server

STATUSES = ["New", "In Progress", "Released", "Done"]
CTX = {"status_eff": {"In Prod": "Released", "QA": "In Progress", "Closed": "Done"},
       "pods": [], "assignee_map": {}, "released": {"Released"},
       "terminal": {"Released": True, "Done": True}}


def _iss(status):
    return {"key": "FRAZ-1", "fields": {"status": {"name": status}, "issuetype": {"name": "Story"},
                                        "project": {"key": "FRAZ"}}}


# ── the shared helper ─────────────────────────────────────────────────────────────────────────────────
def test_helper_stamps_terminal_once():
    item = {"status": "Done"}
    assert server._stamp_completed_at(item, {"Done": True}, "2026-10-19T00:00:00+00:00") is True
    assert item["completedAt"] == "2026-10-19T00:00:00+00:00"
    assert server._stamp_completed_at(item, {"Done": True}, "2027-01-01T00:00:00+00:00") is False
    assert item["completedAt"] == "2026-10-19T00:00:00+00:00"          # first completion wins


def test_helper_ignores_non_terminal_and_reserved():
    item = {"status": "In Progress"}
    assert server._stamp_completed_at(item, {"Done": True}, "x") is False and "completedAt" not in item
    dup = {"status": server.DUPLICATE_STATUS}                           # reserved: not in team config
    assert server._stamp_completed_at(dup, {"Done": True}, "x") is False and "completedAt" not in dup


# ── refresh pass (pure) ───────────────────────────────────────────────────────────────────────────────
def test_refresh_stamps_on_transition_into_terminal():
    for jira, flow in (("In Prod", "Released"), ("Closed", "Done")):
        cur = {"status": "New"}
        chg = server._refresh_one_pulled(cur, _iss(jira), CTX, STATUSES)
        assert cur["status"] == flow and cur.get("completedAt")
        assert chg["completedAt"] == ("", cur["completedAt"])


def test_refresh_no_stamp_without_transition_or_into_non_terminal():
    cur = {"status": "Released"}                                         # already there
    chg = server._refresh_one_pulled(cur, _iss("In Prod"), CTX, STATUSES)
    assert "completedAt" not in chg and "completedAt" not in cur
    cur = {"status": "New"}
    chg = server._refresh_one_pulled(cur, _iss("QA"), CTX, STATUSES)
    assert cur["status"] == "In Progress" and "completedAt" not in cur


def test_refresh_keeps_existing_completed_at():
    cur = {"status": "New", "completedAt": "2026-01-01T00:00:00+00:00"}
    chg = server._refresh_one_pulled(cur, _iss("In Prod"), CTX, STATUSES)
    assert cur["completedAt"] == "2026-01-01T00:00:00+00:00" and "completedAt" not in chg


def test_refresh_ctx_without_terminal_is_a_no_op():
    ctx = {k: v for k, v in CTX.items() if k != "terminal"}
    cur = {"status": "New"}
    server._refresh_one_pulled(cur, _iss("In Prod"), ctx, STATUSES)
    assert "completedAt" not in cur


def test_pull_context_carries_terminal(team):
    with server.db(team) as c:
        c.execute("UPDATE config SET value=? WHERE key='statusIsTerminal'", (json.dumps({"Released": True, "Planned": False}),))
    assert server._pull_context(team)["terminal"] == {"Released": True}


# ── jira_pull_sync end to end ─────────────────────────────────────────────────────────────────────────
def _pull_setup(client, admin_headers, monkeypatch, jira_status):
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_fetch_jira_feature_flags", lambda ticket: set())
    monkeypatch.setattr(server, "_jira_req", lambda method, path, *a, **k: {"fields": {
        "status": {"name": jira_status, "statusCategory": {"key": "done"}}, "summary": "S",
        "issuetype": {"name": "Story"}, "project": {"key": "PROJ", "name": "P"}}})
    client.put("/api/config/statuses", json=["Planned", "In Progress", "In Testing", "Released"], headers=admin_headers)
    client.put("/api/config/statusIsTerminal", json={"Released": True}, headers=admin_headers)
    client.put("/api/config/jiraStatusMapping", json={"Released": "In Prod", "In Testing": "In QA"}, headers=admin_headers)
    pid = client.post("/api/projects", json={"name": "Item", "status": "Planned", "jiraTickets": ["PROJ-1"]},
                      headers=admin_headers).json()["id"]
    r = client.post(f"/api/jira/pull/{pid}", json={}, headers=admin_headers)
    assert r.status_code == 200, r.text
    allr = client.get("/api/all", headers=admin_headers).json()
    return next(p for p in allr["projects"] if p["id"] == pid)


def test_pull_sync_into_terminal_stamps_completed_at(client, admin_headers, monkeypatch):
    item = _pull_setup(client, admin_headers, monkeypatch, "In Prod")
    assert item["status"] == "Released" and item.get("completedAt")


def test_pull_sync_into_non_terminal_does_not_stamp(client, admin_headers, monkeypatch):
    item = _pull_setup(client, admin_headers, monkeypatch, "In QA")
    assert item["status"] == "In Testing" and not item.get("completedAt")
