"""FN1 + FN2 + FN6: a user-raised flag auto-watches the flagger and notifies the item's watchers.

Design under test (server.py _notify_on_flag, hooked into _insert_activity's INSERT branch only):
  - FN6 auto-watch: ALL FLAG_TYPES (At Risk, Blocked, Needs Decision), source='User'.
  - FN1 notify:     FLAG_NOTIFY_TYPES only (= FLAG_TYPES - {Blocked}); Blocked notifies via its status
                    path, Action Taken is audit-only and in neither list.
  - Gated on source=='User' (excludes the client rules-engine's source='System' auto-generation) and the
    allowlist, NEVER on status. Fires on INSERT only: a re-flag takes the dedupe-update branch and is silent.
  - The flagger is passed as the notify actor; _notify excludes the actor, so the flagger is never notified
    about their own flag even though FN6 just made them a watcher.

Amendments applied: A1 (pin the dedupe branch, not just absence-of-notify), A2 (name the notification
fields and assert per-field scope), A3 (pair W-has / F-has-none so a dead hook is distinguishable from a
dropped actor).
"""
import server

F = "flagger.fred"        # the actor who raises flags (identity travels via body['created_by'])
W = "watcher.wanda"       # a watcher who is NOT the flagger


def _mk(client, headers, **fields):
    body = {"name": "Item", "status": "Planned", **fields}
    return client.post("/api/projects", json=body, headers=headers).json()["id"]


def _flag(client, headers, item_id, activity_type, created_by=F, source="User",
          status="Open", name="Item"):
    return client.post("/api/activities", headers=headers, json={
        "activity_type": activity_type, "item_id": item_id, "item_name": name,
        "source": source, "created_by": created_by, "message": "m", "status": status,
    })


def _notifs_for(team, username, ntype="flag"):
    with server.db(team) as c:
        rows = c.execute("SELECT * FROM notifications WHERE username=? AND type=? ORDER BY id",
                         (username, ntype)).fetchall()
    return [dict(r) for r in rows]


def _act_count(team, item_id, activity_type):
    with server.db(team) as c:
        r = c.execute("SELECT COUNT(*) n FROM activities WHERE item_id=? AND activity_type=?",
                      (item_id, activity_type)).fetchone()
    return r["n"]


# ── Acceptance 1 (A3 paired) ────────────────────────────────────────────────────────────────────────
def test_first_user_flag_notifies_watcher_not_flagger(client, team, admin_headers):
    """Both halves in ONE test, so the three states are distinguishable:
         working      -> W has one, F has none
         dropped actor -> W has one, F ALSO has one   (first assert passes, second FAILS)
         dead hook    -> W has none, F has none        (first assert FAILS)
    Do not simplify to a single assertion - each half catches a different failure."""
    pid = _mk(client, admin_headers)
    server._add_watchers(team, pid, [W])
    assert _flag(client, admin_headers, pid, "At Risk").status_code == 200
    assert len(_notifs_for(team, W)) == 1        # dead-hook catch: the notify actually fired
    assert len(_notifs_for(team, F)) == 0        # dropped-actor catch: flagger excluded as the actor


# ── Acceptance 2 ────────────────────────────────────────────────────────────────────────────────────
def test_needs_decision_notifies_like_at_risk(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    server._add_watchers(team, pid, [W])
    _flag(client, admin_headers, pid, "Needs Decision")
    notes = _notifs_for(team, W)
    assert len(notes) == 1
    assert "Needs Decision" in notes[0]["message"]


# ── Acceptance 3 (A1: pin the dedupe branch AND the absence of a notify) ──────────────────────────────
def test_reflag_runs_dedupe_and_does_not_notify(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    server._add_watchers(team, pid, [W])
    _flag(client, admin_headers, pid, "At Risk")             # INSERT
    assert _act_count(team, pid, "At Risk") == 1
    assert len(_notifs_for(team, W)) == 1
    _flag(client, admin_headers, pid, "At Risk")             # re-flag -> dedupe-update branch
    # A1 half 1: the activities row count is UNCHANGED, proving the dedupe-update branch actually ran
    # (a wrongly-placed endpoint hook could also "not notify twice"; this rules that out).
    assert _act_count(team, pid, "At Risk") == 1
    # A1 half 2: no second notification.
    assert len(_notifs_for(team, W)) == 1


def test_dedupe_matches_a_READ_activity_not_only_open(client, team, admin_headers):
    # ACT-DEDUP-1 (the 3-in-an-hour "Needs Decision" bug): a System alert is created (Open); opening the
    # Activity Center flips it to Read (markActivitiesRead); the client rules engine then re-posts the same
    # alert. The dedup must still match a READ row, not just an Open one - otherwise every re-post after the
    # user reads it inserts a fresh duplicate. Dismissed/closed rows are deliberately NOT deduped.
    pid = _mk(client, admin_headers)
    _flag(client, admin_headers, pid, "Needs Decision", source="System", created_by="System")
    assert _act_count(team, pid, "Needs Decision") == 1
    with server.db(team) as c:                                   # user reads it -> Open becomes Read
        c.execute("UPDATE activities SET status='Read' WHERE item_id=? AND activity_type='Needs Decision'", (pid,))
    _flag(client, admin_headers, pid, "Needs Decision", source="System", created_by="System")   # rules engine re-posts
    assert _act_count(team, pid, "Needs Decision") == 1          # STILL one row - deduped against the Read one
    with server.db(team) as c:                                   # user dismisses it
        c.execute("UPDATE activities SET status='Dismissed' WHERE item_id=? AND activity_type='Needs Decision'", (pid,))
    _flag(client, admin_headers, pid, "Needs Decision", source="System", created_by="System")
    assert _act_count(team, pid, "Needs Decision") == 2          # a NEW occurrence after dismissal is fresh (not deduped)


# ── Acceptance 4 (source qualifier) ──────────────────────────────────────────────────────────────────
def test_system_source_does_not_notify_or_watch(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    server._add_watchers(team, pid, [W])
    _flag(client, admin_headers, pid, "At Risk", source="System")
    assert len(_notifs_for(team, W)) == 0
    # And the source='System' auto-generation does not auto-watch the (machine) created_by either.
    assert F not in server._get_watchers(team, pid)


# ── Acceptance 5 (Q7: Blocked does not notify via the flag hook) ─────────────────────────────────────
def test_blocked_does_not_notify_via_flag_hook(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    server._add_watchers(team, pid, [W])
    _flag(client, admin_headers, pid, "Blocked")
    # No 'flag'-type notification: Blocked notifies via its status-change path (watch_status), not here.
    assert len(_notifs_for(team, W)) == 0


# ── Acceptance 6 (Action Taken notifies nobody) ─────────────────────────────────────────────────────
def test_action_taken_notifies_nobody(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    server._add_watchers(team, pid, [W])
    _flag(client, admin_headers, pid, "Action Taken", status="Auto-Cleared")
    assert len(_notifs_for(team, W)) == 0


# ── Acceptance 7 (FN6 auto-watch for ALL flag types, incl. Blocked) ─────────────────────────────────
def test_fn6_auto_watch_all_flag_types(client, team, admin_headers):
    a = _mk(client, admin_headers)
    _flag(client, admin_headers, a, "At Risk")
    assert F in server._get_watchers(team, a)                 # notifying type watches the flagger
    b = _mk(client, admin_headers)
    _flag(client, admin_headers, b, "Blocked")
    assert F in server._get_watchers(team, b)                 # non-notifying type STILL watches the flagger


# ── Acceptance 8 (A2: name the fields, assert per-field scope) ──────────────────────────────────────
def test_flag_notification_fields_and_contributor_scope(client, team, admin_headers, contributor_headers):
    # A2 half 1: a 'flag' notification row carries exactly these content fields; assert each.
    pid = _mk(client, admin_headers, name="Payments migration")
    server._add_watchers(team, pid, [W])
    _flag(client, admin_headers, pid, "At Risk", name="Payments migration")
    n = _notifs_for(team, W)[0]
    assert set(n.keys()) >= {"username", "type", "item_id", "item_name", "message", "actor",
                             "created_ts", "read"}
    assert n["username"] == W
    assert n["type"] == "flag"
    assert n["item_id"] == pid
    assert n["item_name"] == "Payments migration"
    assert n["message"] == f"{F} flagged Payments migration as At Risk"
    assert n["actor"] == F
    assert n["read"] == 0
    assert n["created_ts"]
    # A2 half 2: item_name is the field that carries item content, so it is the leak risk. An out-of-scope
    # flag notification to a Contributor must expose NOTHING - not the item id, not the item_name.
    theirs = _mk(client, admin_headers, name="SECRET-TITLE", assignee="other", dev="OtherPod")
    server._notify(team, ["contrib1"], "flag", theirs, "SECRET-TITLE",
                   f"editor1 flagged SECRET-TITLE as At Risk", "editor1")
    notes = client.get("/api/notifications", headers=contributor_headers).json()["notifications"]
    assert all(x.get("item_id") != theirs for x in notes)
    assert all(x.get("item_name") != "SECRET-TITLE" for x in notes)


# ── Endpoint role gate (Contributors cannot flag at all) ────────────────────────────────────────────
def test_contributor_cannot_post_a_flag(client, team, admin_headers, contributor_headers):
    pid = _mk(client, admin_headers, assignee="contrib1")
    r = _flag(client, contributor_headers, pid, "At Risk")
    assert r.status_code == 403          # POST /api/activities is admin/editor only
