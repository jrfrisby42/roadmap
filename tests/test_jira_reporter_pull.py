"""JIRA-REPORTER-PULL-1: a pulled item's reporter comes from the Jira issue's reporter.

Before this, the pull stamped whoever ran it ("jira-sync", or the admin who clicked) as the reporter. The
Jira reporter is matched to a Flow user through the Org's jiraAssigneeMap (accountId -> username), then by
email; anyone else keeps their Jira display name. A one-time backfill fixes items already pulled.
"""
import json

import server


def _cfg(team, key, val):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(val)))


def _setup(team):
    _cfg(team, "users", [{"username": "admin", "role": "admin"},
                         {"username": "dan.hill", "role": "editor", "email": "Dan.Hill@frazil.com"},
                         {"username": "gopika", "role": "editor"}])
    _cfg(team, "jiraAssigneeMap", {"acc-gopika": "gopika"})
    _cfg(team, "jiraTypeMapping", {"Story": "Story"})


def test_mapping_order():
    amap, by_email = {"acc-1": "gopika"}, {"dan.hill@frazil.com": "dan.hill"}
    f = server._jira_reporter_to_flow
    assert f({"accountId": "acc-1", "displayName": "Gopika J", "emailAddress": "dan.hill@frazil.com"}, amap, by_email) == "gopika"
    assert f({"accountId": "acc-9", "displayName": "Dan", "emailAddress": "DAN.HILL@frazil.com"}, amap, by_email) == "dan.hill"
    assert f({"accountId": "acc-9", "displayName": "  Michelle   Curran "}, amap, by_email) == "Michelle Curran"
    assert f(None, amap, by_email) == "" and f({}, amap, by_email) == ""
    # Jira often hides the email: an UNAMBIGUOUS display-name match still finds the Flow user.
    users = [{"username": "jr.frisby", "firstName": "J.R.", "lastName": "Frisby"},
             {"username": "a1", "firstName": "Sam", "lastName": "Lee"}, {"username": "a2", "firstName": "Sam", "lastName": "Lee"}]
    assert f({"accountId": "z", "displayName": "J.R. Frisby"}, amap, by_email, users) == "jr.frisby"
    assert f({"accountId": "z", "displayName": "Sam Lee"}, amap, by_email, users) == "Sam Lee"   # ambiguous -> name


def test_pull_uses_the_jira_reporter_else_the_actor(team):
    _setup(team)
    ctx = server._pull_context(team)
    base = {"key": "FRAZ-1", "fields": {"summary": "S", "issuetype": {"name": "Story"}}}
    with_rep = {**base, "fields": {**base["fields"], "reporter": {"accountId": "acc-gopika", "displayName": "Gopika"}}}
    item, _ = server._construct_pull_item(with_rep, ctx, "jira-sync")
    assert item["reporter"] == "gopika"
    item, _ = server._construct_pull_item(base, ctx, "jira-sync")
    assert item["reporter"] == "jira-sync"                     # no reporter on the issue -> as before
    assert "reporter" in server._PULL_FETCH_FIELDS.split(",")


def _seed(team, pid, key, reporter, source="pull"):
    p = {"name": f"Item {pid}", "jiraTickets": [key], "reporter": reporter}
    if source:
        p["jiraSource"] = source
    with server.db(team) as c:
        c.execute("INSERT INTO projects(id, data) VALUES(?, ?)", (pid, json.dumps(p)))
        server._reindex_project(c, pid, p, "2026-01-01T00:00:00+00:00")


def test_backfill_dry_run_apply_and_idempotent(team, monkeypatch):
    _setup(team)
    _seed(team, 1, "FRAZ-1", "jira-sync")
    _seed(team, 2, "FRAZ-2", "jr.frisby")
    _seed(team, 3, "FRAZ-3", "gopika")                         # already right
    _seed(team, 4, "FRAZ-4", "jira-sync")                      # Jira has no reporter -> left alone
    _seed(team, 5, "FRAZ-5", "michelle", source=None)          # created in Flow, only linked -> not touched
    reporters = {"FRAZ-1": {"accountId": "acc-gopika"}, "FRAZ-2": {"accountId": "x", "emailAddress": "dan.hill@frazil.com"},
                 "FRAZ-3": {"accountId": "acc-gopika"}, "FRAZ-5": {"accountId": "acc-gopika"}}
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_jira_search_all", lambda jql, fields, cap=2000:
                        [{"key": k, "fields": {"reporter": v}} for k, v in reporters.items() if k in jql])
    dry = server._jira_backfill_pull_reporters(team, "admin", dry=True)
    assert (dry["candidates"], dry["toWrite"], dry["written"]) == (4, 2, 0)
    assert dry["from"] == {"jira-sync": 1, "jr.frisby": 1} and dry["to"] == {"gopika": 1, "dan.hill": 1}
    with server.db(team) as c:
        assert json.loads(c.execute("SELECT data FROM projects WHERE id=1").fetchone()["data"])["reporter"] == "jira-sync"
    out = server._jira_backfill_pull_reporters(team, "admin")
    assert out["written"] == 2 and out["toFlowUser"] == 2
    with server.db(team) as c:
        got = {r["id"]: (json.loads(r["data"])["reporter"], r["reporter"], r["updated_ts"])
               for r in c.execute("SELECT id, data, reporter, updated_ts FROM projects")}
        audits = c.execute("SELECT COUNT(*) FROM audit_log WHERE action='jira:reporter-backfill'").fetchone()[0]
    assert got[1] == ("gopika", "gopika", "2026-01-01T00:00:00+00:00")   # indexed column follows, updated_ts kept
    assert got[2][0] == "dan.hill" and got[4][0] == "jira-sync" and got[5][0] == "michelle"
    assert audits == 1
    assert server._jira_backfill_pull_reporters(team, "admin")["written"] == 0


def test_backfill_aborts_cleanly_when_jira_fails(team, monkeypatch):
    _setup(team)
    _seed(team, 1, "FRAZ-1", "jira-sync")
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    def boom(jql, fields, cap=2000):
        raise RuntimeError("timeout")
    monkeypatch.setattr(server, "_jira_search_all", boom)
    out = server._jira_backfill_pull_reporters(team, "admin")
    assert out["aborted"] and out["written"] == 0
