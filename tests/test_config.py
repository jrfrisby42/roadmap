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


# ── H3: PUT /api/config/users must not bypass the primary-admin protection ─────
import json as _json

import server


def _set_users(client, headers, users):
    return client.put("/api/config/users", json=users, headers=headers)


def _raw_users(team):
    with server.db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
    return _json.loads(row["value"])


def _add_second_admin(client, team, admin_headers):
    """The primary (builtin) admin adds a second, NON-builtin admin. Returns admin2's token."""
    pub = [{k: u.get(k) for k in ("username", "role", "builtin", "email")} for u in _raw_users(team)]
    pub.append({"username": "admin2", "role": "admin", "password": "adminpw2"})
    assert _set_users(client, admin_headers, pub).status_code == 200
    return {"Authorization": f"Bearer {server.create_token(team, 'admin2', 'admin')}", "X-Team": team}


def test_config_users_nonprimary_cannot_takeover_primary(client, team, admin_headers):
    h2 = _add_second_admin(client, team, admin_headers)
    # admin2 (non-primary) tries to set the builtin admin's password AND grant itself builtin.
    attack = [
        {"username": "admin",  "role": "admin", "builtin": True, "password": "pwned123"},
        {"username": "admin2", "role": "admin", "builtin": True, "password": "adminpw2"},
    ]
    assert _set_users(client, h2, attack).status_code == 200   # accepted, but neutralised
    # The primary admin's original password still works; the injected one does not.
    server._rate.clear()
    assert client.post("/api/login", json={"team": team, "username": "admin", "password": "frazil123"}).status_code == 200
    server._rate.clear()
    assert client.post("/api/login", json={"team": team, "username": "admin", "password": "pwned123"}).status_code == 401
    # admin2 did NOT gain builtin (primary) status via the config route.
    assert next(u for u in _raw_users(team) if u["username"] == "admin2").get("builtin") is False


def test_config_users_nonprimary_cannot_remove_primary(client, team, admin_headers):
    h2 = _add_second_admin(client, team, admin_headers)
    # Omitting the builtin admin from the array must be rejected (can't delete the primary).
    r = _set_users(client, h2, [{"username": "admin2", "role": "admin", "password": "adminpw2"}])
    assert r.status_code == 403


def test_config_users_primary_can_still_manage(client, team, admin_headers):
    # Regression: the builtin admin retains full control (can reset another admin's password).
    _add_second_admin(client, team, admin_headers)
    pub = [{k: u.get(k) for k in ("username", "role", "builtin", "email")} for u in _raw_users(team)]
    for u in pub:
        if u["username"] == "admin2":
            u["password"] = "reset-by-primary"
    assert _set_users(client, admin_headers, pub).status_code == 200
    server._rate.clear()
    assert client.post("/api/login",
                       json={"team": team, "username": "admin2", "password": "reset-by-primary"}).status_code == 200
