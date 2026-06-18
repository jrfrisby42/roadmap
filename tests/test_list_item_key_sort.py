"""list_items — natural (numeric) sort for the item_key column.

`ORDER BY projects.item_key` was plain TEXT (FRAZ-1, FRAZ-10, FRAZ-2). The item_key
branch now orders by prefix (case-insensitive) then the trailing integer numerically,
computed over the full set BEFORE LIMIT/OFFSET so it's correct across pagination.
Blanks (NULL/'') sort last in both directions. Only the item_key sort branch changed.
"""
import json

import server


def _mk(client, headers):
    return client.post("/api/projects", json={"name": "I", "status": "Planned"},
                       headers=headers).json()["id"]


def _set_key(team, pid, key):
    """Set both the indexed item_key column (what the sort reads) and the blob itemKey
    (what the response returns) so they stay consistent."""
    with server.db(team) as c:
        row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
        d = json.loads(row["data"])
        if key is None:
            d.pop("itemKey", None)
        else:
            d["itemKey"] = key
        c.execute("UPDATE projects SET data=?, item_key=? WHERE id=?", (json.dumps(d), key, pid))


def _order(client, headers, **q):
    data = client.get("/api/items", params={"sort": "item_key:asc", **q}, headers=headers).json()
    return [(it.get("itemKey") or "") for it in data["items"]]


SEED = ["FRAZ-1", "FRAZ-2", "FRAZ-9", "FRAZ-10", "FRAZ-100", "HUB-3", "HUB-21", None, ""]
KEYED_ASC = ["FRAZ-1", "FRAZ-2", "FRAZ-9", "FRAZ-10", "FRAZ-100", "HUB-3", "HUB-21"]


def _seed(client, team, headers):
    ids = [_mk(client, headers) for _ in SEED]
    # Items get sequential auto-keys (FRAZ-1, FRAZ-2, …) on create; clear them to NULL
    # first (NULL is exempt from the UNIQUE item_key index) so assigning the test values
    # can't hit a transient collision with an auto-key.
    with server.db(team) as c:
        c.execute("UPDATE projects SET item_key=NULL")
    for pid, k in zip(ids, SEED):
        _set_key(team, pid, k)


def test_item_key_natural_ascending(client, team, admin_headers):
    _seed(client, team, admin_headers)
    got = _order(client, admin_headers, page_size=50)
    assert got[:7] == KEYED_ASC          # numeric, not lexicographic
    assert got[7:] == ["", ""]           # both blanks (NULL + '') last


def test_item_key_natural_descending(client, team, admin_headers):
    _seed(client, team, admin_headers)
    data = client.get("/api/items", params={"sort": "item_key:desc", "page_size": 50},
                      headers=admin_headers).json()
    got = [(it.get("itemKey") or "") for it in data["items"]]
    assert got[:7] == list(reversed(KEYED_ASC))   # keyed set reversed
    assert got[7:] == ["", ""]                     # blanks STILL last (not first)


def test_item_key_order_correct_across_pagination(client, team, admin_headers):
    # 9 rows, page_size=3 → 3 pages. Proves the natural order is applied before
    # LIMIT/OFFSET (a per-page client sort could never produce this).
    _seed(client, team, admin_headers)
    p1 = _order(client, admin_headers, page_size=3, page=1)
    p2 = _order(client, admin_headers, page_size=3, page=2)
    p3 = _order(client, admin_headers, page_size=3, page=3)
    assert p1 == ["FRAZ-1", "FRAZ-2", "FRAZ-9"]
    assert p2 == ["FRAZ-10", "FRAZ-100", "HUB-3"]
    assert p3 == ["HUB-21", "", ""]


def test_item_key_non_conforming_does_not_crash(client, team, admin_headers):
    # A key that doesn't fit "{prefix}-{int}" must not crash the CAST; it just sorts by
    # its (prefix=full-text, number=0) fallback and the request still succeeds.
    _set_key(team, _mk(client, admin_headers), "FRAZ-10")
    _set_key(team, _mk(client, admin_headers), "WEIRDKEY")
    r = client.get("/api/items", params={"sort": "item_key:asc", "page_size": 50}, headers=admin_headers)
    assert r.status_code == 200
    assert {"FRAZ-10", "WEIRDKEY"} <= set(it.get("itemKey") or "" for it in r.json()["items"])
