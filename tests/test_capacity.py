"""Capacity overrides: upsert/validation, effective-capacity resolution, the
default-capacity ceiling, batch upsert, delete, and role gating."""


def _set_owner_capacity(client, admin_headers, mapping):
    """Set the team's ownerCapacity defaults (e.g. {'Alice': 2.0})."""
    return client.put("/api/config/ownerCapacity", json=mapping, headers=admin_headers)


def test_effective_capacity_falls_back_to_default(client, admin_headers):
    _set_owner_capacity(client, admin_headers, {"Alice": 2.0})
    r = client.get("/api/capacity-overrides/effective",
                   params={"owner": "Alice", "date": "2026-07-01"}, headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["capacity"] == 2.0
    assert body["default"] == 2.0
    assert body["has_override"] is False


def test_unknown_owner_defaults_to_one(client, admin_headers):
    r = client.get("/api/capacity-overrides/effective",
                   params={"owner": "Nobody", "date": "2026-07-01"}, headers=admin_headers)
    assert r.json()["capacity"] == 1.0


def test_override_upsert_and_effective(client, admin_headers):
    _set_owner_capacity(client, admin_headers, {"Alice": 2.0})
    r = client.post("/api/capacity-overrides",
                    json={"owner": "Alice", "date": "2026-07-01", "capacity": 0.5},
                    headers=admin_headers)
    assert r.status_code == 200

    eff = client.get("/api/capacity-overrides/effective",
                     params={"owner": "Alice", "date": "2026-07-01"}, headers=admin_headers).json()
    assert eff["capacity"] == 0.5
    assert eff["has_override"] is True


def test_override_cannot_exceed_default(client, admin_headers):
    _set_owner_capacity(client, admin_headers, {"Alice": 2.0})
    r = client.post("/api/capacity-overrides",
                    json={"owner": "Alice", "date": "2026-07-01", "capacity": 5.0},
                    headers=admin_headers)
    assert r.status_code == 422


def test_override_rejects_negative(client, admin_headers):
    _set_owner_capacity(client, admin_headers, {"Alice": 2.0})
    r = client.post("/api/capacity-overrides",
                    json={"owner": "Alice", "date": "2026-07-01", "capacity": -1},
                    headers=admin_headers)
    assert r.status_code == 422


def test_override_requires_owner_and_date(client, admin_headers):
    assert client.post("/api/capacity-overrides", json={"date": "2026-07-01", "capacity": 1},
                       headers=admin_headers).status_code == 400
    assert client.post("/api/capacity-overrides", json={"owner": "Alice", "capacity": 1},
                       headers=admin_headers).status_code == 400


def test_override_delete(client, admin_headers):
    _set_owner_capacity(client, admin_headers, {"Alice": 2.0})
    client.post("/api/capacity-overrides",
                json={"owner": "Alice", "date": "2026-07-01", "capacity": 0.5},
                headers=admin_headers)
    r = client.delete("/api/capacity-overrides",
                      params={"owner": "Alice", "date": "2026-07-01"}, headers=admin_headers)
    assert r.status_code == 200
    # Deleting again -> 404 (nothing left).
    r2 = client.delete("/api/capacity-overrides",
                       params={"owner": "Alice", "date": "2026-07-01"}, headers=admin_headers)
    assert r2.status_code == 404


def test_batch_upsert_partial_validation(client, admin_headers):
    _set_owner_capacity(client, admin_headers, {"Alice": 2.0})
    r = client.post("/api/capacity-overrides/batch", json={
        "owner": "Alice",
        "overrides": [
            {"date": "2026-07-01", "capacity": 1.0},   # ok
            {"date": "2026-07-02", "capacity": 9.0},   # exceeds default -> error
        ],
    }, headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["saved"] == 1
    assert len(body["errors"]) == 1


def test_override_role_gating(client, viewer_headers):
    # Viewers cannot write overrides.
    r = client.post("/api/capacity-overrides",
                    json={"owner": "Alice", "date": "2026-07-01", "capacity": 1.0},
                    headers=viewer_headers)
    assert r.status_code == 403
