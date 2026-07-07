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


def test_xteam_without_token_is_rejected(client, team):
    """SECURITY (4.10.3): the old X-Team-only viewer fallback was removed — it
    allowed unauthenticated cross-tenant reads (enumerate slugs via /api/teams,
    then dump /api/all with just an X-Team header). A verified token is now
    required; X-Team alone must be 401, not a viewer session."""
    r = client.get("/api/all", headers={"X-Team": team})
    assert r.status_code == 401
    # And a mutation with only X-Team is likewise rejected (401 at auth, not 403 at role).
    r2 = client.delete("/api/projects/1", headers={"X-Team": team})
    assert r2.status_code == 401


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


# ── T2 (4.11.0): user revocation is enforced at login AND on live tokens ────────
import json as _json


def _append_user(team, user):
    with server.db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
        users = _json.loads(row["value"]) if row else []
        users.append(user)
        c.execute("UPDATE config SET value=? WHERE key='users'", (_json.dumps(users),))


def test_revoked_user_live_token_rejected(client, team):
    # A revoked user's EXISTING token must stop working immediately (not just at
    # next login) — require_auth checks revocation per request.
    _append_user(team, {"username": "gone", "role": "editor",
                        "revokedAt": "2026-01-01T00:00:00Z"})
    tok = server.create_token(team, "gone", "editor")
    r = client.get("/api/all", headers={"Authorization": f"Bearer {tok}", "X-Team": team})
    assert r.status_code == 401


def test_revoked_user_login_rejected(client, team):
    _append_user(team, {"username": "gone2", "role": "editor",
                        "password": server.hash_password("secret123"),
                        "revokedAt": "2026-01-01T00:00:00Z"})
    r = client.post("/api/login", json={"team": team, "username": "gone2",
                                        "password": "secret123"})
    assert r.status_code == 401   # and generic message (folded into not-found path)


def test_non_revoked_user_token_still_works(client, team):
    # Revocation enforcement must NOT break ordinary auth for live users.
    tok = server.create_token(team, "admin", "admin")
    r = client.get("/api/all", headers={"Authorization": f"Bearer {tok}", "X-Team": team})
    assert r.status_code == 200
