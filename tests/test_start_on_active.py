"""START-ON-ACTIVE-1: a Jira move into an active status stamps a missing start date (the item modal's rule).

Only when the item has no start; the day Flow observes it (weekend -> Monday); a missing due is filled from
the estimate, an existing due never moves; roll-up parents are skipped; for pulled items Jira's own Start
date wins. Jira mocked."""
import server


def _jira(client, h, monkeypatch, **item):
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_fetch_jira_feature_flags", lambda t: set())
    monkeypatch.setattr(server, "_jira_req", lambda m, path, *a, **k: {"fields": {
        "status": {"name": "In Dev"}, "summary": "S", "issuetype": {"name": "Story"}, "project": {"key": "FRAZ", "name": "P"}}})
    client.put("/api/config/statuses", json=["Planned", "Approved", "In Progress", "Released"], headers=h)
    client.put("/api/config/statusIsActive", json={"In Progress": True}, headers=h)
    client.put("/api/config/jiraStatusMapping", json={"Approved": "To Do", "In Progress": "In Dev"}, headers=h)
    body = {"name": "I", "status": "Approved", "jiraTickets": ["FRAZ-1"], **item}
    pid = client.post("/api/projects", json=body, headers=h).json()["id"]
    return pid


def _get(client, h, pid):
    return next(p for p in client.get("/api/all", headers=h).json()["projects"] if p["id"] == pid)


def _sync(client, h, pid):
    client.post(f"/api/jira/pull/{pid}", json={}, headers=h)
    return _get(client, h, pid)


def test_activating_in_jira_stamps_today_and_fills_due(client, admin_headers, monkeypatch):
    pid = _jira(client, admin_headers, monkeypatch, dueWeeks=1, testWeeks=0)
    p = _sync(client, admin_headers, pid)
    today = server._flow_today_iso()
    assert p["status"] == "In Progress" and p["start"] == today
    assert p["due"] == server._compute_item_due({"start": today, "dueWeeks": 1})


def test_existing_start_and_due_are_kept(client, admin_headers, monkeypatch):
    pid = _jira(client, admin_headers, monkeypatch, start="2026-09-01", due="2026-09-15", dueWeeks=2, testWeeks=0)
    p = _sync(client, admin_headers, pid)
    assert p["start"] == "2026-09-01" and p["due"] == "2026-09-15"


def test_existing_due_never_moves_when_start_is_stamped(client, admin_headers, monkeypatch):
    pid = _jira(client, admin_headers, monkeypatch, due="2026-12-01", dueWeeks=2, testWeeks=0)
    p = _sync(client, admin_headers, pid)
    assert p["start"] == server._flow_today_iso() and p["due"] == "2026-12-01"


def test_rollup_parent_is_not_stamped(client, admin_headers, monkeypatch):
    pid = _jira(client, admin_headers, monkeypatch)
    client.post("/api/projects", json={"name": "child", "status": "Planned", "parent": pid}, headers=admin_headers)
    p = _sync(client, admin_headers, pid)
    assert p["status"] == "In Progress" and not p.get("start")


def test_today_rolls_a_weekend_to_monday(monkeypatch):
    class Fake(server.datetime):
        @classmethod
        def now(cls, tz=None):
            return server.datetime(2026, 10, 24, 12, tzinfo=tz)   # a Saturday
    monkeypatch.setattr(server, "datetime", Fake)
    assert server._flow_today_iso() == "2026-10-26"


def test_pulled_refresh_stamps_only_when_jira_has_no_start():
    ctx = {"status_eff": {"In Dev": "In Progress"}, "pods": [], "assignee_map": {}, "released": set(),
           "terminal": {}, "active": {"In Progress": True}, "parentIds": set()}
    sts = ["New", "In Progress"]
    no_start = {"status": "New", "jiraSource": "pull"}
    chg = server._refresh_one_pulled(no_start, {"key": "K", "fields": {"status": {"name": "In Dev"}}}, ctx, sts, pid=5)
    assert no_start["start"] == server._flow_today_iso() and chg["start"] == ("", no_start["start"])
    jira_start = {"status": "New", "jiraSource": "pull"}
    server._refresh_one_pulled(jira_start, {"key": "K", "fields": {"status": {"name": "In Dev"}, "customfield_10025": "2026-10-01"}}, ctx, sts, pid=6)
    assert jira_start["start"] == "2026-10-01"
    parent = {"status": "New", "jiraSource": "pull"}
    server._refresh_one_pulled(parent, {"key": "K", "fields": {"status": {"name": "In Dev"}}}, {**ctx, "parentIds": {7}}, sts, pid=7)
    assert not parent.get("start")
