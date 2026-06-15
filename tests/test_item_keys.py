"""Phase 1b (JIRA-REPLACEMENT.md §4): per-product item keys {PREFIX}-{n}."""
import json

import server


def _set_products(client, admin_headers, products):
    return client.put("/api/config/products", json=products, headers=admin_headers)


def _mk(client, headers, **fields):
    body = {"name": "Item", "status": "Planned", **fields}
    return client.post("/api/projects", json=body, headers=headers).json()


def test_no_prefix_uses_default(client, team, admin_headers):
    # A product with no keyPrefix (or no product) still gets a key under the
    # default prefix, so every item is addressable.
    _set_products(client, admin_headers, [{"name": "Fraznet", "builtin": True}])
    item = _mk(client, admin_headers, product="Fraznet")
    assert item.get("itemKey") == f"{server.DEFAULT_KEY_PREFIX}-1"


def test_key_assigned_and_increments(client, team, admin_headers):
    _set_products(client, admin_headers, [{"name": "Fraznet", "keyPrefix": "FRAZ"}])
    a = _mk(client, admin_headers, product="Fraznet")
    b = _mk(client, admin_headers, product="Fraznet")
    assert a["itemKey"] == "FRAZ-1"
    assert b["itemKey"] == "FRAZ-2"


def test_counters_are_per_prefix(client, team, admin_headers):
    _set_products(client, admin_headers, [
        {"name": "Fraznet", "keyPrefix": "FRAZ"},
        {"name": "HubSpot", "keyPrefix": "HUB"},
    ])
    f1 = _mk(client, admin_headers, product="Fraznet")["itemKey"]
    h1 = _mk(client, admin_headers, product="HubSpot")["itemKey"]
    f2 = _mk(client, admin_headers, product="Fraznet")["itemKey"]
    assert f1 == "FRAZ-1" and f2 == "FRAZ-2" and h1 == "HUB-1"


def test_key_indexed_column(client, team, admin_headers):
    _set_products(client, admin_headers, [{"name": "Fraznet", "keyPrefix": "FRAZ"}])
    pid = _mk(client, admin_headers, product="Fraznet")["id"]
    with server.db(team) as c:
        key = c.execute("SELECT item_key FROM projects WHERE id=?", (pid,)).fetchone()[0]
    assert key == "FRAZ-1"


def test_backfill_on_prefix_save(client, team, admin_headers):
    # With no product prefix items get default-prefix keys; since the default IS
    # "FRAZ", defining a "FRAZ" prefix here leaves them as FRAZ-1/FRAZ-2.
    _set_products(client, admin_headers, [{"name": "Fraznet", "builtin": True}])
    p1 = _mk(client, admin_headers, product="Fraznet")["id"]
    p2 = _mk(client, admin_headers, product="Fraznet")["id"]
    # ...defining the prefix backfills them (ordered by id).
    _set_products(client, admin_headers, [{"name": "Fraznet", "keyPrefix": "FRAZ"}])
    with server.db(team) as c:
        keys = {r["id"]: r["item_key"]
                for r in c.execute("SELECT id, item_key FROM projects").fetchall()}
    assert keys[p1] == "FRAZ-1" and keys[p2] == "FRAZ-2"


def test_key_immutable_across_product_change(client, team, admin_headers):
    _set_products(client, admin_headers, [
        {"name": "Fraznet", "keyPrefix": "FRAZ"},
        {"name": "HubSpot", "keyPrefix": "HUB"},
    ])
    item = _mk(client, admin_headers, product="Fraznet")
    assert item["itemKey"] == "FRAZ-1"
    # Move it to HubSpot — key must NOT change (immutable).
    r = client.put(f"/api/projects/{item['id']}",
                   json={**item, "product": "HubSpot"}, headers=admin_headers)
    assert r.json()["itemKey"] == "FRAZ-1"


def test_keys_unique_after_backfill_and_create_mix(client, team, admin_headers):
    _set_products(client, admin_headers, [{"name": "Fraznet", "keyPrefix": "FRAZ"}])
    created = [_mk(client, admin_headers, product="Fraznet")["itemKey"] for _ in range(3)]
    # add a keyless legacy row (indexed like the 1a boot backfill would), then
    # backfill keys via a products re-save
    legacy = {"name": "legacy", "product": "Fraznet"}
    with server.db(team) as c:
        cur = c.execute("INSERT INTO projects(data) VALUES(?)", (json.dumps(legacy),))
        server._reindex_project(c, cur.lastrowid, legacy)   # populates product column
    _set_products(client, admin_headers, [{"name": "Fraznet", "keyPrefix": "FRAZ"}])
    with server.db(team) as c:
        keys = [r[0] for r in c.execute(
            "SELECT item_key FROM projects WHERE item_key IS NOT NULL").fetchall()]
    assert len(keys) == len(set(keys)) == 4   # all unique, legacy got one too
