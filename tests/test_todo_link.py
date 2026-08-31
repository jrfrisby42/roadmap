"""TODO-LINK-1 - one optional URL on a to-do.

A to-do carries a single optional `url` (http/https only, shared LINKS-1 scheme guard). Blank
clears it (not a delete). A javascript: or other non-http scheme is rejected at save (422). The
URL is inherited by a recurring to-do's next occurrence, and stays private (excluded from
/api/export, owner-scoped like the rest of the row).
"""
import datetime

import server


def _hdr(team, user, role="editor"):
    return {"Authorization": f"Bearer {server.create_token(team, user, role)}", "X-Team": team}


def _mk(client, hdr, **fields):
    return client.post("/api/my/todos", json=fields, headers=hdr).json()["todo"]


def _open(client, hdr):
    return client.get("/api/my/todos?status=open", headers=hdr).json()["todos"]


def _row(team, tid):
    with server.db(team) as c:
        r = c.execute("SELECT * FROM todos WHERE id=?", (tid,)).fetchone()
        return dict(r) if r else None


GOOD = "https://registrar.example.com/domains/ssl"


def test_create_persists_url(client, team):
    h = _hdr(team, "alice")
    t = _mk(client, h, title="renew SSL cert", url=GOOD)
    assert t["url"] == GOOD


def test_update_sets_and_clears_url(client, team):
    h = _hdr(team, "alice")
    t = _mk(client, h, title="x")
    assert t["url"] in (None, "")
    client.put(f"/api/my/todos/{t['id']}", json={"url": GOOD}, headers=h)
    assert _row(team, t["id"])["url"] == GOOD
    # blank clears - and does NOT delete the to-do
    client.put(f"/api/my/todos/{t['id']}", json={"url": ""}, headers=h)
    assert _row(team, t["id"])["url"] is None
    assert t["id"] in {x["id"] for x in _open(client, h)}      # row survives the clear


def test_javascript_scheme_rejected_at_save(client, team):
    h = _hdr(team, "alice")
    r = client.post("/api/my/todos", json={"title": "x", "url": "javascript:alert(1)"}, headers=h)
    assert r.status_code == 422
    # and on update
    t = _mk(client, h, title="y")
    r2 = client.put(f"/api/my/todos/{t['id']}", json={"url": "javascript:alert(1)"}, headers=h)
    assert r2.status_code == 422
    assert _row(team, t["id"])["url"] is None                  # unchanged by the rejected save


def test_non_http_schemes_rejected(client, team):
    h = _hdr(team, "alice")
    for bad in ["ftp://host/x", "mailto:a@b.com", "file:///etc/passwd", "notaurl", "http:evil"]:
        r = client.post("/api/my/todos", json={"title": "x", "url": bad}, headers=h)
        assert r.status_code == 422, bad


def test_url_inherited_by_recurrence(client, team):
    h = _hdr(team, "alice")
    due = datetime.date.today().isoformat()
    t = _mk(client, h, title="weekly cert check", due_date=due, recurrence="weekly", url=GOOD)
    client.put(f"/api/my/todos/{t['id']}", json={"status": "Done"}, headers=h)   # complete -> spawn next
    spawned = _open(client, h)
    assert len(spawned) == 1
    assert spawned[0]["url"] == GOOD                            # the next occurrence keeps the link


def test_url_excluded_from_export(client, team):
    h = _hdr(team, "alice", "admin")
    _mk(client, h, title="secret", url="https://private.example.com/x")
    exp = client.get("/api/export", headers=h)
    assert exp.status_code == 200
    assert "todos" not in exp.json()                           # to-dos (and their URLs) never export


def test_url_is_owner_private(client, team):
    alice, bob = _hdr(team, "alice"), _hdr(team, "bob")
    _mk(client, alice, title="alice link", url=GOOD)
    assert GOOD not in {x.get("url") for x in _open(client, bob)}   # bob never sees alice's row or its url
