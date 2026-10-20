"""JIRA-POINTS-1 + JIRA-RELDATE-2.

Story points follow Jira (Story Points customfield_10014, else Story point estimate customfield_10028) on
create, the pulled-item refresh, the per-item sync and a one-time backfill; an empty Jira value never clears
Flow's. A per-item sync INTO a released status stamps releaseDate (Jira's Done date, else the day observed),
and the server sync runs the release-date fill. Jira mocked throughout."""
import json
from datetime import datetime

import server

STATUSES = ["New", "In Progress", "Released"]
CTX = {"type_rev": {"Story": "Story"}, "status_eff": {"In Dev": "In Progress", "In Prod": "Released"}, "overlay": {},
       "proj_rev": {"FRAZ": "Fraznet"}, "pods": [], "assignee_map": {}, "released": {"Released"},
       "terminal": {"Released": True}}


def test_points_helper_prefers_story_points_then_estimate_and_normalises():
    assert server._jira_points({"customfield_10014": 5.0, "customfield_10028": 8}) == 5
    assert server._jira_points({"customfield_10028": 2.5}) == 2.5
    assert server._jira_points({"customfield_10014": None, "customfield_10028": ""}) is None
    assert server._jira_points(None) is None


def test_refresh_follows_jira_points_and_never_clears():
    iss = lambda **f: {"key": "FRAZ-1", "fields": {"status": {"name": "In Dev"}, **f}}
    cur = {"status": "In Progress", "storyPoints": 3}
    chg = server._refresh_one_pulled(cur, iss(customfield_10014=5), CTX, STATUSES)
    assert cur["storyPoints"] == 5 and chg["storyPoints"] == (3, 5)
    chg = server._refresh_one_pulled(cur, iss(), CTX, STATUSES)
    assert cur["storyPoints"] == 5 and "storyPoints" not in chg


def test_create_takes_points():
    iss = {"key": "FRAZ-9", "fields": {"summary": "S", "issuetype": {"name": "Story"}, "status": {"name": "In Dev"},
                                       "project": {"key": "FRAZ"}, "customfield_10028": 3}}
    item, _ = server._construct_pull_item(iss, CTX, "admin")
    assert item["storyPoints"] == 3


# ── per-item sync (Flow-created linked item) ──────────────────────────────────────────────────────────
def _sync(client, h, monkeypatch, fields):
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_fetch_jira_feature_flags", lambda t: set())
    base = {"summary": "S", "issuetype": {"name": "Story"}, "project": {"key": "FRAZ", "name": "P"}}
    monkeypatch.setattr(server, "_jira_req", lambda m, path, *a, **k: {"fields": {**base, **fields}})
    client.put("/api/config/statuses", json=["Planned", "In Progress", "Released"], headers=h)
    client.put("/api/config/statusIsReleased", json={"Released": True}, headers=h)
    client.put("/api/config/statusIsTerminal", json={"Released": True}, headers=h)
    client.put("/api/config/jiraStatusMapping", json={"Released": "In Prod", "In Progress": "In Dev"}, headers=h)
    pid = client.post("/api/projects", json={"name": "Item", "status": "Planned", "jiraTickets": ["FRAZ-1"]}, headers=h).json()["id"]
    r = client.post(f"/api/jira/pull/{pid}", json={}, headers=h).json()
    return r, next(p for p in client.get("/api/all", headers=h).json()["projects"] if p["id"] == pid), pid


def test_sync_into_released_stamps_done_date(client, admin_headers, monkeypatch):
    _, item, _ = _sync(client, admin_headers, monkeypatch, {"status": {"name": "In Prod"}, "resolution": {"name": "Done"},
                                                            "resolutiondate": "2026-10-05T16:00:00.000-0600"})
    assert item["status"] == "Released" and item["releaseDate"] == "2026-10-05"


def test_sync_into_released_without_done_date_stamps_today(client, admin_headers, monkeypatch):
    _, item, _ = _sync(client, admin_headers, monkeypatch, {"status": {"name": "In Prod"}})
    assert item["releaseDate"] == datetime.now(server._MT_ZONE).date().isoformat()


def test_sync_into_non_released_sets_no_release_date(client, admin_headers, monkeypatch):
    _, item, _ = _sync(client, admin_headers, monkeypatch, {"status": {"name": "In Dev"}})
    assert item["status"] == "In Progress" and not item.get("releaseDate")


def test_sync_copies_points_and_reports_the_change(client, admin_headers, monkeypatch):
    r, item, pid = _sync(client, admin_headers, monkeypatch, {"status": {"name": "In Dev"}, "customfield_10014": 8})
    assert item["storyPoints"] == 8 and r["pointsChanged"] is True
    r2 = client.post(f"/api/jira/pull/{pid}", json={}, headers=admin_headers).json()
    assert r2["pointsChanged"] is False                                     # idempotent


# ── one-time points backfill ──────────────────────────────────────────────────────────────────────────
def test_points_backfill_writes_preserves_token_and_is_idempotent(team, monkeypatch):
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_jira_search_all", lambda jql, fields, cap=2000:
                        [{"key": "FRAZ-1", "fields": {"customfield_10014": 5}}, {"key": "FRAZ-2", "fields": {}}])
    with server.db(team) as c:
        a = server._insert_project(c, {"name": "A", "status": "New", "jiraTickets": ["FRAZ-1"]})
        b = server._insert_project(c, {"name": "B", "status": "New", "jiraTickets": ["FRAZ-2"], "storyPoints": 2})
        ts_a = c.execute("SELECT updated_ts FROM projects WHERE id=?", (a,)).fetchone()[0]
    dry = server._jira_backfill_story_points(team, "admin", dry=True)
    assert dry["toWrite"] == 1 and dry["written"] == 0
    r = server._jira_backfill_story_points(team, "admin")
    assert r["written"] == 1 and r["jiraHasPoints"] == 1
    with server.db(team) as c:
        ra = c.execute("SELECT data, updated_ts FROM projects WHERE id=?", (a,)).fetchone()
        rb = json.loads(c.execute("SELECT data FROM projects WHERE id=?", (b,)).fetchone()[0])
    assert json.loads(ra[0])["storyPoints"] == 5 and ra[1] == ts_a and rb["storyPoints"] == 2   # empty Jira keeps 2
    assert server._jira_backfill_story_points(team, "admin")["written"] == 0


def test_server_sync_runs_the_release_date_fill(team, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "jira_pull_all", lambda b, x, auth: {})
    monkeypatch.setattr(server, "jira_pull_now", lambda b, auth: {})
    monkeypatch.setattr(server, "_jira_backfill_release_dates", lambda t, a, dry=False: calls.append(t) or {"written": 3})
    with server.db(team) as c:
        for k, v in (("jiraEnabled", True), ("jiraSyncConfig", {"enabled": True, "intervalMinutes": 15})):
            c.execute("INSERT OR REPLACE INTO config(key, value) VALUES(?, ?)", (k, json.dumps(v)))
    out = server.run_jira_sync(only_team=team)
    assert calls == [team] and out[team]["releaseDates"] == 3
