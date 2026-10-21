"""POINTS-ESTIMATE-1 (story points set the time estimate) + JIRA-STATUSMAP-1 (per-item sync honours the
pull's status overlay). Table rows {upTo, weeks}, OFF by default; the development table floors at 1 day."""
import json

import server

PE = {"enabled": True, "rows": [{"upTo": 2, "weeks": 0.142857}, {"upTo": 3, "weeks": 0.428571}, {"upTo": 5, "weeks": 1},
                                {"upTo": 7, "weeks": 2}, {"upTo": None, "weeks": 3}]}
DAY, THREE = 0.142857, 0.428571


def test_table_lookup_floors_at_one_day_and_catches_all():
    w = lambda p: server._points_estimate_weeks(p, PE)
    assert [w(x) for x in (0.05, 0.5, 1, 1.75, 2)] == [DAY] * 5
    assert w(2.5) == THREE and w(3) == THREE and w(4) == 1 and w(5) == 1 and w(6) == 2 and w(7) == 2
    assert w(10) == 3 and w(40) == 3
    assert w(0) is None and w(None) is None and w("x") is None
    assert server._points_estimate_weeks(3, {**PE, "enabled": False}) is None
    assert server._points_estimate_weeks(3, None) is None


def test_due_is_whole_days_with_weekend_roll():
    due = lambda start, w, **kw: server._compute_item_due({"start": start, "dueWeeks": w, **kw})
    assert due("2026-10-19", DAY) == "2026-10-20"           # Mon + 1 day (0.999999 days rounds to 1)
    assert due("2026-10-19", THREE) == "2026-10-22"         # Mon + 3 days
    assert due("2026-10-20", 1) == "2026-10-27"             # Tue + 1 week
    assert due("2026-10-23", DAY) == "2026-10-26"           # Fri + 1 day = Sat -> Mon
    assert due("2026-10-19", 4, testWeeks=2, parallelResources=2) == "2026-11-09"   # 2 dev/2 + 2 test = 3 wk
    assert due("", 1) == ""


# ── through the API ───────────────────────────────────────────────────────────────────────────────────
def _setup(client, h, enabled=True):
    client.put("/api/config/pointsEstimate", json={**PE, "enabled": enabled}, headers=h)
    client.put("/api/config/statusIsTerminal", json={"Released": True}, headers=h)


def _get(client, h, pid):
    return next(p for p in client.get("/api/all", headers=h).json()["projects"] if p["id"] == pid)


def _put(client, h, pid, **f):
    cur = _get(client, h, pid); cur.update(f)
    r = client.put(f"/api/projects/{pid}", json=cur, headers=h)
    assert r.status_code == 200, r.text
    return _get(client, h, pid)


def _mk(client, h, **f):
    body = {"name": "I", "status": "Planned", "start": "2026-10-19", "dueWeeks": 4, "testWeeks": 2, **f}
    r = client.post("/api/projects", json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_setting_points_sets_estimate_and_due(client, admin_headers):
    h = admin_headers; _setup(client, h)
    pid = _mk(client, h, testWeeks=0, dueWeeks=4)
    p = _put(client, h, pid, storyPoints=3)
    assert p["dueWeeks"] == THREE and p["due"] == "2026-10-22" and p["dueWeeksSource"] == "points"


def test_test_period_is_cleared_when_it_would_block_the_estimate(client, admin_headers):
    h = admin_headers; _setup(client, h)
    pid = _mk(client, h, dueWeeks=4, testWeeks=2)
    p = _put(client, h, pid, storyPoints=1)
    assert p["dueWeeks"] == DAY and p["testWeeks"] == 0
    assert _put(client, h, pid, name="renamed")["name"] == "renamed"   # the 422 rule is not tripped on the next save


def test_estimate_typed_in_the_same_save_wins(client, admin_headers):
    h = admin_headers; _setup(client, h)
    pid = _mk(client, h, testWeeks=0, dueWeeks=4)
    p = _put(client, h, pid, storyPoints=5, dueWeeks=6)
    assert p["dueWeeks"] == 6 and not p.get("dueWeeksSource")


def test_typing_an_estimate_later_stops_it_being_table_owned(client, admin_headers):
    h = admin_headers; _setup(client, h)
    pid = _mk(client, h, testWeeks=0, dueWeeks=4)
    _put(client, h, pid, storyPoints=5)
    p = _put(client, h, pid, dueWeeks=8)
    assert p["dueWeeks"] == 8 and not p.get("dueWeeksSource")


def test_table_off_or_finished_item_is_left_alone(client, admin_headers):
    h = admin_headers; _setup(client, h, enabled=False)
    pid = _mk(client, h, testWeeks=0, dueWeeks=4)
    assert _put(client, h, pid, storyPoints=3)["dueWeeks"] == 4
    _setup(client, h)
    done = _mk(client, h, testWeeks=0, dueWeeks=4, status="Released")
    assert _put(client, h, done, storyPoints=3)["dueWeeks"] == 4


def test_create_with_points_takes_the_table(client, admin_headers):
    h = admin_headers; _setup(client, h)
    p = _get(client, h, _mk(client, h, storyPoints=7))
    assert p["dueWeeks"] == 2 and p["testWeeks"] == 0 and p["due"] == "2026-11-02"


def test_pulled_item_with_jira_dates_is_left_alone_but_undated_one_is_set():
    ctx = {"status_eff": {}, "pods": [], "assignee_map": {}, "released": set(), "terminal": {}, "pointsEstimate": PE}
    dated = {"status": "New", "jiraSource": "pull", "start": "2026-10-19", "due": "2026-10-30"}
    server._refresh_one_pulled(dated, {"key": "K", "fields": {"customfield_10014": 3}}, ctx, ["New"])
    assert dated["storyPoints"] == 3 and dated["due"] == "2026-10-30" and "dueWeeks" not in dated
    undated = {"status": "New", "jiraSource": "pull", "start": "2026-10-19"}
    chg = server._refresh_one_pulled(undated, {"key": "K", "fields": {"customfield_10014": 3}}, ctx, ["New"])
    assert undated["dueWeeks"] == THREE and undated["due"] == "2026-10-22" and "dueWeeks" in chg


# ── per-item Jira sync ────────────────────────────────────────────────────────────────────────────────
def _jira(client, h, monkeypatch, status, **extra):
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_fetch_jira_feature_flags", lambda t: set())
    monkeypatch.setattr(server, "_jira_req", lambda m, path, *a, **k: {"fields": {
        "status": {"name": status}, "summary": "S", "issuetype": {"name": "Story"}, "project": {"key": "FRAZ", "name": "P"}, **extra}})
    client.put("/api/config/statuses", json=["Planned", "Approved", "In Progress", "Released"], headers=h)
    client.put("/api/config/jiraStatusMapping", json={"Approved": "To Do", "In Progress": "In Dev", "Released": "In Prod"}, headers=h)
    client.put("/api/config/jiraPullStatusMap", json={"Code Review": "In Progress"}, headers=h)
    pid = _mk(client, h, status="Approved", jiraTickets=["FRAZ-1"], testWeeks=0, dueWeeks=4)
    client.post(f"/api/jira/pull/{pid}", json={}, headers=h)
    return _get(client, h, pid)


def test_per_item_sync_honours_the_pull_overlay_status(client, admin_headers, monkeypatch):
    _setup(client, admin_headers)
    assert _jira(client, admin_headers, monkeypatch, "Code Review")["status"] == "In Progress"   # FRZ-1094


def test_status_recorded_while_unmapped_is_picked_up_once_it_maps(client, admin_headers, monkeypatch):
    # FRZ-1094: "Code Review" was recorded as the last-known Jira status while it had no mapping, so the
    # "unchanged" short-circuit skipped it forever. It must advance once the status maps forward.
    h = admin_headers; _setup(client, h)
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_fetch_jira_feature_flags", lambda t: set())
    monkeypatch.setattr(server, "_jira_req", lambda m, path, *a, **k: {"fields": {
        "status": {"name": "Code Review"}, "summary": "S", "issuetype": {"name": "Story"}, "project": {"key": "FRAZ", "name": "P"}}})
    client.put("/api/config/statuses", json=["Planned", "Approved", "In Progress", "Released"], headers=h)
    client.put("/api/config/jiraStatusMapping", json={"Approved": "To Do", "In Progress": "In Dev"}, headers=h)
    client.put("/api/config/jiraPullStatusMap", json={"Code Review": "In Progress"}, headers=h)
    pid = _mk(client, h, status="Approved", jiraTickets=["FRAZ-1"], testWeeks=0, dueWeeks=4,
              jiraLastKnownStatus={"FRAZ-1": "Code Review"})
    client.post(f"/api/jira/pull/{pid}", json={}, headers=h)
    assert _get(client, h, pid)["status"] == "In Progress"
    r = client.post(f"/api/jira/pull/{pid}", json={}, headers=h).json()
    assert r["changed"] is False                                   # then idempotent


def test_unchanged_status_mapping_backwards_is_still_skipped(client, admin_headers, monkeypatch):
    h = admin_headers; _setup(client, h)
    p = _jira(client, h, monkeypatch, "To Do")                       # maps to Approved = its current status
    assert p["status"] == "Approved"


def test_per_item_sync_points_set_the_estimate(client, admin_headers, monkeypatch):
    _setup(client, admin_headers)
    p = _jira(client, admin_headers, monkeypatch, "To Do", customfield_10014=5)
    assert p["storyPoints"] == 5 and p["dueWeeks"] == 1 and p["due"] == "2026-10-26"


# ── one-time catch-up ─────────────────────────────────────────────────────────────────────────────────
def test_catchup_changes_default_estimates_and_keeps_hand_typed(team):
    with server.db(team) as c:
        c.execute("INSERT OR REPLACE INTO config(key,value) VALUES('pointsEstimate',?)", (json.dumps(PE),))
        a = server._insert_project(c, {"name": "default", "status": "New", "storyPoints": 3, "dueWeeks": 2, "testWeeks": 2, "start": "2026-10-19"})
        b = server._insert_project(c, {"name": "typed", "status": "New", "storyPoints": 3, "dueWeeks": 6, "start": "2026-10-19"})
        server._insert_project(c, {"name": "no points", "status": "New", "dueWeeks": 2})
    dry = server._points_estimate_catchup(team, "admin", dry=True)
    assert dry["withPoints"] == 2 and dry["toChange"] == 1 and dry["keptHandTyped"] == 1 and dry["written"] == 0
    r = server._points_estimate_catchup(team, "admin")
    assert r["written"] == 1
    with server.db(team) as c:
        pa = json.loads(c.execute("SELECT data FROM projects WHERE id=?", (a,)).fetchone()[0])
        pb = json.loads(c.execute("SELECT data FROM projects WHERE id=?", (b,)).fetchone()[0])
    assert pa["dueWeeks"] == THREE and pa["due"] == "2026-10-22" and pa["testWeeks"] == 0 and pb["dueWeeks"] == 6
    assert server._points_estimate_catchup(team, "admin")["toChange"] == 0      # idempotent


def test_config_key_is_registered_and_defaults_off(client, admin_headers):
    cfg = client.get("/api/all", headers=admin_headers).json()["pointsEstimate"]
    assert cfg["enabled"] is False and cfg["rows"][0] == {"upTo": 2, "weeks": 0.142857}
