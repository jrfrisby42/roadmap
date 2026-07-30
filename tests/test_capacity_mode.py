"""capacityMode config key (per-pod fixed|derived capacity source). The resolver itself
(getBaseCapacity) and the sole-member assignee default are client-side JS verified via the
browser harness; here we only guard the server contract: the key is writable (in VALID_KEYS),
round-trips through /api/all, defaults to {} when absent, and is admin-gated like other config."""


def _all(client, headers):
    return client.get("/api/all", headers=headers).json()


def test_capacity_mode_defaults_empty(client, team, admin_headers):
    assert _all(client, admin_headers).get("capacityMode") == {}


def test_capacity_mode_roundtrips(client, team, admin_headers):
    r = client.put("/api/config/capacityMode", json={"Everest": "derived"}, headers=admin_headers)
    assert r.status_code == 200
    assert _all(client, admin_headers)["capacityMode"] == {"Everest": "derived"}


def test_capacity_mode_is_admin_gated(client, team, editor_headers):
    r = client.put("/api/config/capacityMode", json={"Everest": "derived"}, headers=editor_headers)
    assert r.status_code in (401, 403)


def test_owner_capacity_preserved_alongside_mode(client, team, admin_headers):
    # A pod flipped to derived keeps its dormant fixed ownerCapacity value (the editor preserves it);
    # both keys are independent config maps and coexist.
    client.put("/api/config/ownerCapacity", json={"Everest": 3}, headers=admin_headers)
    client.put("/api/config/capacityMode", json={"Everest": "derived"}, headers=admin_headers)
    data = _all(client, admin_headers)
    assert data["ownerCapacity"] == {"Everest": 3}       # dormant fixed value not clobbered
    assert data["capacityMode"] == {"Everest": "derived"}
