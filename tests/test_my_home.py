"""Stage 5 (/beta "My Home") — the two read endpoints behind the Recent and
Watching tabs:

  GET /api/my/watching  → item ids the caller watches (non-archived, live items)
  GET /api/my/recent    → merged "recently touched by me" trail from the audit
                          log, deduped by item (newest wins), newest-first

Both are additive, read-only, any-authed-user. The classic UI never calls them.
"""
import server


def _mk(client, headers, **fields):
    body = {"name": "Item", "status": "Planned", **fields}
    return client.post("/api/projects", json=body, headers=headers).json()


def _watching(client, headers):
    return client.get("/api/my/watching", headers=headers).json()["items"]


def _recent(client, headers):
    return client.get("/api/my/recent", headers=headers).json()["items"]


def _recent_ids(client, headers):
    return [r["item_id"] for r in _recent(client, headers)]


# ── /api/my/watching ──────────────────────────────────────────────────────────

def test_watching_lists_only_watched(client, team, admin_headers, editor_headers):
    a = _mk(client, admin_headers)["id"]            # creator auto-watches (3b behavior)
    _mk(client, editor_headers)                     # editor's item — admin is not a watcher
    client.post(f"/api/items/{a}/watch", headers=admin_headers)   # explicit, idempotent
    assert _watching(client, admin_headers) == [a]


def test_watching_excludes_archived(client, team, admin_headers):
    a = _mk(client, admin_headers)["id"]
    client.post(f"/api/items/{a}/watch", headers=admin_headers)
    client.put(f"/api/projects/{a}",
               json={"name": "Item", "status": "Planned", "archived": True},
               headers=admin_headers)
    assert _watching(client, admin_headers) == []


def test_watching_is_per_user(client, team, admin_headers, viewer_headers):
    a = _mk(client, admin_headers)["id"]
    client.post(f"/api/items/{a}/watch", headers=admin_headers)
    assert _watching(client, admin_headers) == [a]
    assert _watching(client, viewer_headers) == []   # someone else's watch never leaks


def test_watching_requires_auth(client, team):
    assert client.get("/api/my/watching").status_code == 401


# ── /api/my/recent ──────────────────────────────────────────────────────────

def test_recent_returns_my_worked_items(client, team, admin_headers):
    a = _mk(client, admin_headers)["id"]
    b = _mk(client, admin_headers)["id"]
    ids = _recent_ids(client, admin_headers)
    assert set(ids) == {a, b}
    assert all(r["source"] == "worked" for r in _recent(client, admin_headers))


def test_recent_dedups_by_item(client, team, admin_headers):
    a = _mk(client, admin_headers)["id"]
    client.put(f"/api/projects/{a}",
               json={"name": "Item", "status": "In Progress"}, headers=admin_headers)
    ids = _recent_ids(client, admin_headers)
    assert ids.count(a) == 1   # create + update collapse to one row


def test_recent_excludes_archived(client, team, admin_headers):
    a = _mk(client, admin_headers)["id"]
    client.put(f"/api/projects/{a}",
               json={"name": "Item", "status": "Planned", "archived": True},
               headers=admin_headers)
    assert a not in _recent_ids(client, admin_headers)


def test_recent_is_per_user(client, team, admin_headers, editor_headers):
    mine = _mk(client, admin_headers)["id"]
    theirs = _mk(client, editor_headers)["id"]
    ids = _recent_ids(client, admin_headers)
    assert mine in ids and theirs not in ids   # only items I touched


def test_recent_requires_auth(client, team):
    assert client.get("/api/my/recent").status_code == 401


# ── Stage 6: the "viewed" source (POST /api/items/{pid}/view) ─────────────────

def _recent_by_id(client, headers, item_id):
    return next((r for r in _recent(client, headers) if r["item_id"] == item_id), None)


def test_view_adds_a_viewed_source_item(client, team, admin_headers, editor_headers):
    # editor creates it (so admin never "worked" it); admin only views it.
    v = _mk(client, editor_headers)["id"]
    assert v not in _recent_ids(client, admin_headers)          # not yet on admin's trail
    assert client.post(f"/api/items/{v}/view", headers=admin_headers).status_code == 200
    row = _recent_by_id(client, admin_headers, v)
    assert row is not None and row["source"] == "viewed"        # surfaced purely via the view


def test_view_merges_and_dedups_with_worked(client, team, admin_headers):
    a = _mk(client, admin_headers)["id"]                        # worked
    client.post(f"/api/items/{a}/view", headers=admin_headers)  # also viewed
    assert _recent_ids(client, admin_headers).count(a) == 1     # one row, not two
    assert _recent_by_id(client, admin_headers, a)["source"] in ("worked", "viewed")


def test_view_excludes_archived(client, team, admin_headers, editor_headers):
    v = _mk(client, editor_headers)["id"]
    client.post(f"/api/items/{v}/view", headers=admin_headers)
    client.put(f"/api/projects/{v}",
               json={"name": "Item", "status": "Planned", "archived": True},
               headers=admin_headers)
    assert v not in _recent_ids(client, admin_headers)


def test_view_is_per_user(client, team, admin_headers, editor_headers, viewer_headers):
    v = _mk(client, editor_headers)["id"]
    client.post(f"/api/items/{v}/view", headers=admin_headers)
    assert v in _recent_ids(client, admin_headers)
    assert _recent_ids(client, viewer_headers) == []            # another user's view never leaks


def test_view_requires_auth(client, team):
    assert client.post("/api/items/1/view").status_code == 401


def test_view_prune_keeps_newest_100(client, team, admin_headers):
    # the table is bounded per user (read also caps at 50); insert > 100 distinct views
    for i in range(1, 106):
        client.post(f"/api/items/{i}/view", headers=admin_headers)
    with server.db(team) as c:
        n = c.execute("SELECT COUNT(*) FROM recent_views WHERE username=?", ("admin",)).fetchone()[0]
    assert n == 100
