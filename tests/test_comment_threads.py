"""Stage 4 — single-level comment threads.

`comments.parent_id` (NULL = top-level). add_comment normalizes a reply's parent to
the thread ROOT (reply-to-reply collapses to the top comment) and ignores a parent on
another item. A reply notifies the parent comment's author (self-suppressed); mentions
still notify. Deleting a top-level comment cascade-deletes its replies (no orphans).
No new endpoint — POST /api/comments takes an optional parent_id.
"""
import json

import server


def _hdr(team, username, role="editor"):
    return {"Authorization": f"Bearer {server.create_token(team, username, role)}", "X-Team": team}


def _add_user(team, username, role="editor"):
    with server.db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
        users = json.loads(row["value"]) if row else []
        users.append({"username": username, "role": role})
        c.execute("INSERT INTO config(key,value) VALUES('users',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(users),))


def _mk(client, headers):
    return client.post("/api/projects", json={"name": "Item", "status": "Planned"},
                       headers=headers).json()["id"]


def _post(client, headers, pid, body, parent_id=None):
    payload = {"item_id": pid, "body": body}
    if parent_id is not None:
        payload["parent_id"] = parent_id
    return client.post("/api/comments", json=payload, headers=headers).json()


def _comments(client, headers, pid):
    return client.get(f"/api/comments/{pid}", headers=headers).json()


def _notifs(client, team, username):
    return client.get("/api/notifications", headers=_hdr(team, username)).json()


def test_new_comment_is_top_level(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    cid = _post(client, admin_headers, pid, "top")["id"]
    rows = _comments(client, admin_headers, pid)
    assert rows[0]["id"] == cid and rows[0]["parent_id"] is None


def test_reply_stores_parent_id(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    top = _post(client, admin_headers, pid, "top")["id"]
    r = _post(client, admin_headers, pid, "reply", parent_id=top)
    assert r["parent_id"] == top


def test_reply_to_reply_normalizes_to_root(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    top = _post(client, admin_headers, pid, "top")["id"]
    rep = _post(client, admin_headers, pid, "r1", parent_id=top)["id"]
    rep2 = _post(client, admin_headers, pid, "r2", parent_id=rep)   # reply-to-reply
    assert rep2["parent_id"] == top          # collapsed to the root, not rep


def test_cross_item_parent_becomes_top_level(client, team, admin_headers):
    p1 = _mk(client, admin_headers)
    p2 = _mk(client, admin_headers)
    top = _post(client, admin_headers, p1, "on p1")["id"]
    r = _post(client, admin_headers, p2, "reply across items", parent_id=top)
    assert r["parent_id"] is None


def test_reply_notifies_parent_author(client, team, admin_headers):
    _add_user(team, "bob"); _add_user(team, "alice")
    pid = _mk(client, admin_headers)
    top = _post(client, _hdr(team, "bob"), pid, "bob's top")["id"]
    _post(client, _hdr(team, "alice"), pid, "alice reply", parent_id=top)
    nd = _notifs(client, team, "bob")
    assert any(n["type"] == "reply" and n["item_id"] == pid for n in nd["notifications"])


def test_reply_to_own_comment_no_self_notify(client, team, admin_headers):
    _add_user(team, "bob")
    pid = _mk(client, admin_headers)
    top = _post(client, _hdr(team, "bob"), pid, "bob's top")["id"]
    _post(client, _hdr(team, "bob"), pid, "bob self-reply", parent_id=top)
    nd = _notifs(client, team, "bob")
    assert not any(n["type"] == "reply" for n in nd["notifications"])


def test_mention_in_reply_still_notifies(client, team, admin_headers):
    _add_user(team, "bob"); _add_user(team, "carol")
    pid = _mk(client, admin_headers)
    top = _post(client, _hdr(team, "bob"), pid, "top")["id"]
    _post(client, admin_headers, pid, "hey @carol", parent_id=top)
    nd = _notifs(client, team, "carol")
    assert any(n["type"] == "mention" for n in nd["notifications"])


def test_cascade_delete_removes_replies(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    top = _post(client, admin_headers, pid, "top")["id"]
    _post(client, admin_headers, pid, "r1", parent_id=top)
    _post(client, admin_headers, pid, "r2", parent_id=top)
    assert len(_comments(client, admin_headers, pid)) == 3
    client.delete(f"/api/comments/{top}", headers=admin_headers)
    assert _comments(client, admin_headers, pid) == []   # parent + both replies gone


# ── 4.13.0: delete-comment ownership (editors delete only their own; admins any) ──
def test_editor_cannot_delete_others_comment(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    # editor1 posts a comment (author is forced to the authenticated poster server-side)
    cid = _post(client, _hdr(team, "editor1", "editor"), pid, "mine")["id"]
    # a DIFFERENT editor cannot delete it
    r = client.delete(f"/api/comments/{cid}", headers=_hdr(team, "editor2", "editor"))
    assert r.status_code == 403
    # the author can delete their own
    assert client.delete(f"/api/comments/{cid}", headers=_hdr(team, "editor1", "editor")).status_code == 200


def test_admin_can_delete_any_comment(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    cid = _post(client, _hdr(team, "editor1", "editor"), pid, "theirs")["id"]
    assert client.delete(f"/api/comments/{cid}", headers=admin_headers).status_code == 200


def test_comment_author_is_authenticated_user(client, team, admin_headers):
    # A client cannot post a comment as someone else.
    pid = _mk(client, admin_headers)
    r = client.post("/api/comments",
                    json={"item_id": pid, "body": "hi", "author": "someone_else"},
                    headers=_hdr(team, "editor1", "editor")).json()
    assert r["author"] == "editor1"
