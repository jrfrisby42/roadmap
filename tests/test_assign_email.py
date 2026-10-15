"""ASSIGN-EMAIL-1: assignment emails.

- Morning email (default ON per person): a "Newly assigned to you" section listing items assigned since the
  previous morning email (first time: the last 24h), only while still theirs. Sent on its own if no To-dos are due.
- Instant email (default OFF per person): one email per "assigned" notification (create / edit / bulk / portal),
  sent through the same _notify hook so every assignment path is covered; bulk 4+ is one summary.
- One-click off links: the morning link turns off both morning sections; k=instant turns off instant emails.
Mail is monkeypatched; _OUTBOUND_ASYNC is forced off so instant sends happen inline.
"""
from datetime import datetime, timezone

import pytest

import server

MON = datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc)      # Mon 07:00 MDT
TUE = datetime(2026, 10, 6, 13, 0, tzinfo=timezone.utc)
BEFORE_MON = "2026-10-05T10:00:00+00:00"                     # inside Monday's first window (last 24h)


def _h(team, user, role="editor"):
    return {"Authorization": f"Bearer {server.create_token(team, user, role)}", "X-Team": team}


@pytest.fixture
def env(client, team, admin_headers, monkeypatch):
    jake = f"jake-{team}@example.com"
    client.put("/api/config/users", json=[
        {"username": "admin", "role": "admin", "email": f"admin-{team}@example.com"},
        {"username": "jake", "role": "editor", "email": jake},
        {"username": "ann", "role": "editor", "email": f"ann-{team}@example.com"},
    ], headers=admin_headers)
    sent = []
    monkeypatch.setattr(server, "mail_configured", lambda: True)
    monkeypatch.setattr(server, "send_email", lambda to, subj, text, html=None: sent.append((to, subj, text, html)))
    monkeypatch.setattr(server, "_OUTBOUND_ASYNC", False)

    class E:
        pass
    e = E(); e.team, e.jake, e.sent = team, jake, sent
    e.to = lambda addr: [m for m in sent if m[0] == addr]
    e.run = lambda when: server.send_todo_reminders(now_utc=when, only_team=team)

    def item(name, assignee="jake"):
        return client.post("/api/projects", json={"name": name, "status": "Planned", "assignee": assignee},
                           headers=admin_headers).json()["id"]
    e.item = item

    def backdate():
        with server.db(team) as c:
            c.execute("UPDATE notifications SET created_ts=? WHERE type='assigned'", (BEFORE_MON,))
    e.backdate = backdate
    return e


def test_pref_defaults_and_validation(env, client):
    h = _h(env.team, "jake")
    d = client.get("/api/my/prefs", headers=h).json()
    assert d["assignDaily"] is True and d["assignInstant"] is False and d["todoEmail"] is True
    d = client.put("/api/my/prefs", json={"assignInstant": True, "assignDaily": False}, headers=h).json()
    assert d["assignInstant"] is True and d["assignDaily"] is False and d["todoEmail"] is True
    assert client.put("/api/my/prefs", json={"assignDaily": "yes"}, headers=h).status_code == 422


def test_morning_email_lists_new_assignments_without_any_todos(env):
    env.item("Fix the <b>login</b> page")
    env.backdate()
    env.run(MON)
    [(_, subj, text, body)] = env.to(env.jake)
    assert subj == "1 item newly assigned to you"
    assert "Newly assigned to you" in text and "Fix the <b>login</b> page" in text
    assert "&lt;b&gt;login&lt;/b&gt;" in body and "Newly assigned to you" in body


def test_next_morning_does_not_repeat_old_assignments(env):
    env.item("Once")
    env.backdate()
    env.run(MON)
    env.run(TUE)                                     # window now starts at Monday's run
    assert len(env.to(env.jake)) == 1


def test_reassigned_away_is_not_listed(env, client, admin_headers):
    pid = env.item("Moved on")
    client.put(f"/api/projects/{pid}", json={"name": "Moved on", "status": "Planned", "assignee": "ann"}, headers=admin_headers)
    env.backdate()
    env.run(MON)
    assert env.to(env.jake) == []                    # no longer Jake's, and nothing else to report


def test_daily_section_off_means_no_assignment_email(env, client):
    client.put("/api/my/prefs", json={"assignDaily": False}, headers=_h(env.team, "jake"))
    env.item("Quiet")
    env.backdate()
    env.run(MON)
    assert env.to(env.jake) == []


def test_instant_email_is_off_by_default(env):
    env.item("No instant")
    assert env.to(env.jake) == []


def test_instant_email_when_turned_on(env, client):
    client.put("/api/my/prefs", json={"assignInstant": True}, headers=_h(env.team, "jake"))
    pid = env.item("Urgent fix")
    [(_, subj, text, _)] = env.to(env.jake)
    assert subj.startswith("Assigned to you:") and "Urgent fix" in subj
    assert f"/item/{pid}" in text and "k=instant" in text


def test_instant_bulk_assignment_is_one_summary_email(env, client, admin_headers):
    client.put("/api/my/prefs", json={"assignInstant": True}, headers=_h(env.team, "jake"))
    ids = [env.item(f"Bulk {i}", assignee="") for i in range(5)]
    client.post("/api/items/bulk", json={"ids": ids, "patch": {"assignee": "jake"}}, headers=admin_headers)
    mine = env.to(env.jake)
    assert len(mine) == 1 and "assigned you 5 items" in mine[0][1]


def test_self_assignment_sends_no_email(env, client):
    client.put("/api/my/prefs", json={"assignInstant": True}, headers=_h(env.team, "jake"))
    client.post("/api/projects", json={"name": "Mine", "status": "Planned", "assignee": "jake"}, headers=_h(env.team, "jake"))
    assert env.to(env.jake) == []


def test_off_links(env, client):
    h = _h(env.team, "jake")
    client.put("/api/my/prefs", json={"assignInstant": True}, headers=h)
    bad = client.get(f"/todo-email/off?team={env.team}&u=jake&k=instant&t={server._todo_off_token(env.team, 'jake')}")
    assert bad.status_code == 400                     # a morning token cannot switch off instant emails
    ok = client.get(f"/todo-email/off?team={env.team}&u=jake&k=instant&t={server._assign_off_token(env.team, 'jake')}")
    assert ok.status_code == 200 and client.get("/api/my/prefs", headers=h).json()["assignInstant"] is False
    ok = client.get(f"/todo-email/off?team={env.team}&u=jake&t={server._todo_off_token(env.team, 'jake')}")
    d = client.get("/api/my/prefs", headers=h).json()
    assert ok.status_code == 200 and d["todoEmail"] is False and d["assignDaily"] is False
