"""Login flow, token/X-Team handling, the viewer fallback, and rate limiting."""
import server


def _login(client, team, username="admin", password="frazil123"):
    return client.post("/api/login", json={"team": team, "username": username,
                                            "password": password})


def test_login_success(client, team):
    r = _login(client, team)
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "admin"
    assert body["token"]
    # Default admin is provisioned with a forced password change.
    assert body["mustChangePassword"] is True


def test_login_wrong_password(client, team):
    r = _login(client, team, password="wrong-password")
    assert r.status_code == 401


def test_login_unknown_team(client):
    r = client.post("/api/login", json={"team": "nosuchteam",
                                        "username": "admin", "password": "frazil123"})
    assert r.status_code == 400


def test_no_auth_is_rejected(client):
    # No Authorization header and no X-Team -> 401.
    r = client.get("/api/all")
    assert r.status_code == 401


def test_xteam_fallback_is_viewer(client, team):
    """X-Team without a token grants read-only viewer access (documented back-compat)."""
    r = client.get("/api/all", headers={"X-Team": team})
    assert r.status_code == 200  # viewers can read
    # ...but cannot perform an admin-only action.
    r2 = client.delete("/api/projects/1", headers={"X-Team": team})
    assert r2.status_code == 403


def test_token_team_must_match_xteam(client, team):
    token = server.create_token(team, "admin", "admin")
    r = client.get("/api/all", headers={"Authorization": f"Bearer {token}",
                                        "X-Team": "differentteam"})
    assert r.status_code == 403


def test_login_rate_limited(client, team):
    server._rate.clear()
    statuses = [_login(client, team, password="bad").status_code
                for _ in range(server.RATE_MAX + 3)]
    # Once the window's RATE_MAX is exceeded, further attempts get 429.
    assert 429 in statuses
    server._rate.clear()  # don't leak the tripped limiter into later tests
