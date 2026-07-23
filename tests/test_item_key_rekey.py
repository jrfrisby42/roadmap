"""Re-key on product change: when an item's product changes to one whose resolved key
PREFIX differs, update_project re-mints the key under the new prefix (a fresh number).
Same-prefix moves and non-product edits keep the key. Keys are otherwise immutable and
never trusted from the client."""
import server


def _put_config(client, headers, key, val):
    # The config PUT takes the value as the RAW body (Body(...)), not wrapped in {"value": ...}.
    r = client.put(f"/api/config/{key}", json=val, headers=headers)
    assert r.status_code == 200, r.text


def test_rekey_when_product_prefix_changes(client, admin_headers, team):
    _put_config(client, admin_headers, "products",
                [{"name": "Alpha", "keyPrefix": "ALP"}, {"name": "Beta", "keyPrefix": "BET"}])
    r = client.post("/api/projects", json={"name": "x", "product": "Alpha"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    it = r.json(); pid = it["id"]
    assert it["itemKey"].startswith("ALP-"), it["itemKey"]

    # product -> Beta (different prefix): re-minted under BET
    r = client.put(f"/api/projects/{pid}", json={**it, "product": "Beta"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    it2 = r.json()
    assert it2["itemKey"].startswith("BET-"), it2["itemKey"]
    assert it2["itemKey"] != it["itemKey"]

    # a non-product edit keeps the key
    r = client.put(f"/api/projects/{pid}", json={**it2, "name": "renamed"}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["itemKey"] == it2["itemKey"]


def test_no_rekey_when_prefix_unchanged(client, admin_headers, team):
    _put_config(client, admin_headers, "products",
                [{"name": "One", "keyPrefix": "SAME"}, {"name": "Two", "keyPrefix": "SAME"}])
    r = client.post("/api/projects", json={"name": "x", "product": "One"}, headers=admin_headers)
    it = r.json(); pid = it["id"]
    assert it["itemKey"].startswith("SAME-")
    # product changes but both resolve to the same prefix -> key unchanged
    r = client.put(f"/api/projects/{pid}", json={**it, "product": "Two"}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["itemKey"] == it["itemKey"]


def test_client_cannot_forge_key(client, admin_headers, team):
    _put_config(client, admin_headers, "products", [{"name": "Alpha", "keyPrefix": "ALP"}])
    r = client.post("/api/projects", json={"name": "x", "product": "Alpha"}, headers=admin_headers)
    it = r.json(); pid = it["id"]; orig = it["itemKey"]
    # client tries to set an arbitrary key while NOT changing the product -> ignored
    r = client.put(f"/api/projects/{pid}", json={**it, "itemKey": "HACK-999"}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["itemKey"] == orig
