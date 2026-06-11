"""Config writes: the VALID_KEYS allowlist and round-tripping through /api/all."""


def test_unknown_config_key_rejected(client, admin_headers):
    r = client.put("/api/config/totally_made_up_key", json={"x": 1}, headers=admin_headers)
    assert r.status_code == 400


def test_valid_config_key_round_trips(client, admin_headers):
    flags = {"In Progress": True, "Released": False}
    r = client.put("/api/config/statusIsActive", json=flags, headers=admin_headers)
    assert r.status_code == 200

    allr = client.get("/api/all", headers=admin_headers)
    assert allr.status_code == 200
    assert allr.json()["statusIsActive"] == flags


def test_product_ignore_conflicts_round_trips(client, admin_headers):
    # Project-level conflict exemption (small single-assignee projects).
    flags = {"Fraznet": True, "HubSpot": False}
    r = client.put("/api/config/productIgnoreConflicts", json=flags, headers=admin_headers)
    assert r.status_code == 200
    body = client.get("/api/all", headers=admin_headers).json()
    assert body["productIgnoreConflicts"] == flags


def test_get_all_shape(client, admin_headers):
    """The frontend depends on /api/all returning these top-level keys in one call."""
    body = client.get("/api/all", headers=admin_headers).json()
    for key in ("projects", "statuses", "users", "statusIsActive", "statusIsDefault"):
        assert key in body
    # users must be the sanitised shape (no password field leaks).
    for u in body["users"]:
        assert "password" not in u
