"""Phase 1d-ii (bulk): POST /api/items/bulk — patch whitelisted fields across
many items at once."""
import server


def _mk(client, headers, **fields):
    body = {"name": "Item", "status": "Planned", **fields}
    return client.post("/api/projects", json=body, headers=headers).json()["id"]


def _statuses(team, ids):
    with server.db(team) as c:
        return {r["id"]: r["status"] for r in c.execute(
            f"SELECT id, status FROM projects WHERE id IN ({','.join('?'*len(ids))})", ids).fetchall()}


def test_bulk_status_update(client, team, admin_headers):
    ids = [_mk(client, admin_headers) for _ in range(3)]
    r = client.post("/api/items/bulk",
                    json={"ids": ids, "patch": {"status": "In Progress"}},
                    headers=admin_headers)
    assert r.status_code == 200 and r.json()["updated"] == 3
    assert all(s == "In Progress" for s in _statuses(team, ids).values())   # indexed column updated


def test_bulk_archive(client, team, admin_headers):
    ids = [_mk(client, admin_headers) for _ in range(2)]
    client.post("/api/items/bulk", json={"ids": ids, "patch": {"archived": True}},
                headers=admin_headers)
    # archived items drop out of the default /api/items listing
    listed = {i["id"] for i in client.get("/api/items", headers=admin_headers).json()["items"]}
    assert not (set(ids) & listed)
    assert client.get("/api/items?archived=1", headers=admin_headers).json()["total"] == 2


def test_bulk_owner_and_assignee(client, team, admin_headers):
    ids = [_mk(client, admin_headers) for _ in range(2)]
    client.post("/api/items/bulk",
                json={"ids": ids, "patch": {"dev": "PodX", "assignee": "alice"}},
                headers=admin_headers)
    got = client.get("/api/items?owner=PodX&assignee=alice", headers=admin_headers).json()
    assert got["total"] == 2


def test_bulk_disallowed_field_rejected(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    r = client.post("/api/items/bulk",
                    json={"ids": [pid], "patch": {"parallelResources": 5}},
                    headers=admin_headers)
    assert r.status_code == 400


def test_bulk_validation(client, team, admin_headers):
    assert client.post("/api/items/bulk", json={"ids": [], "patch": {"status": "X"}},
                       headers=admin_headers).status_code == 400
    assert client.post("/api/items/bulk", json={"ids": [1], "patch": {}},
                       headers=admin_headers).status_code == 400


def test_bulk_role_gating(client, team, viewer_headers, editor_headers, admin_headers):
    pid = _mk(client, admin_headers)
    assert client.post("/api/items/bulk", json={"ids": [pid], "patch": {"status": "Done"}},
                       headers=viewer_headers).status_code == 403
    assert client.post("/api/items/bulk", json={"ids": [pid], "patch": {"status": "Done"}},
                       headers=editor_headers).status_code == 200


def test_bulk_skips_missing_ids(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    r = client.post("/api/items/bulk",
                    json={"ids": [pid, 999999], "patch": {"status": "In Progress"}},
                    headers=admin_headers)
    assert r.json()["updated"] == 1   # missing id silently skipped
