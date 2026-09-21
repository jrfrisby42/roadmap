"""JIRA-PULL-1 Stage 5 - the refresh pass + triggers.

The refresh keeps status/pod/assignee current on items the pull CREATED (jiraSource == 'pull') and
NOTHING ELSE. The live end-to-end proofs (a real pod added in Jira reaching a real pulled item, etc.)
run in the separate acceptance prompt against development. These cover what is deterministic in-process:

  - _refresh_one_pulled (pure): forward-only status; matched pod applies while an absent/ambiguous label
    keeps Flow's last value; assignee only from the map; unassigned/unmapped keeps last.
  - _jira_refresh_pulled (engine, Jira mocked): THE CRITICAL INVARIANT - a non-jiraSource item sharing the
    same Jira key is never touched; a search failure aborts with zero writes; a per-item failure skips one
    and keeps the rest; a refresh generates no notifications and no activities.
  - endpoint gating + the pull-now combined report.
"""
import json
import pytest
import server


# ── config + item helpers ─────────────────────────────────────────────────────────────────────────────
STATUSES = ["New", "In Progress", "In Review", "Done"]          # ordered -> ranks 0..3

CFG = {
    "statuses": STATUSES,
    "jiraEnabled": True,
    "jiraStatusMapping": {"New": "NEW", "In Progress": "In Dev", "Done": "Done"},
    "jiraPullStatusMap": {"Code Review": "In Review"},           # overlay: a pull-only Jira status
    "jiraProjectMapping": {"Fraznet": "FRAZ"},
    "jiraTypeMapping": {"Story": "Story"},
    "developers": ["Everest", "Wasatch"],
    "jiraAssigneeMap": {"acc-1": "jane.doe", "acc-2": "john.roe"},
}


def _set_cfg(team, key, val):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(val)))


def _seed_cfg(team):
    for k, v in CFG.items():
        _set_cfg(team, k, v)


def _insert(team, **fields):
    item = {"name": fields.get("name", "Item"), "product": "Fraznet", "type": "Story",
            "status": "New", "dev": "", "assignee": "", **fields}
    with server.db(team) as c:
        server._assign_item_key(c, item)
        item["id"] = server._insert_project(c, item)
    return item["id"]


def _blob(team, pid):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])


def _iss(key, status, labels=None, acct=None):
    f = {"status": {"name": status}, "issuetype": {"name": "Story"}, "project": {"key": "FRAZ"}}
    if labels is not None: f["labels"] = labels
    if acct: f["assignee"] = {"accountId": acct}
    return {"key": key, "fields": f}


CTX = {  # a _pull_context()-shaped dict for the pure tests
    "status_eff": {"NEW": "New", "In Dev": "In Progress", "Done": "Done", "Code Review": "In Review"},
    "pods": ["Everest", "Wasatch"],
    "assignee_map": {"acc-1": "jane.doe", "acc-2": "john.roe"},
}


# ── _refresh_one_pulled: the field rules, pure ──────────────────────────────────────────────────────────
def test_status_forward_only_advances():
    cur = {"status": "New", "dev": "", "assignee": ""}
    chg = server._refresh_one_pulled(cur, _iss("FRAZ-1", "Code Review"), CTX, STATUSES)   # overlay -> In Review
    assert cur["status"] == "In Review" and chg["status"] == ("New", "In Review")


def test_status_forward_only_never_regresses():
    cur = {"status": "In Progress", "dev": "", "assignee": ""}
    chg = server._refresh_one_pulled(cur, _iss("FRAZ-1", "NEW"), CTX, STATUSES)           # NEW -> New, lower rank
    assert cur["status"] == "In Progress" and "status" not in chg                         # kept, not regressed


def test_status_unmapped_left_alone():
    cur = {"status": "New", "dev": "", "assignee": ""}
    chg = server._refresh_one_pulled(cur, _iss("FRAZ-1", "Totally Unknown"), CTX, STATUSES)
    assert cur["status"] == "New" and "status" not in chg


def test_pod_matched_applies():
    cur = {"status": "New", "dev": "", "assignee": ""}
    chg = server._refresh_one_pulled(cur, _iss("FRAZ-1", "NEW", labels=["Pod-Everest"]), CTX, STATUSES)
    assert cur["dev"] == "Everest" and chg["dev"] == ("", "Everest")


def test_pod_absent_keeps_last():
    # A pod label removed in Jira (no Pod- label) must NOT clear Flow's value - 3.2 "removal does not clear".
    cur = {"status": "New", "dev": "Wasatch", "assignee": ""}
    chg = server._refresh_one_pulled(cur, _iss("FRAZ-1", "NEW", labels=["misc"]), CTX, STATUSES)
    assert cur["dev"] == "Wasatch" and "dev" not in chg


def test_pod_not_in_flow_keeps_last():
    cur = {"status": "New", "dev": "Wasatch", "assignee": ""}
    chg = server._refresh_one_pulled(cur, _iss("FRAZ-1", "NEW", labels=["Pod-Nope"]), CTX, STATUSES)
    assert cur["dev"] == "Wasatch" and "dev" not in chg


def test_pod_ambiguous_keeps_last():
    cur = {"status": "New", "dev": "Wasatch", "assignee": ""}
    chg = server._refresh_one_pulled(cur, _iss("FRAZ-1", "NEW", labels=["Pod-Everest", "Pod-Wasatch"]),
                                     CTX, STATUSES)
    assert cur["dev"] == "Wasatch" and "dev" not in chg


def test_assignee_from_map_applies():
    cur = {"status": "New", "dev": "", "assignee": ""}
    chg = server._refresh_one_pulled(cur, _iss("FRAZ-1", "NEW", acct="acc-2"), CTX, STATUSES)
    assert cur["assignee"] == "john.roe" and chg["assignee"] == ("", "john.roe")


def test_assignee_unmapped_keeps_last():
    cur = {"status": "New", "dev": "", "assignee": "jane.doe"}
    chg = server._refresh_one_pulled(cur, _iss("FRAZ-1", "NEW", acct="acc-unknown"), CTX, STATUSES)
    assert cur["assignee"] == "jane.doe" and "assignee" not in chg


def test_assignee_unassigned_keeps_last():
    # No accountId in Jira (unassigned) is "absent", not "changed" - keep Flow's value.
    cur = {"status": "New", "dev": "", "assignee": "jane.doe"}
    chg = server._refresh_one_pulled(cur, _iss("FRAZ-1", "NEW"), CTX, STATUSES)
    assert cur["assignee"] == "jane.doe" and "assignee" not in chg


def test_no_change_returns_empty():
    cur = {"status": "In Review", "dev": "Everest", "assignee": "jane.doe"}
    chg = server._refresh_one_pulled(cur, _iss("FRAZ-1", "Code Review", labels=["Pod-Everest"], acct="acc-1"),
                                     CTX, STATUSES)
    assert chg == {}


# ── the engine: scope, the critical invariant, failure discipline, no side effects ─────────────────────
def _patch_search(monkeypatch, issues, raise_exc=None):
    def fake(jql, fields, cap=2000):
        if raise_exc:
            raise raise_exc
        return list(issues)
    monkeypatch.setattr(server, "_jira_search_all", fake)
    monkeypatch.setattr(server, "jira_configured", lambda: True)


def test_refresh_touches_only_pull_items_CRITICAL(team, monkeypatch):
    """THE invariant: a non-jiraSource item that carries the SAME Jira key as a pulled item is never
    touched. Scope is jiraSource, not the key match."""
    _seed_cfg(team)
    pulled = _insert(team, name="pulled", jiraSource="pull", jiraTickets=["FRAZ-1"],
                     status="New", dev="", assignee="")
    pushed = _insert(team, name="pushed-not-pulled", jiraTickets=["FRAZ-1"],   # SAME key, NO jiraSource
                     status="New", dev="Wasatch", assignee="john.roe")
    # FRAZ-1 in Jira: would set pod Everest / assignee jane.doe / status In Review.
    _patch_search(monkeypatch, [_iss("FRAZ-1", "Code Review", labels=["Pod-Everest"], acct="acc-1")])
    res = server._jira_refresh_pulled(team, "admin")
    assert res["scanned"] == 1 and res["changed"] == 1        # only the pulled item was in scope
    p = _blob(team, pulled)
    assert (p["status"], p["dev"], p["assignee"]) == ("In Review", "Everest", "jane.doe")
    # The pushed item did NOT take Jira's values, even though it shares the key.
    n = _blob(team, pushed)
    assert (n["status"], n["dev"], n["assignee"]) == ("New", "Wasatch", "john.roe")


def test_refresh_idempotent_rerun_changes_zero(team, monkeypatch):
    _seed_cfg(team)
    _insert(team, jiraSource="pull", jiraTickets=["FRAZ-1"], status="New", dev="", assignee="")
    _patch_search(monkeypatch, [_iss("FRAZ-1", "Code Review", labels=["Pod-Everest"], acct="acc-1")])
    first = server._jira_refresh_pulled(team, "admin")
    assert first["changed"] == 1
    second = server._jira_refresh_pulled(team, "admin")
    assert second["changed"] == 0                            # nothing left to advance - forward-only fixpoint


def test_search_failure_aborts_with_zero_writes(team, monkeypatch):
    _seed_cfg(team)
    pid = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-1"], status="New", dev="", assignee="")
    _patch_search(monkeypatch, [], raise_exc=RuntimeError("Jira 503"))
    res = server._jira_refresh_pulled(team, "admin")
    assert res.get("aborted") is True and res["changed"] == 0
    assert _blob(team, pid)["status"] == "New"               # untouched - no partial state


def test_per_item_failure_skips_and_continues(team, monkeypatch):
    _seed_cfg(team)
    p1 = _insert(team, name="one", jiraSource="pull", jiraTickets=["FRAZ-1"], status="New")
    p2 = _insert(team, name="two", jiraSource="pull", jiraTickets=["FRAZ-2"], status="New")
    _patch_search(monkeypatch, [_iss("FRAZ-1", "Code Review"), _iss("FRAZ-2", "Code Review")])
    real_save = server._save_project
    def boom(c, pid, data, ts=None):
        if pid == p1:
            raise RuntimeError("write failed for p1")
        return real_save(c, pid, data, ts)
    monkeypatch.setattr(server, "_save_project", boom)
    res = server._jira_refresh_pulled(team, "admin")
    assert res["errors"] == 1                                # p1 failed, was skipped
    assert res["changed"] == 1                               # p2 still advanced
    assert _blob(team, p1)["status"] == "New"                # p1 kept last-known
    assert _blob(team, p2)["status"] == "In Review"


def test_refresh_generates_no_notifications_or_activities(team, monkeypatch):
    _seed_cfg(team)
    pid = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-1"], status="New", dev="", assignee="")
    with server.db(team) as c:                               # a watcher who would normally be notified
        c.execute("INSERT INTO watchers(item_id,username) VALUES(?,?)", (pid, "someone"))
        n0 = c.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        a0 = c.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    _patch_search(monkeypatch, [_iss("FRAZ-1", "Code Review", labels=["Pod-Everest"], acct="acc-1")])
    res = server._jira_refresh_pulled(team, "admin")
    assert res["changed"] == 1                               # a real status/pod/assignee change happened
    with server.db(team) as c:
        n1 = c.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        a1 = c.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    assert n1 == n0 and a1 == a0                             # silent: no watcher mail, no activity spam


def test_refresh_no_pulled_items_is_a_noop(team, monkeypatch):
    _seed_cfg(team)
    _insert(team, jiraTickets=["FRAZ-1"], status="New")      # linked but NOT pulled
    _patch_search(monkeypatch, [_iss("FRAZ-1", "Code Review")])
    res = server._jira_refresh_pulled(team, "admin")
    assert res["scanned"] == 0 and res["changed"] == 0


# ── triggers: gating + the combined pull-now report ────────────────────────────────────────────────────
def test_refresh_endpoint_admin_gated(client, team, viewer_headers):
    assert client.post("/api/jira/refresh", headers=viewer_headers).status_code in (401, 403)


def test_pull_now_endpoint_admin_gated(client, team, viewer_headers):
    assert client.post("/api/jira/pull-now", json={}, headers=viewer_headers).status_code in (401, 403)


def test_pull_now_reports_both_counts(client, team, admin_headers, monkeypatch):
    # Floor not set -> create path is not ready (createdCount 0); refresh still runs and reports.
    _seed_cfg(team)
    _insert(team, jiraSource="pull", jiraTickets=["FRAZ-1"], status="New")
    _patch_search(monkeypatch, [_iss("FRAZ-1", "Code Review", labels=["Pod-Everest"])])
    r = client.post("/api/jira/pull-now", json={}, headers=admin_headers).json()
    assert "create" in r and "refresh" in r
    assert r["createdCount"] == 0                            # no floor -> create not ready
    assert r["refreshedCount"] == 1                          # refresh advanced the pulled item
