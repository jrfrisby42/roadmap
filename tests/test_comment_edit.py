"""COMMENT-EDIT-1 - edit your own comment (PATCH /api/comments/{cid}).

Three rules, all server-enforced:
  1. Ownership - only the author (token username) may edit. NO admin override.
  2. Marked - edited_ts is stamped; created_ts (thread position) is unchanged.
  3. Silent - an edit fires NO notification. Holds structurally: the edit path never calls
     _notify_on_comment and contains no firstResponseAt block.
Plus: empty is not a delete (422); image-only bodies are allowed.
"""
import json

import server


def _hdr(team, username, role="editor"):
    return {"Authorization": f"Bearer {server.create_token(team, username, role)}", "X-Team": team}


def _add_user(team, username, role="editor"):
    with server.db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
        users = json.loads(row["value"]) if row else []
        if not any(u.get("username") == username for u in users):
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


def _patch(client, headers, cid, body):
    return client.patch(f"/api/comments/{cid}", json={"body": body}, headers=headers)


def _row(team, cid):
    with server.db(team) as c:
        r = c.execute("SELECT * FROM comments WHERE id=?", (cid,)).fetchone()
        return dict(r) if r else None


def _item(team, pid):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])


# ── Rule 1: ownership ─────────────────────────────────────────────────────────
def test_owner_can_edit_own_comment(client, team, editor_headers):
    pid = _mk(client, editor_headers)
    cid = _post(client, editor_headers, pid, "typo")["id"]
    r = _patch(client, editor_headers, cid, "<p>fixed</p>")
    assert r.status_code == 200
    assert _row(team, cid)["body"] == "<p>fixed</p>"


def test_other_editor_cannot_edit(client, team, editor_headers):
    pid = _mk(client, editor_headers)
    cid = _post(client, editor_headers, pid, "mine")["id"]
    r = _patch(client, _hdr(team, "editor2", "editor"), cid, "<p>hijack</p>")
    assert r.status_code == 403
    assert _row(team, cid)["body"] == "mine"          # unchanged


def test_admin_cannot_edit_others(client, team, admin_headers, editor_headers):
    # NO admin override (rule 1): editing someone else's words under their name is worse
    # than the typo it fixes.
    pid = _mk(client, editor_headers)
    cid = _post(client, editor_headers, pid, "mine")["id"]
    r = _patch(client, admin_headers, cid, "<p>admin override</p>")
    assert r.status_code == 403
    assert _row(team, cid)["body"] == "mine"          # unchanged


def test_edit_missing_comment_404(client, team, editor_headers):
    assert _patch(client, editor_headers, 999999, "<p>x</p>").status_code == 404


# ── Rule 2: marked, position preserved ────────────────────────────────────────
def test_edited_ts_null_until_edited(client, team, editor_headers):
    pid = _mk(client, editor_headers)
    cid = _post(client, editor_headers, pid, "orig")["id"]
    assert _row(team, cid)["edited_ts"] is None        # existing comments render no marker
    _patch(client, editor_headers, cid, "<p>edited</p>")
    assert _row(team, cid)["edited_ts"] is not None    # marker stamp set


def test_created_ts_unchanged_by_edit(client, team, editor_headers):
    pid = _mk(client, editor_headers)
    cid = _post(client, editor_headers, pid, "orig")["id"]
    before = _row(team, cid)["created_ts"]
    _patch(client, editor_headers, cid, "<p>new</p>")
    assert _row(team, cid)["created_ts"] == before     # keeps its position in the thread


# ── Empty is not a delete ─────────────────────────────────────────────────────
def test_empty_body_rejected(client, team, editor_headers):
    pid = _mk(client, editor_headers)
    cid = _post(client, editor_headers, pid, "content")["id"]
    for empty in ["", "   ", "<p></p>", "<p>  </p>", "<p>&nbsp;</p>"]:
        assert _patch(client, editor_headers, cid, empty).status_code == 422, empty
    assert _row(team, cid)["body"] == "content"        # unchanged by any rejected save
    # an image-only body IS content
    assert _patch(client, editor_headers, cid, '<img data-att-key="k1">').status_code == 200


# ── Rule 3: silent (structural) ───────────────────────────────────────────────
def test_edit_fires_no_notification(client, team, editor_headers):
    # editor1 comments; watcher1 (watching) gets ONE watch_comment notification. Then editor1
    # edits, ADDING an @mention of watcher1 - which must notify NOBODY.
    _add_user(team, "editor1", "editor")
    _add_user(team, "watcher1", "editor")
    pid = _mk(client, editor_headers)
    client.post(f"/api/items/{pid}/watch", headers=_hdr(team, "watcher1", "editor"))
    cid = _post(client, editor_headers, pid, "orig")["id"]
    before = len(client.get("/api/notifications", headers=_hdr(team, "watcher1", "editor")).json())
    _patch(client, editor_headers, cid, "<p>edited @watcher1</p>")
    after = len(client.get("/api/notifications", headers=_hdr(team, "watcher1", "editor")).json())
    assert after == before                             # the edit added no notification


def test_edit_does_not_alter_firstResponseAt_when_present(client, team, editor_headers):
    # editor1 creates (so reporter=editor1); editor2's comment is the first non-reporter
    # response and stamps firstResponseAt. editor2 editing their own comment must not alter it.
    _add_user(team, "editor2", "editor")
    pid = _mk(client, editor_headers)
    e2 = _hdr(team, "editor2", "editor")
    cid = _post(client, e2, pid, "first")["id"]
    fr = _item(team, pid).get("firstResponseAt")
    assert fr                                           # set by the first non-reporter comment
    _patch(client, e2, cid, "<p>edited</p>")
    assert _item(team, pid).get("firstResponseAt") == fr      # unchanged (set-once, forward-only)


def test_edit_does_not_set_firstResponseAt_when_absent(client, team, editor_headers):
    # A comment by the REPORTER does not stamp firstResponseAt; editing it must not either.
    pid = _mk(client, editor_headers)
    with server.db(team) as c:
        it = json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])
        it["reporter"] = "editor1"
        c.execute("UPDATE projects SET data=? WHERE id=?", (json.dumps(it), pid))
    cid = _post(client, editor_headers, pid, "reporter note")["id"]
    assert not _item(team, pid).get("firstResponseAt")        # absent
    _patch(client, editor_headers, cid, "<p>edited</p>")
    assert not _item(team, pid).get("firstResponseAt")        # still absent
