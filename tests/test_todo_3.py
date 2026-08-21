"""TODO-3: completing a to-do deletes its reminder notification, and the completed log becomes
per-row deletable (with undo restoring the completed state).

Two rules, both server-side and both privacy-scoped:
  Part 1 - transitioning a to-do INTO Done deletes any notification whose todo_id points at it.
  Part 2.3 - deleting a to-do deletes any surviving notification for it (rare after Part 1, but a
             to-do deleted while still open + fired can have one).

The load-bearing guard is the same as TODO-1: username from the token, in the WHERE clause of BOTH
the to-do write AND the notification cleanup. A crafted todo_id can never reach another user's
notification, because (layer 1) update_todo/delete_todo 404 a tid that is not the caller's before the
cleanup runs, and (layer 2) the cleanup DELETE itself carries `AND username=?`. See
test_cross_user_cannot_clear_anothers_notification for the falsifiable demonstration.
"""
import server


def _hdr(team, user, role="editor"):
    return {"Authorization": f"Bearer {server.create_token(team, user, role)}", "X-Team": team}


def _mk(client, hdr, title="Call the vendor", **fields):
    return client.post("/api/my/todos", json={"title": title, **fields}, headers=hdr)


def _notifs(client, hdr):
    return client.get("/api/notifications", headers=hdr).json()["notifications"]


def _todo_notif(client, hdr, tid):
    """The reminder notification pointing at to-do `tid`, or None."""
    return next((n for n in _notifs(client, hdr) if n.get("todo_id") == tid), None)


def _arm_reminder(client, hdr, title="Ship it"):
    """Create a past-due to-do and fire its reminder, returning (tid, notification-dict)."""
    tid = _mk(client, hdr, title=title, due_date="2020-01-01").json()["todo"]["id"]
    fired = client.post("/api/my/reminders/check", headers=hdr).json()
    assert fired["created"] >= 1
    n = _todo_notif(client, hdr, tid)
    assert n is not None and n["type"] == "reminder"       # armed and pointing at the to-do
    return tid, n


# ── PART 1: completing deletes the reminder notification ──────────────────────────────────────────────
def test_completing_a_todo_deletes_its_reminder_notification(client, team):
    h = _hdr(team, "alice")
    tid, _ = _arm_reminder(client, h)
    # transition INTO Done via the only path that can (PUT with status=Done - what the checkbox calls)
    assert client.put(f"/api/my/todos/{tid}", json={"status": "Done"}, headers=h).status_code == 200
    assert _todo_notif(client, h, tid) is None             # notification gone, not merely marked read


def test_completion_deletes_not_marks_read(client, team):
    h = _hdr(team, "alice")
    tid, n = _arm_reminder(client, h)
    nid = n["id"]
    client.put(f"/api/my/todos/{tid}", json={"status": "Done"}, headers=h)
    assert all(x["id"] != nid for x in _notifs(client, h))  # the row itself is gone


def test_only_that_todos_notification_is_touched(client, team):
    h = _hdr(team, "alice")
    tid_a, _ = _arm_reminder(client, h, title="A")
    tid_b, _ = _arm_reminder(client, h, title="B")
    client.put(f"/api/my/todos/{tid_a}", json={"status": "Done"}, headers=h)
    assert _todo_notif(client, h, tid_a) is None            # A cleared
    assert _todo_notif(client, h, tid_b) is not None        # B untouched


def test_completion_only_fires_on_transition_into_done(client, team):
    # Re-PUT of an already-Done to-do (no transition) must not error; nothing to clean by then.
    h = _hdr(team, "alice")
    tid, _ = _arm_reminder(client, h)
    client.put(f"/api/my/todos/{tid}", json={"status": "Done"}, headers=h)
    assert _todo_notif(client, h, tid) is None
    assert client.put(f"/api/my/todos/{tid}", json={"status": "Done"}, headers=h).status_code == 200


# ── PART 2.3: deleting a to-do clears any surviving notification ──────────────────────────────────────
def test_deleting_an_open_reminded_todo_clears_its_notification(client, team):
    h = _hdr(team, "alice")
    tid, _ = _arm_reminder(client, h)                       # still OPEN and fired
    assert client.delete(f"/api/my/todos/{tid}", headers=h).status_code == 200
    assert _todo_notif(client, h, tid) is None


# ── PART 2: undo restores the COMPLETED state, not a reopened row ─────────────────────────────────────
def test_undo_restores_completed_state_via_create(client, team):
    # The log delete's undo re-POSTs a snapshot carrying status/completed_ts/due_date. create_todo must
    # honor a provided completed_ts (restore the ORIGINAL completion time), not re-date to now.
    h = _hdr(team, "alice")
    tid = _mk(client, h, title="Done thing", due_date="2026-08-20").json()["todo"]["id"]
    done = client.put(f"/api/my/todos/{tid}", json={"status": "Done"}, headers=h).json()["todo"]
    snap = {"title": done["title"], "notes": done["notes"], "status": done["status"],
            "completed_ts": done["completed_ts"], "due_date": done["due_date"],
            "item_id": done["item_id"], "item_key": done["item_key"]}
    client.delete(f"/api/my/todos/{tid}", headers=h)        # remove from the log
    restored = client.post("/api/my/todos", json=snap, headers=h).json()["todo"]
    assert restored["status"] == "Done"                     # came back completed, not open
    assert restored["completed_ts"] == done["completed_ts"] # ORIGINAL time, not re-dated
    assert restored["due_date"] == "2026-08-20"             # date preserved
    # and it lands in the completed log, not the open list
    assert restored["id"] in {t["id"] for t in client.get("/api/my/todos?status=done", headers=h).json()["todos"]}


def test_plain_create_still_stamps_completed_ts_when_done(client, team):
    # A normal Done create with NO completed_ts still stamps one (the completed=... or now branch).
    h = _hdr(team, "alice")
    t = client.post("/api/my/todos", json={"title": "x", "status": "Done"}, headers=h).json()["todo"]
    assert t["status"] == "Done" and t["completed_ts"]


# ── PART 3: privacy - the cross-user guard on the notification cleanup ─────────────────────────────────
def test_cross_user_cannot_clear_anothers_notification(client, team):
    """A crafted todo_id from another user reaches NEITHER the to-do write NOR the notification cleanup.

    Falsifiable: this holds only because `username` is in the WHERE of the to-do write (layer 1: Bob's
    PUT/DELETE on Alice's tid 404s before the cleanup runs) AND of the notification DELETE (layer 2). Drop
    the username filter from delete_todo's `DELETE FROM todos ... WHERE id=? AND username=?` and the first
    assertion below flips to 200 and Alice's notification vanishes - the revert demo in the deliverable.
    """
    alice, bob = _hdr(team, "alice"), _hdr(team, "bob")
    a_tid, _ = _arm_reminder(client, alice, title="Alice reminder")
    # Bob completes Alice's to-do: 404 (never confirms it exists), Alice's notification survives.
    assert client.put(f"/api/my/todos/{a_tid}", json={"status": "Done"}, headers=bob).status_code == 404
    assert _todo_notif(client, alice, a_tid) is not None
    # Bob deletes Alice's to-do: 404, Alice's notification still survives.
    assert client.delete(f"/api/my/todos/{a_tid}", headers=bob).status_code == 404
    assert _todo_notif(client, alice, a_tid) is not None
    # Bob's own list/notifications are unaffected and he sees none of Alice's.
    assert all(n.get("todo_id") != a_tid for n in _notifs(client, bob))


def test_todo_operations_write_no_audit_rows(client, team, admin_headers):
    # Privacy rule 4: neither completing nor deleting a to-do writes an audit_log/activity row.
    h = _hdr(team, "alice")

    def _acts():
        r = client.get("/api/activities", headers=admin_headers).json()
        return r if isinstance(r, list) else r.get("activities", [])
    tid, _ = _arm_reminder(client, h)
    before = _acts()
    client.put(f"/api/my/todos/{tid}", json={"status": "Done"}, headers=h)
    client.delete(f"/api/my/todos/{tid}", headers=h)
    assert len(_acts()) == len(before)


def test_todos_still_excluded_from_export(client, team, admin_headers):
    # Privacy rule 5, re-confirmed after adding a cross-table cleanup.
    _mk(client, _hdr(team, "alice"), title="PRIVATE-TODO-3")
    dump = client.get("/api/export", headers=admin_headers).json()
    assert "todos" not in dump
    assert "PRIVATE-TODO-3" not in server.json.dumps(dump)


def test_no_bulk_clear_endpoint_exists(client, team):
    # Part 2.2: no bulk clear was (re-)added. The removed endpoint stays 404/405.
    h = _hdr(team, "alice")
    assert client.post("/api/my/todos/clear-completed", headers=h).status_code in (404, 405)
