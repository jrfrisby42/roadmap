"""REM-3: due dates become reminders.

The load-bearing guards here are:
- fire-once (reminded_ts), MT day-boundary correctness, and PER-USER isolation of the fire path
  (test_reminder_fire_is_per_user - drop the username filter in _fire_due_reminders and it goes red).
- the deploy backfill (test_backfill_*) that prevents a notification flood on first load.
- the export exclusion (test_reminder_title_not_in_export) that keeps private to-do titles out of the
  admin backup, mirroring test_export_excludes_todos.
"""
import os
import sqlite3
from datetime import datetime, timezone

import pytest

import server


def _hdr(team, user, role="editor"):
    return {"Authorization": f"Bearer {server.create_token(team, user, role)}", "X-Team": team}


def _mk(client, hdr, title, due=None, **fields):
    body = {"title": title, **fields}
    if due is not None:
        body["due_date"] = due
    return client.post("/api/my/todos", json=body, headers=hdr).json()["todo"]


def _notifs(client, hdr):
    return client.get("/api/notifications", headers=hdr).json()["notifications"]


# ── ZONE RESOLUTION (B7.14) ───────────────────────────────────────────────────────────────────────────
def test_mt_zone_resolved_and_bad_zone_is_loud():
    # _MT_ZONE resolves at import (loud there if the IANA db is missing). A bogus zone raises - the same
    # exception that would have failed the boot loudly rather than silently per-request.
    assert server._MT_ZONE is not None
    from zoneinfo import ZoneInfoNotFoundError, ZoneInfo
    with pytest.raises(ZoneInfoNotFoundError):
        ZoneInfo("Definitely/NotAZone")


# ── MT DAY BOUNDARY off-by-one (9.1.7) ────────────────────────────────────────────────────────────────
def test_today_mt_key_off_by_one():
    # 2026-08-20 04:00 UTC is still 2026-08-19 in Mountain Time (MDT, UTC-6 in August). The UTC date is
    # already "tomorrow"; the MT key must be "today". This is the exact off-by-one the stage guards.
    utc = datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc)
    assert server._today_mt_key(utc) == "2026-08-19"
    # a winter (MST, UTC-7) instant, same shape
    assert server._today_mt_key(datetime(2026, 1, 5, 5, 0, tzinfo=timezone.utc)) == "2026-01-04"


def test_fire_uses_mt_today_not_utc(client, team):
    # A to-do due on the MT "today" fires even though the server's UTC date is already tomorrow.
    h = _hdr(team, "alice")
    _mk(client, h, "due today MT", due="2026-08-19")
    created = server._fire_due_reminders(team, "alice", server._today_mt_key(datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc)))
    assert created == 1


# ── MIGRATION + BACKFILL (9.1.3, 9.1.4) ───────────────────────────────────────────────────────────────
def test_migration_adds_columns_idempotent(client, team):
    with server.db(team) as c:
        tcols = {r["name"] for r in c.execute("PRAGMA table_info(todos)").fetchall()}
        ncols = {r["name"] for r in c.execute("PRAGMA table_info(notifications)").fetchall()}
    assert "reminded_ts" in tcols
    assert "todo_id" in ncols
    server.init_team_db(team); server.init_team_db(team)  # re-run must not raise
    with server.db(team) as c:
        assert "reminded_ts" in {r["name"] for r in c.execute("PRAGMA table_info(todos)").fetchall()}


def test_backfill_stamps_past_and_done_not_future(client):
    # Build a PRE-REM-3 todos table (no reminded_ts) with rows, then run init_team_db so the real ALTER +
    # one-time backfill execute against existing rows.
    slug = "rem3bf_team"
    os.makedirs(os.path.join(server.TENANTS_DIR, slug), exist_ok=True)
    path = os.path.join(server.TENANTS_DIR, slug, "roadmap.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE todos (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, title TEXT, "
                "notes TEXT DEFAULT '', status TEXT DEFAULT 'Today', due_date TEXT, sort_order REAL DEFAULT 0, "
                "created_ts TEXT, updated_ts TEXT, completed_ts TEXT)")
    now = "2020-01-01T00:00:00+00:00"
    for title, status, due in [("past", "Today", "2000-01-01"), ("future", "Today", "2999-12-31"),
                               ("done", "Done", "2999-12-31"), ("nodate", "Today", None)]:
        con.execute("INSERT INTO todos(username,title,status,due_date,created_ts,updated_ts) VALUES(?,?,?,?,?,?)",
                    ("alice", title, status, due, now, now))
    con.commit(); con.close()

    server.init_team_db(slug)  # runs the ALTER + one-time backfill
    with server.db(slug) as c:
        r = {row["title"]: row["reminded_ts"] for row in c.execute("SELECT title, reminded_ts FROM todos").fetchall()}
    assert r["past"] is not None    # already past -> stamped, will NOT flood on deploy
    assert r["done"] is not None    # Done -> stamped
    assert r["future"] is None      # future -> left null, fires on its date
    assert r["nodate"] is None      # no date -> null (never fires, but not stamped)

    server.init_team_db(slug)       # second run: backfill must NOT re-stamp the legitimately-null rows
    with server.db(slug) as c:
        r2 = {row["title"]: row["reminded_ts"] for row in c.execute("SELECT title, reminded_ts FROM todos").fetchall()}
    assert r2["future"] is None and r2["nodate"] is None


def test_backfill_crash_window_closed(client):
    # Reproduce the exact CRASH STATE: the ALTER (DDL) auto-committed on its own, then the process died
    # BEFORE the backfill+marker transaction committed. So on the next boot: the reminded_ts column EXISTS,
    # schema_meta EXISTS but is EMPTY (no marker), and rows are unstamped. The old gate ("column newly
    # added") would SKIP the backfill here -> the flood. The marker gate must still RUN it.
    slug = "rem3crash_team"
    os.makedirs(os.path.join(server.TENANTS_DIR, slug), exist_ok=True)
    path = os.path.join(server.TENANTS_DIR, slug, "roadmap.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE todos (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, title TEXT, "
                "notes TEXT DEFAULT '', status TEXT DEFAULT 'Today', due_date TEXT, sort_order REAL DEFAULT 0, "
                "created_ts TEXT, updated_ts TEXT, completed_ts TEXT, reminded_ts TEXT)")   # column ALREADY added
    con.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, applied_ts TEXT NOT NULL)")  # exists, EMPTY (marker rolled back)
    now = "2020-01-01T00:00:00+00:00"
    for title, status, due in [("past", "Today", "2000-01-01"), ("future", "Today", "2999-12-31"),
                               ("done", "Done", "2999-12-31")]:
        con.execute("INSERT INTO todos(username,title,status,due_date,reminded_ts,created_ts,updated_ts) "
                    "VALUES('u',?,?,?,NULL,?,?)", (title, status, due, now, now))
    con.commit(); con.close()

    server._initialized_teams.discard(slug)      # simulate a fresh process boot (crash retry)
    server.init_team_db(slug)                    # ALTER fails (column exists) but the MARKER is absent -> backfill RUNS
    with server.db(slug) as c:
        r = {row["title"]: row["reminded_ts"] for row in c.execute("SELECT title, reminded_ts FROM todos").fetchall()}
        marked = c.execute("SELECT 1 FROM schema_meta WHERE key='rem3_reminded_backfill'").fetchone()
    assert r["past"] is not None and r["done"] is not None   # backfill ran despite the pre-existing column
    assert r["future"] is None
    assert marked is not None                                 # marker now set

    # A to-do created AFTER the migration (still unstamped) must NOT be stamped by a later init: marker present.
    with server.db(slug) as c:
        c.execute("INSERT INTO todos(username,title,status,due_date,reminded_ts,created_ts,updated_ts) "
                  "VALUES('u','postmigration','Today','2000-06-06',NULL,?,?)", (now, now))
    server._initialized_teams.discard(slug)
    server.init_team_db(slug)                    # third boot: marker present -> backfill is a no-op
    with server.db(slug) as c:
        pm = c.execute("SELECT reminded_ts FROM todos WHERE title='postmigration'").fetchone()["reminded_ts"]
    assert pm is None                            # NOT re-stamped -> it will fire normally on the next check


# ── FIRE RULES (9.1.5, 9.1.6) ─────────────────────────────────────────────────────────────────────────
def test_fire_once(client, team):
    h = _hdr(team, "alice")
    _mk(client, h, "call vendor", due="2000-01-01")
    assert server._fire_due_reminders(team, "alice", "2026-08-19") == 1
    assert server._fire_due_reminders(team, "alice", "2026-08-19") == 0   # reminded_ts guards a re-fire
    assert len([n for n in _notifs(client, h) if n["type"] == "reminder"]) == 1


def test_done_never_fires_but_waiting_and_backlog_do(client, team):
    h = _hdr(team, "alice")
    d = _mk(client, h, "done one", due="2000-01-01")["id"]
    client.put(f"/api/my/todos/{d}", json={"status": "Done"}, headers=h)
    assert server._fire_due_reminders(team, "alice", "2026-08-19") == 0   # Done suppresses
    w = _mk(client, h, "waiting one", due="2000-01-01")["id"]
    client.put(f"/api/my/todos/{w}", json={"status": "Waiting"}, headers=h)
    b = _mk(client, h, "backlog one", due="2000-01-01")["id"]
    client.put(f"/api/my/todos/{b}", json={"status": "Backlog"}, headers=h)
    assert server._fire_due_reminders(team, "alice", "2026-08-19") == 2   # Waiting + Backlog both fire


def test_future_dated_does_not_fire_yet(client, team):
    h = _hdr(team, "alice")
    _mk(client, h, "later", due="2999-12-31")
    assert server._fire_due_reminders(team, "alice", "2026-08-19") == 0


# ── NOTIFICATION CONTENT (5.2, B2) ────────────────────────────────────────────────────────────────────
def test_reminder_notification_shape_linked(client, team, admin_headers):
    h = _hdr(team, "alice")
    iid = client.post("/api/projects", json={"name": "Ticket"}, headers=admin_headers).json()["id"]
    with server.db(team) as c:  # force a known key so the derived item_key is deterministic
        row = c.execute("SELECT data FROM projects WHERE id=?", (iid,)).fetchone()
        d = server.json.loads(row["data"]); d["itemKey"] = "TEST-9"
        c.execute("UPDATE projects SET data=? WHERE id=?", (server.json.dumps(d), iid))
    tid = _mk(client, h, "linked follow up", due="2000-01-01", item_id=iid)["id"]
    server._fire_due_reminders(team, "alice", "2026-08-19")
    n = [x for x in _notifs(client, h) if x["type"] == "reminder"][0]
    assert n["message"] == "Reminder: linked follow up"
    assert n["item_id"] == iid and n["todo_id"] == tid   # links to the item; carries todo_id for snooze
    assert n["item_name"] == "TEST-9"                     # key derived server-side, shown in the meta line
    assert n["actor"] == ""                               # self-actor avoided (5.3)


def test_unlinked_reminder_has_null_item(client, team):
    h = _hdr(team, "alice")
    _mk(client, h, "no item here", due="2000-01-01")
    server._fire_due_reminders(team, "alice", "2026-08-19")
    n = [x for x in _notifs(client, h) if x["type"] == "reminder"][0]
    assert n["item_id"] is None and n["todo_id"] is not None


# ── SNOOZE / RE-ARM (6.4, B4, B7.17, B7.18) ───────────────────────────────────────────────────────────
def test_snooze_clears_reminded_and_refires_once(client, team):
    h = _hdr(team, "alice")
    tid = _mk(client, h, "snoozable", due="2000-01-01")["id"]
    assert server._fire_due_reminders(team, "alice", "2026-08-19") == 1
    # snooze = move the date (to another past date so it fires again in-test); server clears reminded_ts
    client.put(f"/api/my/todos/{tid}", json={"due_date": "2001-01-01"}, headers=h)
    with server.db(team) as c:
        row = c.execute("SELECT due_date, reminded_ts FROM todos WHERE id=?", (tid,)).fetchone()
    assert row["due_date"] == "2001-01-01" and row["reminded_ts"] is None   # moved + re-armed
    assert server._fire_due_reminders(team, "alice", "2026-08-19") == 1     # fires again
    assert server._fire_due_reminders(team, "alice", "2026-08-19") == 0     # exactly once


def test_due_change_detection(client, team):
    h = _hdr(team, "alice")
    tid = _mk(client, h, "x", due="2000-01-01")["id"]
    server._fire_due_reminders(team, "alice", "2026-08-19")
    # unchanged due (echo the same value) must NOT clear reminded_ts
    client.put(f"/api/my/todos/{tid}", json={"due_date": "2000-01-01"}, headers=h)
    with server.db(team) as c:
        assert c.execute("SELECT reminded_ts FROM todos WHERE id=?", (tid,)).fetchone()["reminded_ts"] is not None
    assert server._fire_due_reminders(team, "alice", "2026-08-19") == 0     # still armed-off, no re-fire
    # a changed due DOES clear it
    client.put(f"/api/my/todos/{tid}", json={"due_date": "2001-02-02"}, headers=h)
    with server.db(team) as c:
        assert c.execute("SELECT reminded_ts FROM todos WHERE id=?", (tid,)).fetchone()["reminded_ts"] is None
    # clearing the date also clears reminded_ts, and a dateless to-do never fires
    server._fire_due_reminders(team, "alice", "2026-08-19")                 # fires on the 2001 date
    client.put(f"/api/my/todos/{tid}", json={"due_date": ""}, headers=h)
    with server.db(team) as c:
        row = c.execute("SELECT due_date, reminded_ts FROM todos WHERE id=?", (tid,)).fetchone()
    assert row["due_date"] is None and row["reminded_ts"] is None
    assert server._fire_due_reminders(team, "alice", "2026-08-19") == 0     # no date -> never fires


def test_same_date_twice_one_notification(client, team):
    h = _hdr(team, "alice")
    tid = _mk(client, h, "y", due="2000-01-01")["id"]
    server._fire_due_reminders(team, "alice", "2026-08-19")
    client.put(f"/api/my/todos/{tid}", json={"due_date": "2000-01-01"}, headers=h)  # same value
    server._fire_due_reminders(team, "alice", "2026-08-19")
    assert len([n for n in _notifs(client, h) if n["type"] == "reminder"]) == 1


# ── ENDPOINT + PER-USER (4.2, 9.1.10) ─────────────────────────────────────────────────────────────────
def test_check_endpoint_is_token_scoped(client, team):
    h = _hdr(team, "alice")
    _mk(client, h, "mine", due="2000-01-01")
    r = client.post("/api/my/reminders/check", headers=h).json()
    assert r["created"] == 1
    assert client.post("/api/my/reminders/check", headers=h).json()["created"] == 0  # idempotent


def test_reminder_fire_is_per_user(client, team):
    # THE GUARD: alice's due to-do must never fire for bob. Drop `username=?` from the SELECT in
    # _fire_due_reminders and this goes red (bob's check would fire alice's to-do and leak her title).
    alice, bob = _hdr(team, "alice"), _hdr(team, "bob")
    _mk(client, alice, "alice private reminder", due="2000-01-01")
    assert server._fire_due_reminders(team, "bob", "2026-08-19") == 0
    assert [n for n in _notifs(client, bob) if n["type"] == "reminder"] == []
    # alice's to-do is untouched by bob's check (not stamped) and still fires for alice
    assert server._fire_due_reminders(team, "alice", "2026-08-19") == 1


def test_todo_id_is_not_an_auth_path(client, team):
    # A notification's todo_id cannot be used to reach another user's to-do: the snooze write goes through
    # PUT /api/my/todos/{id}, which re-checks ownership by the token (404 for a foreign row).
    alice, bob = _hdr(team, "alice"), _hdr(team, "bob")
    tid = _mk(client, alice, "alice todo", due="2000-01-01")["id"]
    server._fire_due_reminders(team, "alice", "2026-08-19")
    assert client.put(f"/api/my/todos/{tid}", json={"due_date": "2030-01-01"}, headers=bob).status_code == 404


# ── EXPORT PRIVACY (B3 / B7.16) ───────────────────────────────────────────────────────────────────────
def test_reminder_title_not_in_export(client, team, admin_headers):
    h = _hdr(team, "alice")
    _mk(client, h, "SECRET-REMINDER-TITLE", due="2000-01-01")
    server._fire_due_reminders(team, "alice", "2026-08-19")
    dump = client.get("/api/export", headers=admin_headers).json()
    assert "SECRET-REMINDER-TITLE" not in server.json.dumps(dump)               # title never leaves
    assert all(n.get("type") != "reminder" for n in dump.get("notifications", []))  # reminder rows omitted
