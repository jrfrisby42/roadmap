"""ITEM-PAGE-PARENT-1: update_project validates a CHANGED parent (the item page can now set it).

A new parent must be a different, existing item that is not one of this item's own descendants, and an
item has a parent OR a dependency (requires), never both. Unchanged parents are never re-checked, so a
re-save of existing (even inconsistent) data still passes."""
import json

import server


def _item(team, **f):
    with server.db(team) as c:
        return server._insert_project(c, {"name": "Thing", "status": "Backlogged", **f})


def _put(client, h, pid, **f):
    p = next(x for x in client.get("/api/all", headers=h).json()["projects"] if x["id"] == pid)
    return client.put(f"/api/projects/{pid}", headers=h, json={**p, **f})


def _parent(team, pid):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()[0]).get("parent")


def test_set_change_and_clear_parent(client, team, editor_headers):
    a, b, kid = _item(team, name="A"), _item(team, name="B"), _item(team, name="Kid")
    assert _put(client, editor_headers, kid, parent=a).status_code == 200 and _parent(team, kid) == a
    assert _put(client, editor_headers, kid, parent=b).status_code == 200 and _parent(team, kid) == b
    assert _put(client, editor_headers, kid, parent=None).status_code == 200 and _parent(team, kid) is None


def test_self_parent_is_refused(client, team, admin_headers):
    a = _item(team)
    r = _put(client, admin_headers, a, parent=a)
    assert r.status_code == 422 and "own parent" in r.json()["detail"]


def test_missing_parent_is_refused(client, team, admin_headers):
    a = _item(team)
    r = _put(client, admin_headers, a, parent=999999)
    assert r.status_code == 422 and "not found" in r.json()["detail"]


def test_loop_is_refused(client, team, admin_headers):
    top = _item(team, name="Top")
    mid = _item(team, name="Mid", parent=top)
    leaf = _item(team, name="Leaf", parent=mid)
    r = _put(client, admin_headers, top, parent=leaf)         # top -> leaf -> mid -> top
    assert r.status_code == 422 and "loop" in r.json()["detail"]
    assert _parent(team, top) is None


def test_parent_and_dependency_are_exclusive(client, team, admin_headers):
    dep, par = _item(team, name="Dep"), _item(team, name="Par")
    a = _item(team, requires=dep)
    r = _put(client, admin_headers, a, parent=par)
    assert r.status_code == 422 and "dependency" in r.json()["detail"]
    # clearing the dependency in the same save is fine
    assert _put(client, admin_headers, a, parent=par, requires=None).status_code == 200


def test_unchanged_parent_is_not_rechecked(client, team, admin_headers):
    dep, par = _item(team, name="Dep"), _item(team, name="Par")
    a = _item(team, requires=dep, parent=par)                 # pre-existing inconsistent data
    assert _put(client, admin_headers, a, name="Renamed").status_code == 200
