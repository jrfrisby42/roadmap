"""JIRA-SPRINT-1 Stage 3 - sprint membership: set item sprintId from Jira. Membership resolves the
list-valued Sprint field to the single mirrored non-closed sprint; writes pulled AND the pushed items
(the stated exception, counted + audited); removal keeps last. Jira mocked."""
import json
import pytest
import server

MIR = {"id": "jira-1298", "name": "Sept 21 - Oct 02", "state": "Active", "jiraSource": "jira", "jiraSprintId": 1298}
MIR2 = {"id": "jira-1301", "name": "Next", "state": "Planned", "jiraSource": "jira", "jiraSprintId": 1301}


def _set_cfg(team, key, val):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(val)))


def _insert(team, **fields):
    item = {"name": fields.get("name", "I"), "status": "New", **fields}
    with server.db(team) as c:
        server._assign_item_key(c, item); item["id"] = server._insert_project(c, item)
    return item["id"]


def _blob(team, pid):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])


def _iss(key, sprint_ids):
    return {"key": key, "fields": {"customfield_10010": [{"id": i, "state": "active"} for i in sprint_ids]}}


def _arm(team, monkeypatch, issues, sprints=(MIR,)):
    _set_cfg(team, "jiraEnabled", True)
    _set_cfg(team, "statuses", ["New"])
    _set_cfg(team, "sprints", list(sprints))
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_jira_search_all", lambda jql, fields, cap=2000: list(issues))


# ── pure resolver ───────────────────────────────────────────────────────────────────────────────────────
def test_resolver_one_multi_none():
    nc = {1298: "jira-1298", 1301: "jira-1301"}
    assert server._item_mirrored_membership([{"id": 1298}], nc) == ("jira-1298", "one")
    assert server._item_mirrored_membership([{"id": 1298}, {"id": 1301}], nc) == (None, "multi")
    assert server._item_mirrored_membership([{"id": 999}], nc) == (None, "none")   # unmirrored sprint ignored
    assert server._item_mirrored_membership([], nc) == (None, "none")
    assert server._item_mirrored_membership(None, nc) == (None, "none")


# ── the pass: pulled + pushed, with the stated exception counted ────────────────────────────────────────
def test_sets_membership_pulled_and_pushed(team, monkeypatch):
    pulled = _insert(team, name="pulled", jiraSource="pull", jiraTickets=["FRAZ-1"])
    pushed = _insert(team, name="pushed", jiraTickets=["FRAZ-2"])               # no jiraSource = a "566"
    _arm(team, monkeypatch, [_iss("FRAZ-1", [1298]), _iss("FRAZ-2", [1298])])
    r = server._sync_sprint_membership(team, "admin")
    assert r["changed"] == 2 and r["pulledWrites"] == 1 and r["pushedWrites"] == 1
    assert _blob(team, pulled)["sprintId"] == "jira-1298"
    assert _blob(team, pushed)["sprintId"] == "jira-1298"


def test_pushed_write_is_audited_as_exception(team, monkeypatch):
    pushed = _insert(team, name="pushed", jiraTickets=["FRAZ-2"])
    _arm(team, monkeypatch, [_iss("FRAZ-2", [1298])])
    server._sync_sprint_membership(team, "admin")
    with server.db(team) as c:
        rows = c.execute("SELECT changes FROM audit_log WHERE action='jira:sprint-membership'").fetchall()
    assert rows and any(json.loads(r["changes"]).get("pushedException") is True for r in rows)


def test_multi_mirrored_left_unset(team, monkeypatch):
    pid = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-1"])
    _arm(team, monkeypatch, [_iss("FRAZ-1", [1298, 1301])], sprints=(MIR, MIR2))
    r = server._sync_sprint_membership(team, "admin")
    assert r["multiMirrored"] == 1 and r["changed"] == 0
    assert not _blob(team, pid).get("sprintId")


def test_none_keeps_last_value(team, monkeypatch):
    # item already has a sprintId pointing at a Flow sprint; its Jira list has only an UNMIRRORED sprint
    pid = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-1"], sprintId="flow-old")
    _arm(team, monkeypatch, [_iss("FRAZ-1", [999])])
    r = server._sync_sprint_membership(team, "admin")
    assert r["changed"] == 0
    assert _blob(team, pid)["sprintId"] == "flow-old"                          # kept last (3.4 removal)


def test_idempotent_rerun(team, monkeypatch):
    _insert(team, jiraSource="pull", jiraTickets=["FRAZ-1"])
    _arm(team, monkeypatch, [_iss("FRAZ-1", [1298])])
    assert server._sync_sprint_membership(team, "admin")["changed"] == 1
    assert server._sync_sprint_membership(team, "admin")["changed"] == 0


def test_abort_on_search_failure_zero_writes(team, monkeypatch):
    pid = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-1"])
    _set_cfg(team, "jiraEnabled", True); _set_cfg(team, "statuses", ["New"]); _set_cfg(team, "sprints", [MIR])
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    def boom(jql, fields, cap=2000):
        raise RuntimeError("Jira 503")
    monkeypatch.setattr(server, "_jira_search_all", boom)
    r = server._sync_sprint_membership(team, "admin")
    assert r.get("aborted") is True and r["changed"] == 0
    assert not _blob(team, pid).get("sprintId")


def test_inert_when_no_mirrored_nonclosed_sprint(team, monkeypatch):
    pid = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-1"])
    # only a Completed mirrored sprint -> not non-closed -> nothing to set
    _arm(team, monkeypatch, [_iss("FRAZ-1", [1298])],
         sprints=({"id": "jira-1298", "jiraSource": "jira", "jiraSprintId": 1298, "state": "Completed", "name": "X"},))
    r = server._sync_sprint_membership(team, "admin")
    assert r["nonClosedMirrored"] == 0 and r["changed"] == 0


def test_no_notifications_or_activities(team, monkeypatch):
    pid = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-1"])
    with server.db(team) as c:
        c.execute("INSERT INTO watchers(item_id,username) VALUES(?,?)", (pid, "someone"))
        n0 = c.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        a0 = c.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    _arm(team, monkeypatch, [_iss("FRAZ-1", [1298])])
    assert server._sync_sprint_membership(team, "admin")["changed"] == 1
    with server.db(team) as c:
        n1 = c.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        a1 = c.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    assert n1 == n0 and a1 == a0


def test_only_sprintid_field_changes(team, monkeypatch):
    pid = _insert(team, jiraSource="pull", jiraTickets=["FRAZ-1"], dev="Everest", assignee="jane")
    before = _blob(team, pid)
    _arm(team, monkeypatch, [_iss("FRAZ-1", [1298])])
    server._sync_sprint_membership(team, "admin")
    after = _blob(team, pid)
    assert after["sprintId"] == "jira-1298"
    assert {k: v for k, v in after.items() if k != "sprintId"} == {k: v for k, v in before.items() if k != "sprintId"}


def test_endpoint_admin_gated(client, team, viewer_headers):
    assert client.post("/api/jira/sync-sprint-membership", headers=viewer_headers).status_code in (401, 403)
