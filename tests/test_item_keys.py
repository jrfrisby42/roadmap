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


def test_key_survives_wholesale_put_without_itemkey(client, team, admin_headers):
    # Regression (item-page edit wiped the key in prod): the item-page/modal edit
    # sends a WHOLESALE PUT body that does NOT round-trip itemKey. update_project
    # must preserve the server-assigned key — both the blob and the mirrored
    # item_key column — rather than let the omission wipe it (which also NULLed the
    # column via _reindex_project).
    _set_products(client, admin_headers, [{"name": "Fraznet", "keyPrefix": "FRAZ"}])
    item = _mk(client, admin_headers, product="Fraznet")
    pid = item["id"]
    assert item["itemKey"] == "FRAZ-1"
    # Edit body with NO itemKey (e.g. a rename from the item page).
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Renamed", "status": "Planned", "product": "Fraznet"},
                   headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["itemKey"] == "FRAZ-1"        # preserved in the response blob
    with server.db(team) as c:
        row = c.execute("SELECT item_key, json_extract(data,'$.itemKey') "
                        "FROM projects WHERE id=?", (pid,)).fetchone()
    assert row[0] == "FRAZ-1"                      # mirrored column preserved
    assert row[1] == "FRAZ-1"                      # blob preserved


def test_key_not_wiped_by_blank_itemkey_in_put(client, team, admin_headers):
    # A client that sends itemKey:"" must NOT be able to clear an existing key.
    _set_products(client, admin_headers, [{"name": "Fraznet", "keyPrefix": "FRAZ"}])
    item = _mk(client, admin_headers, product="Fraznet")
    r = client.put(f"/api/projects/{item['id']}",
                   json={**item, "itemKey": ""}, headers=admin_headers)
    assert r.json()["itemKey"] == "FRAZ-1"


def test_recur_assigns_fresh_key_no_collision(client, team, admin_headers):
    # Regression (4.10.3): spawn_recurrence used to inherit the parent's itemKey,
    # which tripped the unique item_key index in _reindex_project → 500 on EVERY
    # spawn. The successor must get a FRESH per-product key, not the parent's.
    _set_products(client, admin_headers, [{"name": "Fraznet", "keyPrefix": "FRAZ"}])
    item = _mk(client, admin_headers, product="Fraznet",
               recurrence="weekly", start="2026-01-01", dueWeeks=2)
    assert item["itemKey"] == "FRAZ-1"
    r = client.post(f"/api/projects/{item['id']}/recur", json={}, headers=admin_headers)
    assert r.status_code == 200, r.text          # no more IntegrityError 500
    succ = r.json()
    assert succ["itemKey"] == "FRAZ-2"           # fresh key, NOT the parent's
    assert succ["id"] != item["id"]
    assert succ["recurrence_parent"] == item["id"]


def test_recur_skips_elapsed_cycles_to_current(client, team, admin_headers):
    # 4.10.4: a long-overdue recurring chain must jump straight to the CURRENT
    # window (skip fully-elapsed cycles), NOT back-fill every missed occurrence.
    import datetime as _dt
    _set_products(client, admin_headers, [{"name": "Fraznet", "keyPrefix": "FRAZ"}])
    old_start = (_dt.date.today() - _dt.timedelta(days=90)).isoformat()   # ~13 weeks behind
    item = _mk(client, admin_headers, product="Fraznet",
               recurrence="weekly", start=old_start, dueWeeks=1)
    r = client.post(f"/api/projects/{item['id']}/recur", json={}, headers=admin_headers)
    assert r.status_code == 200, r.text
    s = _dt.date.fromisoformat(r.json()["start"])
    today = _dt.date.today()
    # Landed on the current cycle: this window hasn't fully elapsed, and it isn't
    # a future-only jump — i.e. exactly the occurrence containing "now".
    assert s <= today < s + _dt.timedelta(days=7)
    # Still grid-aligned to the original start (advanced by whole 7-day periods).
    assert (s - _dt.date.fromisoformat(old_start)).days % 7 == 0


def test_recur_is_idempotent(client, team, admin_headers):
    # A recurring item spawns exactly one successor; a repeat call (re-save / double
    # click / worker race) must return the same successor, not create a duplicate.
    _set_products(client, admin_headers, [{"name": "Fraznet", "keyPrefix": "FRAZ"}])
    item = _mk(client, admin_headers, product="Fraznet",
               recurrence="weekly", start="2026-01-01", dueWeeks=2)
    r1 = client.post(f"/api/projects/{item['id']}/recur", json={}, headers=admin_headers)
    r2 = client.post(f"/api/projects/{item['id']}/recur", json={}, headers=admin_headers)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]    # same successor, no duplicate


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
