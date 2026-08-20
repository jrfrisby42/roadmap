"""Stage 3b — notifications, @mentions, watchers. Server-side generation hooked
into the real write paths; per-user private inbox; watchers in their own table
(NOT the item blob, which update_project replaces). Asserts generation, self-
suppression, mark-read, watch/unwatch, and the blob-replace regression guard."""
import json
import server


def _hdr(team, username, role="editor"):
    return {"Authorization": f"Bearer {server.create_token(team, username, role)}", "X-Team": team}


def _add_user(team, username, role="editor"):
    """Append a user to the team's config 'users' list so mentions resolve."""
    with server.db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
        users = json.loads(row["value"]) if row else []
        users.append({"username": username, "role": role})
        c.execute("INSERT INTO config(key,value) VALUES('users',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(users),))


def _mk(client, headers, **fields):
    return client.post("/api/projects", json={"name": "Item", "status": "Planned", **fields},
                       headers=headers).json()["id"]


def _notifs(client, team, username, role="editor"):
    return client.get("/api/notifications", headers=_hdr(team, username, role)).json()


# ── mention parsing helpers (pure) ────────────────────────────────────────────

def test_mention_parse_helpers():
    valid = {"bob", "alice"}
    assert server._parse_mentions_text("hey @bob and @carol", valid) == {"bob"}
    assert server._parse_mentions_html('x <span data-u="alice">@alice</span> <span data-u="zzz">@zzz</span>', valid) == {"alice"}


# ── comment @mentions ─────────────────────────────────────────────────────────

def test_comment_mention_notifies_and_watches(client, team, admin_headers):
    _add_user(team, "bob")
    pid = _mk(client, admin_headers)
    r = client.post("/api/comments", json={"item_id": pid, "body": "please look @bob"}, headers=admin_headers)
    assert r.status_code == 200
    nd = _notifs(client, team, "bob")
    assert nd["unread"] == 1
    assert nd["notifications"][0]["type"] == "mention" and nd["notifications"][0]["item_id"] == pid
    # mentioned user auto-watches
    assert "bob" in client.get(f"/api/items/{pid}/watchers", headers=admin_headers).json()["watchers"]


def test_self_mention_suppressed(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    client.post("/api/comments", json={"item_id": pid, "body": "note to self @admin"}, headers=admin_headers)
    assert _notifs(client, team, "admin", "admin")["unread"] == 0


def test_watch_comment_notifies_watchers(client, team, admin_headers):
    _add_user(team, "bob")
    pid = _mk(client, admin_headers)
    client.post(f"/api/items/{pid}/watch", headers=_hdr(team, "bob"))
    client.post("/api/comments", json={"item_id": pid, "body": "an update"}, headers=admin_headers)
    nd = _notifs(client, team, "bob")
    assert nd["unread"] == 1 and nd["notifications"][0]["type"] == "watch_comment"


# ── assignment ────────────────────────────────────────────────────────────────

def test_assignment_notifies_assignee(client, team, admin_headers):
    _add_user(team, "bob")
    pid = _mk(client, admin_headers)
    client.put(f"/api/projects/{pid}", json={"name": "Item", "status": "Planned", "assignee": "bob"},
               headers=admin_headers)
    nd = _notifs(client, team, "bob")
    assert nd["unread"] == 1 and nd["notifications"][0]["type"] == "assigned"
    assert "bob" in client.get(f"/api/items/{pid}/watchers", headers=admin_headers).json()["watchers"]


def test_self_assignment_suppressed(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    client.put(f"/api/projects/{pid}", json={"name": "Item", "status": "Planned", "assignee": "admin"},
               headers=admin_headers)
    assert _notifs(client, team, "admin", "admin")["unread"] == 0


# ── status change → watchers ──────────────────────────────────────────────────

def test_status_change_notifies_watchers_not_actor(client, team, admin_headers):
    _add_user(team, "bob")
    pid = _mk(client, admin_headers)
    client.post(f"/api/items/{pid}/watch", headers=_hdr(team, "bob"))
    client.post(f"/api/items/{pid}/watch", headers=admin_headers)   # actor also watches
    client.put(f"/api/projects/{pid}", json={"name": "Item", "status": "In Progress"}, headers=admin_headers)
    assert _notifs(client, team, "bob")["unread"] == 1                    # watcher notified
    assert _notifs(client, team, "admin", "admin")["unread"] == 0         # actor suppressed


# ── condition A regression: watchers survive a full-blob PUT ──────────────────

def test_watchers_survive_full_blob_put(client, team, admin_headers):
    _add_user(team, "bob")
    pid = _mk(client, admin_headers)
    client.post(f"/api/items/{pid}/watch", headers=_hdr(team, "bob"))
    # update_project replaces the whole item blob from the body (no watchers field sent)
    client.put(f"/api/projects/{pid}", json={"name": "Renamed", "status": "Planned"}, headers=admin_headers)
    assert "bob" in client.get(f"/api/items/{pid}/watchers", headers=admin_headers).json()["watchers"]


# ── watch / unwatch + mark read ───────────────────────────────────────────────

def test_watch_unwatch_toggle(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    assert client.post(f"/api/items/{pid}/watch", headers=admin_headers).json()["watching"] is True
    assert client.get(f"/api/items/{pid}/watchers", headers=admin_headers).json()["watching"] is True
    assert client.post(f"/api/items/{pid}/unwatch", headers=admin_headers).json()["watching"] is False
    assert client.get(f"/api/items/{pid}/watchers", headers=admin_headers).json()["watching"] is False


def test_mark_read_one_and_all(client, team, admin_headers):
    _add_user(team, "bob")
    pid = _mk(client, admin_headers)
    client.post("/api/comments", json={"item_id": pid, "body": "@bob 1"}, headers=admin_headers)
    client.post("/api/comments", json={"item_id": pid, "body": "@bob 2"}, headers=admin_headers)
    bob = _hdr(team, "bob")
    nd = client.get("/api/notifications", headers=bob).json()
    assert nd["unread"] == 2
    first_id = nd["notifications"][0]["id"]
    assert client.post("/api/notifications/read", json={"id": first_id}, headers=bob).json()["unread"] == 1
    assert client.post("/api/notifications/read", json={"all": True}, headers=bob).json()["unread"] == 0


def test_create_with_assignee_notifies(client, team, admin_headers):
    _add_user(team, "bob")
    _mk(client, admin_headers, assignee="bob")
    nd = _notifs(client, team, "bob")
    assert nd["unread"] == 1 and nd["notifications"][0]["type"] == "assigned"


def test_notifications_are_private_per_user(client, team, admin_headers):
    _add_user(team, "bob")
    _add_user(team, "carol")
    pid = _mk(client, admin_headers)
    client.post("/api/comments", json={"item_id": pid, "body": "@bob only"}, headers=admin_headers)
    assert _notifs(client, team, "bob")["unread"] == 1
    assert _notifs(client, team, "carol")["unread"] == 0


# ── BELL-1: notification deletion (per-row delete, clear-read, restore) ──────────────────────────────────
def _make_notif(team, user, msg="hello", read=0):
    # Notifications are server-generated in prod; inserted directly here so the delete/clear tests have rows.
    with server.db(team) as c:
        cur = c.execute(
            "INSERT INTO notifications(username,type,item_id,item_name,message,actor,created_ts,read)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (user, "mention", None, "", msg, "", "2020-01-01T00:00:00+00:00", read))
        return cur.lastrowid


def test_delete_own_notification(client, team):
    nid = _make_notif(team, "alice")
    a = _hdr(team, "alice")
    assert client.delete(f"/api/notifications/{nid}", headers=a).status_code == 200
    assert all(n["id"] != nid for n in client.get("/api/notifications", headers=a).json()["notifications"])


def test_delete_notification_cross_user_rejected(client, team):
    # THE GUARD: bob must not be able to delete alice's notification. Remove `AND username=?` from the DELETE
    # (and the ownership SELECT) and this goes red - bob's delete would 200 and remove alice's row.
    nid = _make_notif(team, "alice")
    assert client.delete(f"/api/notifications/{nid}", headers=_hdr(team, "bob")).status_code == 404
    assert any(n["id"] == nid for n in client.get("/api/notifications", headers=_hdr(team, "alice")).json()["notifications"])


def test_clear_read_deletes_only_read(client, team):
    r1 = _make_notif(team, "alice", read=1); r2 = _make_notif(team, "alice", read=1); u1 = _make_notif(team, "alice", read=0)
    a = _hdr(team, "alice")
    res = client.post("/api/notifications/clear-read", headers=a).json()
    assert res["cleared"] == 2
    ids = {n["id"] for n in client.get("/api/notifications", headers=a).json()["notifications"]}
    assert u1 in ids and r1 not in ids and r2 not in ids   # unread survives, read gone


def test_clear_read_is_per_user(client, team):
    _make_notif(team, "alice", read=1); bobr = _make_notif(team, "bob", read=1)
    client.post("/api/notifications/clear-read", headers=_hdr(team, "alice"))
    assert any(n["id"] == bobr for n in client.get("/api/notifications", headers=_hdr(team, "bob")).json()["notifications"])


def test_delete_returns_row_and_restore_reinserts(client, team):
    nid = _make_notif(team, "alice", msg="restore me")
    a = _hdr(team, "alice")
    deleted = client.delete(f"/api/notifications/{nid}", headers=a).json()["deleted"]
    assert deleted["message"] == "restore me"
    r = client.post("/api/notifications/restore", json=deleted, headers=a).json()
    assert r["notification"]["message"] == "restore me"
    assert any(n["message"] == "restore me" for n in client.get("/api/notifications", headers=a).json()["notifications"])


def test_restore_forces_caller_username(client, team):
    # A body username is ignored: a restore only ever lands in the caller's own inbox.
    r = client.post("/api/notifications/restore",
                    json={"type": "mention", "message": "x", "username": "bob"},
                    headers=_hdr(team, "alice")).json()
    assert r["notification"]["username"] == "alice"
