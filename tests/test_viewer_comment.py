"""VIEWER-COMMENT-1 - viewers may comment (the least destructive write).

A viewer can CREATE a comment and EDIT their own, enforced server-side. The one trap: a
viewer-authored comment must NOT set firstResponseAt (the IT-team first-touch SLA metric,
set-once + forward-only) - the exclusion EXTENDS the existing reporter exclusion as one
condition, not a parallel guard. A viewer's mention still notifies but must not grant a
Contributor item access (a privilege path through the lowest role). Every other viewer
restriction stays in force.
"""
import json

import server


def _hdr(team, user, role):
    return {"Authorization": f"Bearer {server.create_token(team, user, role)}", "X-Team": team}


def _seed_user(team, username, role):
    with server.db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
        users = json.loads(row["value"]) if row else []
        users.append({"username": username, "role": role})
        c.execute("INSERT INTO config(key,value) VALUES('users',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(users),))


def _mk(client, headers):
    return client.post("/api/projects", json={"name": "T", "status": "New"}, headers=headers).json()["id"]


def _blob(team, pid):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])


def _grants(team, username):
    with server.db(team) as c:
        return [r[0] for r in c.execute("SELECT item_id FROM item_access_grants WHERE username=?",
                                        (username,)).fetchall()]


# ── The permission (server-enforced) ─────────────────────────────────────────

def test_viewer_can_post_comment(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    r = client.post("/api/comments", json={"item_id": pid, "body": "any update?"},
                    headers=_hdr(team, "viewer1", "viewer"))
    assert r.status_code == 200
    assert r.json()["author"] == "viewer1"


def test_viewer_still_cannot_write_items(client, team):
    # Nothing else about the role moved: a viewer creating an item is still refused.
    r = client.post("/api/projects", json={"name": "nope", "status": "New"},
                    headers=_hdr(team, "viewer1", "viewer"))
    assert r.status_code == 403


# ── firstResponseAt: the trap ────────────────────────────────────────────────

def test_viewer_comment_does_not_set_first_response(client, team, admin_headers):
    pid = _mk(client, admin_headers)                     # reporter = admin
    assert not _blob(team, pid).get("firstResponseAt")
    client.post("/api/comments", json={"item_id": pid, "body": "any update?"},
                headers=_hdr(team, "viewer1", "viewer"))
    assert not _blob(team, pid).get("firstResponseAt")   # viewer's first comment excluded


def test_editor_comment_still_sets_first_response(client, team, admin_headers):
    # The positive path must keep working - a fix that stops the metric is worse than the bug.
    pid = _mk(client, admin_headers)
    client.post("/api/comments", json={"item_id": pid, "body": "on it"},
                headers=_hdr(team, "editor1", "editor"))
    assert _blob(team, pid).get("firstResponseAt")


def test_role_is_the_deciding_clause(client, team, admin_headers):
    # The falsifiable pair: viewer1 and editor1 are BOTH non-reporters (reporter=admin). Only the
    # role differs, and only the editor's comment sets firstResponseAt - proving it is the viewer
    # clause (not the reporter clause) that excludes the viewer. One condition, two clauses.
    p_v = _mk(client, admin_headers)
    p_e = _mk(client, admin_headers)
    client.post("/api/comments", json={"item_id": p_v, "body": "?"}, headers=_hdr(team, "viewer1", "viewer"))
    client.post("/api/comments", json={"item_id": p_e, "body": "?"}, headers=_hdr(team, "editor1", "editor"))
    assert not _blob(team, p_v).get("firstResponseAt")
    assert _blob(team, p_e).get("firstResponseAt")


def test_reporter_exclusion_still_holds(client, team):
    # The reporter clause is untouched: editor1 files a ticket then comments on it themselves ->
    # first-touch stays unset (the real-world case the exclusion was built for).
    pid = client.post("/api/projects", json={"name": "T", "status": "New"},
                      headers=_hdr(team, "editor1", "editor")).json()["id"]
    client.post("/api/comments", json={"item_id": pid, "body": "mine"}, headers=_hdr(team, "editor1", "editor"))
    assert not _blob(team, pid).get("firstResponseAt")


def test_assignment_branch_still_sets_first_response(client, team, admin_headers):
    # The OTHER write site (first assignee) is untouched by this stage.
    pid = _mk(client, admin_headers)
    client.put(f"/api/projects/{pid}", json={"name": "T", "status": "New", "assignee": "editor1"},
               headers=admin_headers)
    assert _blob(team, pid).get("firstResponseAt")


# ── Viewer edits their own comment only ──────────────────────────────────────

def test_viewer_edits_own_comment_but_not_others(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    cid = client.post("/api/comments", json={"item_id": pid, "body": "typo"},
                      headers=_hdr(team, "viewer1", "viewer")).json()["id"]
    r = client.patch(f"/api/comments/{cid}", json={"body": "fixed"}, headers=_hdr(team, "viewer1", "viewer"))
    assert r.status_code == 200                          # own comment: editable (ownership rule)
    r2 = client.patch(f"/api/comments/{cid}", json={"body": "hax"}, headers=_hdr(team, "viewer2", "viewer"))
    assert r2.status_code == 403                          # someone else's: refused, no admin override either


# ── Mentions notify, but a viewer grants no Contributor access ───────────────

def test_viewer_mention_grants_no_access_but_editor_does(client, team, admin_headers):
    _seed_user(team, "contrib1", "contributor")
    p_v = _mk(client, admin_headers)
    client.post("/api/comments", json={"item_id": p_v, "body": "@contrib1 fyi"},
                headers=_hdr(team, "viewer1", "viewer"))
    assert _grants(team, "contrib1") == []               # viewer author -> notify only, NO grant
    p_e = _mk(client, admin_headers)
    client.post("/api/comments", json={"item_id": p_e, "body": "@contrib1 fyi"},
                headers=_hdr(team, "editor1", "editor"))
    assert p_e in _grants(team, "contrib1")              # editor author -> grant (unchanged)


def test_viewer_mention_still_notifies(client, team, admin_headers):
    _seed_user(team, "bob", "editor")
    pid = _mk(client, admin_headers)
    client.post("/api/comments", json={"item_id": pid, "body": "please look @bob"},
                headers=_hdr(team, "viewer1", "viewer"))
    nd = client.get("/api/notifications", headers=_hdr(team, "bob", "editor")).json()
    assert any(n["type"] == "mention" and n["item_id"] == pid for n in nd["notifications"])
