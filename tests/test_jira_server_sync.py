"""JIRA-SERVER-SYNC-1: the background Jira sync runs on the server (`server.py --sync-jira`, systemd timer).

Per Org: runs the linked-item sync then the pull when Background Pull-Sync is enabled and its interval has
elapsed; records the run in a state file shown to admins. The browser timer is retired. Jira mocked."""
import json
import os
from datetime import datetime, timedelta, timezone

import server


def _cfg(team, enabled=True, interval=15, jira=True):
    with server.db(team) as c:
        for k, v in (("jiraEnabled", jira), ("jiraSyncConfig", {"enabled": enabled, "intervalMinutes": interval})):
            c.execute("INSERT OR REPLACE INTO config(key, value) VALUES(?, ?)", (k, json.dumps(v)))


def _mock(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "jira_pull_all", lambda body, x_team, auth: calls.append(("all", auth["team"], auth["username"])) or
                        {"synced": 3, "changes": 1, "errors": 0, "childrenAdded": 0})
    monkeypatch.setattr(server, "jira_pull_now", lambda body, auth: calls.append(("now", auth["team"], auth["username"])) or
                        {"createdCount": 2, "refreshedCount": 4, "membershipChanged": 0,
                         "refresh": {"statusChanged": 1, "datesChanged": 2}, "create": {"autoLinked": [{"key": "X"}]}})
    return calls


def test_runs_both_steps_in_order_and_records_state(team, monkeypatch):
    calls = _mock(monkeypatch); _cfg(team)
    out = server.run_jira_sync(only_team=team)
    assert calls == [("all", team, "jira-sync"), ("now", team, "jira-sync")]
    assert out[team]["linked"]["changes"] == 1 and out[team]["pull"] == {
        "created": 2, "refreshed": 4, "statusChanged": 1, "datesChanged": 2, "autoLinked": 1, "sprintMembership": 0}
    st = server._read_jira_sync_state(team)
    assert st["lastRunAt"] and st["intervalMinutes"] == 15 and st["result"] == out[team]


def test_skips_until_the_interval_has_elapsed(team, monkeypatch):
    calls = _mock(monkeypatch); _cfg(team, interval=15)
    server.run_jira_sync(only_team=team)
    server.run_jira_sync(only_team=team)                                   # 5 min later: not due
    assert len(calls) == 2
    past = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    server._write_jira_sync_state(team, {"lastRunAt": past})
    server.run_jira_sync(only_team=team)                                   # due again
    assert len(calls) == 4


def test_force_ignores_the_interval(team, monkeypatch):
    calls = _mock(monkeypatch); _cfg(team)
    server.run_jira_sync(only_team=team)
    server.run_jira_sync(only_team=team, force=True)
    assert len(calls) == 4


def test_interval_is_floored_at_the_timer_tick(team, monkeypatch):
    _mock(monkeypatch); _cfg(team, interval=2)
    server.run_jira_sync(only_team=team)
    assert server._read_jira_sync_state(team)["intervalMinutes"] == server._JIRA_SYNC_TICK_MIN


def test_disabled_or_jira_off_or_unconfigured_does_nothing(team, monkeypatch):
    calls = _mock(monkeypatch)
    _cfg(team, enabled=False); server.run_jira_sync(only_team=team)
    _cfg(team, jira=False);    server.run_jira_sync(only_team=team)
    monkeypatch.setattr(server, "jira_configured", lambda: False); _cfg(team)
    server.run_jira_sync(only_team=team)
    assert calls == [] and not os.path.exists(server._jira_sync_state_path(team))


def test_one_step_failing_still_runs_the_other_and_records_it(team, monkeypatch):
    calls = _mock(monkeypatch); _cfg(team)
    def boom(body, x_team, auth): raise RuntimeError("Jira down")
    monkeypatch.setattr(server, "jira_pull_all", boom)
    out = server.run_jira_sync(only_team=team)
    assert "Jira down" in out[team]["linkedError"] and ("now", team, "jira-sync") in calls


def test_sync_status_endpoint_is_admin_only(client, team, admin_headers, editor_headers):
    server._write_jira_sync_state(team, {"lastRunAt": "2026-10-20T15:00:00+00:00", "result": {"pull": {"created": 1}}})
    assert client.get("/api/jira/sync-status", headers=admin_headers).json()["lastRunAt"] == "2026-10-20T15:00:00+00:00"
    assert client.get("/api/jira/sync-status", headers=editor_headers).status_code == 403


def test_browser_timer_is_retired():
    html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "roadmap.html"), encoding="utf-8").read()
    i = html.index("function _startBackgroundJiraSync(){")
    body = html[i:html.index("\n}\n", i)]
    assert "setInterval" not in body and "_stopBackgroundJiraSync();" in body
