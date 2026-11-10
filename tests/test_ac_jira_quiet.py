"""AC-JIRA-QUIET-1: no System date/decision alerts on items pulled from Jira (Jira owns their dates and
status), plus the one-time clean-up of the ones already open. Person-raised alerts are untouched."""
import server


def _item(team, **f):
    with server.db(team) as c:
        return server._insert_project(c, {"name": "I", "status": "In Progress", **f})


def _rule(client, h, pid, kind="Needs Date Check", source="System"):
    return client.post("/api/activities", json={"activity_type": kind, "source": source, "item_id": pid,
                                                "item_name": "I", "message": "Due date has passed - confirm dates",
                                                "status": "Open"}, headers=h).json()


def _open(team, pid):
    with server.db(team) as c:
        return [dict(r) for r in c.execute("SELECT * FROM activities WHERE item_id=? AND status IN ('Open','Read')", (pid,))]


def test_rule_alert_on_a_pulled_item_is_refused(client, team, admin_headers):
    pulled = _item(team, jiraSource="pull", jiraTickets=["FRAZ-1"])
    own = _item(team, jiraTickets=["FRAZ-2"])                     # pushed from Flow: Flow owns its dates
    assert _rule(client, admin_headers, pulled).get("skipped") is True
    assert _open(team, pulled) == []
    for kind in ("Needs Date Check", "Needs Decision", "At Risk"):
        assert _rule(client, admin_headers, own, kind).get("id")
    assert len(_open(team, own)) == 3


def test_person_raised_alerts_on_pulled_items_still_work(client, team, admin_headers):
    pulled = _item(team, jiraSource="pull")
    assert _rule(client, admin_headers, pulled, "Blocked", source="User").get("id")
    assert _rule(client, admin_headers, pulled, "At Risk", source="User").get("id")
    assert len(_open(team, pulled)) == 2


def test_clean_up_clears_only_system_rule_alerts_on_pulled_items(team):
    pulled, own = _item(team, jiraSource="pull"), _item(team)
    with server.db(team) as c:
        for pid, kind, src in ((pulled, "Needs Date Check", "System"), (pulled, "Needs Decision", "System"),
                               (pulled, "Blocked", "User"), (own, "Needs Date Check", "System")):
            c.execute("INSERT INTO activities(activity_type, source, item_id, status, created_ts) VALUES(?,?,?,'Open','x')",
                      (kind, src, pid))
    dry = server._ac_clear_jira_owned(team, dry_run=True)
    assert dry["cleared"] == 2 and len(_open(team, pulled)) == 3       # dry run writes nothing
    r = server._ac_clear_jira_owned(team, "admin")
    assert r["cleared"] == 2 and r["byType"] == {"Needs Date Check": 1, "Needs Decision": 1}
    assert [a["activity_type"] for a in _open(team, pulled)] == ["Blocked"]
    assert len(_open(team, own)) == 1
    with server.db(team) as c:
        assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='activity:clear-jira-owned'").fetchone()[0] == 1
    assert server._ac_clear_jira_owned(team)["cleared"] == 0             # idempotent
