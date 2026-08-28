"""WATCH-COL-1 - the List query attaches watchers per page item.

`GET /api/items` returns each item with a `_watchers` list (usernames, sorted
alphabetically - the watchers table carries no timestamp). Watchers are fetched in
ONE grouped query keyed on the page's item ids (never per-row), and an item with no
watchers gets an empty list. This is the server half; the stacked-avatar column,
opt-in, and CSV export are frontend-only.
"""
import server


def _mk(client, headers, name="Item"):
    return client.post("/api/projects", json={"name": name, "status": "Planned"},
                       headers=headers).json()["id"]


def _items(client, headers, **params):
    from urllib.parse import urlencode
    qs = urlencode(params)
    return client.get(f"/api/items?{qs}" if qs else "/api/items", headers=headers).json()["items"]


def _by_id(items, pid):
    return next(x for x in items if x["id"] == pid)


def _clear(team, pid):
    # create_project auto-watches the creator; clear for a controlled watcher set.
    with server.db(team) as c:
        c.execute("DELETE FROM watchers WHERE item_id=?", (pid,))


def test_item_carries_watchers_sorted(client, team, editor_headers):
    pid = _mk(client, editor_headers)
    _clear(team, pid)
    server._add_watchers(team, pid, ["zoe", "amy", "bob"])
    it = _by_id(_items(client, editor_headers), pid)
    assert it["_watchers"] == ["amy", "bob", "zoe"]          # alphabetical, stable order


def test_no_watchers_is_empty_list(client, team, editor_headers):
    pid = _mk(client, editor_headers)
    _clear(team, pid)
    it = _by_id(_items(client, editor_headers), pid)
    assert it["_watchers"] == []                             # empty -> [] (the column shows a dash)


def test_watchers_scoped_to_their_item(client, team, editor_headers):
    p1 = _mk(client, editor_headers, "one")
    p2 = _mk(client, editor_headers, "two")
    _clear(team, p1); _clear(team, p2)
    server._add_watchers(team, p1, ["amy"])
    items = _items(client, editor_headers)
    assert _by_id(items, p1)["_watchers"] == ["amy"]
    assert _by_id(items, p2)["_watchers"] == []             # p1's watcher does not leak to p2


def test_watch_endpoint_reflects_in_list(client, team, editor_headers):
    # The real toggle path: POST /watch -> the username appears on the next List fetch.
    pid = _mk(client, editor_headers)
    client.post(f"/api/items/{pid}/watch", headers=editor_headers)
    it = _by_id(_items(client, editor_headers), pid)
    assert "editor1" in it["_watchers"]


def test_multiple_items_one_page_all_get_watchers(client, team, editor_headers):
    # A page of many items each carries its own watcher set from the single grouped query.
    ids = [_mk(client, editor_headers, f"i{n}") for n in range(5)]
    for pid in ids:
        _clear(team, pid)
    server._add_watchers(team, ids[0], ["amy", "bob"])
    server._add_watchers(team, ids[2], ["carol"])
    items = _items(client, editor_headers, page_size=50)
    assert _by_id(items, ids[0])["_watchers"] == ["amy", "bob"]
    assert _by_id(items, ids[1])["_watchers"] == []
    assert _by_id(items, ids[2])["_watchers"] == ["carol"]
    assert _by_id(items, ids[3])["_watchers"] == []
