"""TODO-RECUR-1 - recurring to-dos.

Completing a recurring to-do SPAWNS its next occurrence (spawn-on-completion, same model
as items) inside update_todo's Done transition. Next due = prev_due + period, skipping
fully-elapsed cycles into the current window (addendum A2). monthly = 30 days flat (A1).
The spawn is atomic with the completion (A5) and owned by the token's user (privacy rule 2).
A recurring to-do with no due date does not spawn and does not error (A4).
"""
import datetime

import server


def _hdr(team, user, role="editor"):
    return {"Authorization": f"Bearer {server.create_token(team, user, role)}", "X-Team": team}


def _mk(client, hdr, **fields):
    return client.post("/api/my/todos", json=fields, headers=hdr).json()["todo"]


def _open(client, hdr):
    return client.get("/api/my/todos?status=open", headers=hdr).json()["todos"]


def _complete(client, hdr, tid):
    return client.put(f"/api/my/todos/{tid}", json={"status": "Done"}, headers=hdr)


def _today():
    return datetime.date.today()


def _iso(d):
    return d.isoformat()


def test_complete_spawns_next_weekly(client, team):
    h = _hdr(team, "alice")
    t = _mk(client, h, title="Backups", due_date=_iso(_today()), recurrence="weekly")
    _complete(client, h, t["id"])
    opens = _open(client, h)
    assert len(opens) == 1                       # completed row left the open list; successor is here
    s = opens[0]
    assert s["id"] != t["id"]
    assert s["recurrence"] == "weekly"
    assert s["due_date"] == _iso(_today() + datetime.timedelta(days=7))
    assert s["reminded_ts"] is None and s["completed_ts"] is None
    assert s["status"] != "Done"


def test_next_due_is_prev_due_plus_period_not_completion_date(client, team):
    # Due 3 days ago; completed TODAY. Next must be due+7, NOT today+7.
    h = _hdr(team, "alice")
    due = _today() - datetime.timedelta(days=3)
    t = _mk(client, h, title="x", due_date=_iso(due), recurrence="weekly")
    _complete(client, h, t["id"])
    s = _open(client, h)[0]
    assert s["due_date"] == _iso(due + datetime.timedelta(days=7))           # prev_due + period
    assert s["due_date"] != _iso(_today() + datetime.timedelta(days=7))      # NOT completion date + period


def test_skip_elapsed_ten_weeks_late(client, team):
    # 10 weeks overdue: single-step would spawn 9 weeks overdue; skip-elapsed lands in the current window.
    h = _hdr(team, "alice")
    due = _today() - datetime.timedelta(days=70)
    t = _mk(client, h, title="x", due_date=_iso(due), recurrence="weekly")
    _complete(client, h, t["id"])
    s = _open(client, h)[0]
    assert s["due_date"] == _iso(_today())                                   # current window, not overdue


def test_monthly_is_30_days(client, team):
    h = _hdr(team, "alice")
    t = _mk(client, h, title="x", due_date=_iso(_today()), recurrence="monthly")
    _complete(client, h, t["id"])
    s = _open(client, h)[0]
    assert s["due_date"] == _iso(_today() + datetime.timedelta(days=30))


def test_recurrence_none_does_not_spawn(client, team):
    h = _hdr(team, "alice")
    t = _mk(client, h, title="x", due_date=_iso(_today()), recurrence="none")
    _complete(client, h, t["id"])
    assert _open(client, h) == []


def test_recurring_without_due_does_not_spawn_or_error(client, team):
    # A4: recurrence with no due date has no computable next date -> fail quiet (200, no spawn), not 500.
    h = _hdr(team, "alice")
    t = _mk(client, h, title="x", recurrence="weekly")
    r = _complete(client, h, t["id"])
    assert r.status_code == 200
    assert _open(client, h) == []


def test_spawned_row_owned_by_completer_not_cross_user(client, team):
    # Privacy rule 2: completing user A's recurring to-do can never create a row owned by user B.
    alice, bob = _hdr(team, "alice"), _hdr(team, "bob")
    t = _mk(client, alice, title="secret", due_date=_iso(_today()), recurrence="weekly")
    assert _complete(client, bob, t["id"]).status_code == 404      # bob cannot complete alice's -> no spawn
    assert _open(client, bob) == []
    _complete(client, alice, t["id"])
    assert len(_open(client, alice)) == 1                          # successor is alice's
    assert _open(client, bob) == []                                # nothing leaked to bob


def test_undo_after_delete_does_not_spawn(client, team):
    # Undo re-POSTs the snapshot through create_todo (which has no spawn path) -> one row, not two.
    h = _hdr(team, "alice")
    t = _mk(client, h, title="x", due_date=_iso(_today()), recurrence="weekly")
    client.delete(f"/api/my/todos/{t['id']}", headers=h)
    restored = client.post("/api/my/todos",
                           json={"title": "x", "due_date": _iso(_today()), "recurrence": "weekly"},
                           headers=h).json()["todo"]
    assert restored["recurrence"] == "weekly"                       # recurrence preserved on restore
    assert len(_open(client, h)) == 1                               # no spawned duplicate


def test_invalid_recurrence_rejected(client, team):
    h = _hdr(team, "alice")
    assert client.post("/api/my/todos", json={"title": "x", "recurrence": "daily"}, headers=h).status_code == 400


def test_create_persists_recurrence(client, team):
    h = _hdr(team, "alice")
    t = _mk(client, h, title="x", recurrence="biweekly")
    assert t["recurrence"] == "biweekly"


def test_todos_excluded_from_export(client, team):
    # Privacy rule 5: to-dos never appear in /api/export.
    h = _hdr(team, "alice", "admin")
    _mk(client, h, title="secret todo", recurrence="weekly")
    exp = client.get("/api/export", headers=h)
    assert exp.status_code == 200
    assert "todos" not in exp.json()
