"""CLEANUP-1 Item 1 - stamp releaseDate when the refresh OBSERVES A TRANSITION into a released status.
On transition not state (already-Released items never stamped), set-once, keyed on statusIsReleased (not
Terminal, which also has Done), refresh-path only. Jira mocked."""
import json
from datetime import datetime, timezone
import server

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
STATUSES = ["New", "In Progress", "Released", "Done"]
CTX = {"status_eff": {"In Prod": "Released", "QA": "In Progress", "ShippedNothing": "Done"},
       "pods": [], "assignee_map": {}, "released": {"Released"}}


def _iss(status):
    return {"key": "FRAZ-1", "fields": {"status": {"name": status}, "issuetype": {"name": "Story"},
                                        "project": {"key": "FRAZ"}}}


# ── pure: the transition rule ───────────────────────────────────────────────────────────────────────────
def test_stamps_on_transition_into_released():
    cur = {"status": "New"}
    chg = server._refresh_one_pulled(cur, _iss("In Prod"), CTX, STATUSES, today=TODAY)
    assert cur["status"] == "Released" and cur["releaseDate"] == TODAY
    assert chg["releaseDate"] == ("", TODAY)


def test_no_stamp_when_already_released_no_transition():
    cur = {"status": "Released"}                          # already there -> no status change this run
    chg = server._refresh_one_pulled(cur, _iss("In Prod"), CTX, STATUSES, today=TODAY)
    assert "releaseDate" not in chg and "releaseDate" not in cur   # state-based stamping avoided


def test_no_stamp_on_transition_into_done():
    cur = {"status": "New"}
    chg = server._refresh_one_pulled(cur, _iss("ShippedNothing"), CTX, STATUSES, today=TODAY)
    assert cur["status"] == "Done" and "releaseDate" not in chg and "releaseDate" not in cur  # Done != Released


def test_set_once_never_overwrites():
    cur = {"status": "New", "releaseDate": "2026-01-01"}
    chg = server._refresh_one_pulled(cur, _iss("In Prod"), CTX, STATUSES, today=TODAY)
    assert cur["status"] == "Released" and cur["releaseDate"] == "2026-01-01"   # kept
    assert "releaseDate" not in chg


def test_no_stamp_without_today():
    cur = {"status": "New"}
    chg = server._refresh_one_pulled(cur, _iss("In Prod"), CTX, STATUSES)       # today defaults None
    assert cur["status"] == "Released" and "releaseDate" not in cur


# ── integration through the refresh ─────────────────────────────────────────────────────────────────────
def _set_cfg(team, key, val):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(val)))


def _insert(team, **f):
    item = {"name": "I", "status": "New", **f}
    with server.db(team) as c:
        server._assign_item_key(c, item); item["id"] = server._insert_project(c, item)
    return item["id"]


def _blob(team, pid):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])


def _arm(team, monkeypatch, jira_status):
    _set_cfg(team, "jiraEnabled", True)
    _set_cfg(team, "statuses", STATUSES)
    _set_cfg(team, "statusIsReleased", {"Released": True})
    _set_cfg(team, "statusIsTerminal", {"Released": True, "Done": True})
    _set_cfg(team, "jiraStatusMapping", {"Released": "In Prod", "In Progress": "QA"})   # -> status_eff reverse
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_jira_search_all",
                        lambda jql, fields, cap=2000: [{"key": "FRAZ-1", "fields": {"status": {"name": jira_status}}}])


def test_refresh_stamps_and_audits_then_set_once(team, monkeypatch):
    pid = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-1"], status="New")
    _arm(team, monkeypatch, "In Prod")
    r = server._jira_refresh_pulled(team, "admin")
    assert r["releaseStamped"] == 1 and r["statusChanged"] == 1
    b = _blob(team, pid)
    assert b["status"] == "Released" and b["releaseDate"] == TODAY
    with server.db(team) as c:
        aud = [json.loads(x["changes"]) for x in c.execute("SELECT changes FROM audit_log WHERE action='jira:pull-refresh'").fetchall()]
    assert any("releaseDate" in x.get("fields", {}) for x in aud)          # 1.4: visibly derived, audited
    # re-run: status already Released, releaseDate set -> no change, no overwrite
    r2 = server._jira_refresh_pulled(team, "admin")
    assert r2["changed"] == 0 and _blob(team, pid)["releaseDate"] == TODAY


def test_refresh_already_released_item_gets_nothing(team, monkeypatch):
    # an item pulled while already Released (no transition observed) is never stamped
    pid = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-1"], status="Released")
    _arm(team, monkeypatch, "In Prod")
    r = server._jira_refresh_pulled(team, "admin")
    assert r["releaseStamped"] == 0 and not _blob(team, pid).get("releaseDate")
