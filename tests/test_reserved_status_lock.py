"""POLISH-2 Part 1: an item in a reserved terminal status (Transferred/Duplicate) is status-locked -
no ordinary picker / bulk / update can move it out. The dedicated flows (mark/unmark, transfer) use
_save_project directly, so they bypass the lock - "Not a duplicate" must never be blocked by it."""
import server


def _mk(client, headers, **f):
    return client.post("/api/projects", json={"name": "X", "status": "Planned", **f}, headers=headers).json()["id"]


def _blob(team, pid):
    with server.db(team) as c:
        return server._get_item_blob(c, pid)


def _make_duplicate(client, headers):
    lid, sid = _mk(client, headers), _mk(client, headers)
    assert client.post(f"/api/items/{lid}/mark-duplicate", json={"survivorId": sid}, headers=headers).status_code == 200
    return lid, sid


def _set_status_direct(team, pid, status):
    with server.db(team) as c:
        b = server._get_item_blob(c, pid); b["status"] = status; server._save_project(c, pid, b)


# ── the lock (server-side, authoritative) ────────────────────────────────────
def test_status_change_refused_on_duplicate_and_names_the_way_out(client, team, admin_headers):
    lid, _ = _make_duplicate(client, admin_headers)
    r = client.put(f"/api/projects/{lid}", json={"status": "In Progress"}, headers=admin_headers)
    assert r.status_code == 409
    assert "Not a duplicate" in r.json()["detail"]["message"]                 # names the way out
    assert _blob(team, lid)["status"] == server.DUPLICATE_STATUS              # unchanged


def test_status_change_refused_on_transferred(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    _set_status_direct(team, pid, server.TRANSFERRED_STATUS)
    r = client.put(f"/api/projects/{pid}", json={"status": "In Progress"}, headers=admin_headers)
    assert r.status_code == 409 and "transferred" in r.json()["detail"]["message"].lower()
    assert _blob(team, pid)["status"] == server.TRANSFERRED_STATUS


def test_non_status_edit_still_allowed_on_a_reserved_item(client, team, admin_headers):
    lid, _ = _make_duplicate(client, admin_headers)
    r = client.put(f"/api/projects/{lid}", json={"notes": "context"}, headers=admin_headers)   # no status field
    assert r.status_code == 200
    assert _blob(team, lid)["status"] == server.DUPLICATE_STATUS              # status untouched


def test_normal_status_change_is_unaffected(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    r = client.put(f"/api/projects/{pid}", json={"status": "In Progress"}, headers=admin_headers)
    assert r.status_code == 200
    assert _blob(team, pid)["status"] == "In Progress"


# ── the escape hatch is NOT blocked by the lock (the way to get this wrong) ───
def test_unmark_still_works_on_a_locked_duplicate(client, team, admin_headers):
    lid, sid = _make_duplicate(client, admin_headers)
    r = client.post(f"/api/items/{lid}/unmark-duplicate", json={}, headers=admin_headers)
    assert r.status_code == 200
    assert _blob(team, lid)["status"] != server.DUPLICATE_STATUS
    assert server._find_dup_ref(_blob(team, sid), "duplicated-by") is None    # both directions cleared


# ── bulk skips reserved items on a status change, names them, still touches the rest ──
def test_bulk_status_skips_reserved_items(client, team, admin_headers):
    lid, _ = _make_duplicate(client, admin_headers)
    lkey = _blob(team, lid)["itemKey"]
    normal = _mk(client, admin_headers)
    r = client.post("/api/items/bulk", json={"ids": [lid, normal], "patch": {"status": "In Progress"}}, headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == 1                                               # only the normal one
    assert lkey in body["skipped"]                                            # the duplicate named, not moved
    assert _blob(team, lid)["status"] == server.DUPLICATE_STATUS
    assert _blob(team, normal)["status"] == "In Progress"
