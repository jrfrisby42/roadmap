"""TODO-EMAIL-1: weekday-morning To-do reminders (email + optional Slack DM).

Every call is scoped with only_team (via _run) so the job does not walk the hundreds of tenants the rest of
the suite has created - that alone pushed the full run past ten minutes.

send_todo_reminders(now_utc=...) is driven with fixed instants so the Mountain-Time day boundary, DST and
the weekend rule are exercised exactly. send_email / Slack transport are monkeypatched - nothing leaves
the box. Assertions also filter by this test's own recipients.
"""
from datetime import datetime, timezone

import pytest

import server

MON = datetime(2026, 10, 5, 13, 0, tzinfo=timezone.utc)      # Mon 07:00 MDT
SAT = datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc)      # Sat 08:00 MDT
SUN_MT_MON_UTC = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)   # Sun 21:00 MDT, already Monday in UTC
# DST ends Sun 2026-11-01. Tue 2026-11-03 06:30 UTC = Mon 2026-11-02 23:30 MST: UTC says the 3rd, MT the 2nd.
POST_DST_LATE = datetime(2026, 11, 3, 6, 30, tzinfo=timezone.utc)


def _h(team, user, role="editor"):
    return {"Authorization": f"Bearer {server.create_token(team, user, role)}", "X-Team": team}


@pytest.fixture
def setup(client, team, admin_headers, monkeypatch):
    jake = f"jake-{team}@example.com"; gopi = f"gopi-{team}@example.com"
    r = client.put("/api/config/users", json=[
        {"username": "admin", "role": "admin"},
        {"username": "jake", "role": "editor", "email": jake},
        {"username": "gopi", "role": "editor", "email": gopi},
        {"username": "nomail", "role": "editor"},
    ], headers=admin_headers)
    assert r.status_code == 200, r.text
    sent, dms = [], []
    monkeypatch.setattr(server, "mail_configured", lambda: True)
    monkeypatch.setattr(server, "send_email", lambda to, subj, text, html=None: sent.append((to, subj, text, html)))
    monkeypatch.setattr(server, "_slack_bot_token", lambda t: "xoxb-test" if t == team else "")
    monkeypatch.setattr(server, "_slack_user_id", lambda t, tok, email: "U-" + email if t == team else "")
    monkeypatch.setattr(server, "_slack_post_dm", lambda tok, uid, text: dms.append((uid, text)))

    def todo(user, title, due=None, done=False):
        body = {"title": title}
        if due:
            body["due_date"] = due
        tid = client.post("/api/my/todos", json=body, headers=_h(team, user)).json()["todo"]["id"]
        if done:
            client.put(f"/api/my/todos/{tid}", json={"status": "Done"}, headers=_h(team, user))
        return tid

    class S:
        pass
    s = S(); s.team, s.jake, s.gopi, s.sent, s.dms, s.todo = team, jake, gopi, sent, dms, todo
    s.run = lambda **kw: server.send_todo_reminders(only_team=team, **kw)
    s.mine = lambda addr: [m for m in sent if m[0] == addr]
    s.my_dms = lambda addr: [d for d in dms if d[0] == "U-" + addr]
    return s


def test_email_lists_overdue_and_due_today_only(setup):
    s = setup
    s.todo("jake", "Send <b>Q3</b> numbers", "2026-09-28")    # overdue 7 days
    s.todo("jake", "Review deploy", "2026-10-05")              # due today
    s.todo("jake", "Future thing", "2026-10-06")               # tomorrow - excluded
    s.todo("jake", "No date")                                  # excluded
    s.todo("jake", "Finished", "2026-09-01", done=True)        # excluded
    s.run(now_utc=MON)
    [(to, subj, text, body)] = s.mine(s.jake)
    assert subj == "1 To-do overdue, 1 due today"
    assert "Overdue - 7 days" in text and "Due today" in text
    assert "Future thing" not in text and "No date" not in text and "Finished" not in text
    assert "&lt;b&gt;Q3&lt;/b&gt;" in body and "<b>Q3</b>" not in body     # escaped in HTML
    assert "/todo-email/off?" in text                                      # one-click off link


def test_once_per_day(setup):
    s = setup
    s.todo("jake", "Thing", "2026-10-05")
    s.run(now_utc=MON)
    s.run(now_utc=MON)
    assert len(s.mine(s.jake)) == 1


def test_failed_send_releases_the_claim(setup, monkeypatch):
    s = setup
    s.todo("jake", "Thing", "2026-10-05")
    calls = {"n": 0}

    def flaky(to, subj, text, html=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("SES down")
        s.sent.append((to, subj, text, html))
    monkeypatch.setattr(server, "send_email", flaky)
    s.run(now_utc=MON)          # fails -> released
    s.run(now_utc=MON)          # retry succeeds
    assert len(s.mine(s.jake)) == 1


def test_default_on_and_opt_out(setup, client):
    s = setup
    s.todo("jake", "Thing", "2026-10-05")
    h = _h(s.team, "jake")
    assert client.get("/api/my/prefs", headers=h).json()["todoEmail"] is True        # default ON
    assert client.put("/api/my/prefs", json={"todoEmail": False}, headers=h).json()["todoEmail"] is False
    s.run(now_utc=MON)
    assert s.mine(s.jake) == []
    assert client.put("/api/my/prefs", json={"todoEmail": "no"}, headers=h).status_code == 422


def test_one_click_off_link(setup, client):
    s = setup
    good = server._todo_off_token(s.team, "jake")
    assert client.get(f"/todo-email/off?team={s.team}&u=jake&t=bad").status_code == 400
    assert client.get("/api/my/prefs", headers=_h(s.team, "jake")).json()["todoEmail"] is True   # forged link changed nothing
    r = client.get(f"/todo-email/off?team={s.team}&u=jake&t={good}")
    assert r.status_code == 200 and "no longer" in r.text
    assert client.get("/api/my/prefs", headers=_h(s.team, "jake")).json()["todoEmail"] is False


def test_privacy_and_skips(setup):
    s = setup
    s.todo("jake", "Jake private", "2026-10-05")
    s.todo("gopi", "Gopi private", "2026-10-01")
    s.todo("nomail", "No address", "2026-10-05")
    s.run(now_utc=MON)
    [(_, _, jt, _)] = s.mine(s.jake)
    [(_, _, gt, _)] = s.mine(s.gopi)
    assert "Jake private" in jt and "Gopi private" not in jt
    assert "Gopi private" in gt and "Jake private" not in gt
    assert not any("No address" in m[2] for m in s.sent)


def test_nothing_due_sends_nothing(setup):
    s = setup
    s.todo("jake", "Later", "2026-10-20")
    s.run(now_utc=MON)
    assert s.mine(s.jake) == []


@pytest.mark.parametrize("when", [SAT, SUN_MT_MON_UTC])
def test_weekend_in_mountain_time_sends_nothing(setup, when):
    s = setup
    s.todo("jake", "Thing", "2026-09-28")
    out = s.run(now_utc=when)
    assert out["skipped_weekend"] is True and s.mine(s.jake) == []


def test_mountain_day_boundary_after_dst(setup):
    """UTC is already Nov 3 but it is still Nov 2 in Mountain Time: a to-do due Nov 3 is not due yet."""
    s = setup
    s.todo("jake", "Due Nov 2", "2026-11-02")
    s.todo("jake", "Due Nov 3", "2026-11-03")
    out = s.run(now_utc=POST_DST_LATE)
    assert out["date"] == "2026-11-02"
    [(_, subj, text, _)] = s.mine(s.jake)
    assert "Due Nov 2" in text and "Due Nov 3" not in text and subj == "1 To-do due today"


def test_no_email_service_means_no_claim(setup, monkeypatch):
    s = setup
    s.todo("jake", "Thing", "2026-10-05")
    monkeypatch.setattr(server, "mail_configured", lambda: False)
    s.run(now_utc=MON)
    monkeypatch.setattr(server, "mail_configured", lambda: True)
    s.run(now_utc=MON)          # the earlier no-op did not burn today's send
    assert len(s.mine(s.jake)) == 1


def test_slack_dm_only_when_the_org_opts_in(setup, client, admin_headers):
    s = setup
    s.todo("jake", "Thing", "2026-10-05")
    # Off by default: the existing type list has no "reminder".
    s.run(now_utc=MON)
    assert s.my_dms(s.jake) == []
    # Channel-only mode never DMs (and never posts a private to-do to the channel).
    client.put("/api/config/slackNotify", json={"enabled": True, "types": ["reminder"], "mode": "channel"}, headers=admin_headers)
    s.run(now_utc=datetime(2026, 10, 6, 13, 0, tzinfo=timezone.utc))
    assert s.my_dms(s.jake) == []


def test_slack_dm_sent_once_when_enabled(setup, client, admin_headers):
    s = setup
    s.todo("jake", "Thing", "2026-10-05")
    client.put("/api/config/slackNotify", json={"enabled": True, "types": ["reminder"], "mode": "dm"}, headers=admin_headers)
    assert client.get("/api/my/prefs", headers=_h(s.team, "jake")).json()["slackReminders"] is True
    s.run(now_utc=MON)
    s.run(now_utc=MON)
    [(_, text)] = s.my_dms(s.jake)
    assert "1 To-do due today" in text and "Thing" in text


def test_spa_catch_all_is_the_last_route():
    """A GET route declared after the catch-all is silently shadowed (it serves roadmap.html instead)."""
    paths = [getattr(r, "path", "") for r in server.app.routes]
    assert paths[-1] == "/{full_path:path}"
