"""Self-service password reset + new-user invite (Amazon SES via SMTP).

No SMTP in the test env, so these monkeypatch `mail_configured`/`send_email` to
capture the outgoing mail instead of sending it.
"""
import time

import server


# ── helpers ────────────────────────────────────────────────────────────────
def _set_users(client, admin_headers, users):
    return client.put("/api/config/users", json=users, headers=admin_headers)


def _get_user(team, username):
    with server.db(team) as c:
        users = __import__("json").loads(
            c.execute("SELECT value FROM config WHERE key='users'").fetchone()["value"])
    return next(u for u in users if u["username"] == username)


# ── token helper: single-use bind + expiry ──────────────────────────────────
def test_password_token_roundtrip_and_bind():
    tok = server.make_password_token("teamx", "alice", "reset", "hash-v1", 3600)
    d = server.decode_password_token(tok)
    assert d["team"] == "teamx" and d["username"] == "alice" and d["purpose"] == "reset"
    # Bind is derived from the password hash → changes when the hash changes.
    assert d["bind"] == server._pw_token_bind("hash-v1")
    assert d["bind"] != server._pw_token_bind("hash-v2")


def test_password_token_expired_rejected():
    tok = server.make_password_token("teamx", "alice", "reset", "h", -1)  # already expired
    try:
        server.decode_password_token(tok)
        assert False, "expected rejection"
    except server.HTTPException as e:
        assert e.status_code == 400


def test_password_token_tampered_rejected():
    tok = server.make_password_token("teamx", "alice", "reset", "h", 3600)
    try:
        server.decode_password_token(tok + "x")
        assert False, "expected rejection"
    except server.HTTPException as e:
        assert e.status_code == 400


# ── email uniqueness + login by email ────────────────────────────────────────
def test_duplicate_email_rejected(client, admin_headers):
    r = _set_users(client, admin_headers, [
        {"username": "a", "role": "editor", "email": "dup@frazil.com", "password": "secret1"},
        {"username": "b", "role": "editor", "email": "DUP@frazil.com", "password": "secret2"},
    ])
    assert r.status_code == 422


def test_invalid_email_rejected(client, admin_headers):
    r = _set_users(client, admin_headers,
                   [{"username": "a", "role": "editor", "email": "not-an-email", "password": "secret1"}])
    assert r.status_code == 422


def test_login_by_email(client, team, admin_headers):
    _set_users(client, admin_headers,
               [{"username": "alice", "role": "editor", "email": "alice@frazil.com", "password": "secret1"}])
    server._rate.clear()
    r = client.post("/api/login", json={"team": team, "username": "alice@frazil.com", "password": "secret1"})
    assert r.status_code == 200
    assert r.json()["role"] == "editor"


def test_email_surfaced_in_get_all(client, admin_headers):
    _set_users(client, admin_headers,
               [{"username": "alice", "role": "editor", "email": "alice@frazil.com", "password": "secret1"}])
    users = client.get("/api/all", headers=admin_headers).json()["users"]
    alice = next(u for u in users if u["username"] == "alice")
    assert alice["email"] == "alice@frazil.com"
    assert "password" not in alice


# ── forgot-password: uniform response, sends only when matched + configured ──
def test_forgot_password_uniform_response(client, team, admin_headers, monkeypatch):
    sent = []
    monkeypatch.setattr(server, "mail_configured", lambda: True)
    monkeypatch.setattr(server, "send_email", lambda *a, **k: sent.append(a))
    _set_users(client, admin_headers,
               [{"username": "alice", "role": "editor", "email": "alice@frazil.com", "password": "secret1"}])

    server._rate.clear()
    r_known = client.post("/api/forgot-password", json={"team": team, "email": "alice@frazil.com"})
    server._rate.clear()
    r_unknown = client.post("/api/forgot-password", json={"team": team, "email": "nobody@frazil.com"})

    assert r_known.status_code == r_unknown.status_code == 200
    assert r_known.json() == r_unknown.json()      # response must not reveal existence
    assert len(sent) == 1                          # but only the real address gets mail
    assert "alice@frazil.com" in sent[0][0]


def test_forgot_password_noop_when_unconfigured(client, team, admin_headers, monkeypatch):
    monkeypatch.setattr(server, "mail_configured", lambda: False)
    _set_users(client, admin_headers,
               [{"username": "alice", "role": "editor", "email": "alice@frazil.com", "password": "secret1"}])
    server._rate.clear()
    r = client.post("/api/forgot-password", json={"team": team, "email": "alice@frazil.com"})
    assert r.status_code == 200   # still uniform, just doesn't send


# ── reset-password: sets pw, single-use, validates ──────────────────────────
def test_reset_password_flow_and_single_use(client, team, admin_headers):
    _set_users(client, admin_headers,
               [{"username": "alice", "role": "editor", "email": "alice@frazil.com", "password": "oldpass1"}])
    current_hash = _get_user(team, "alice")["password"]
    token = server.make_password_token(team, "alice", "reset", current_hash, 3600)

    r = client.post("/api/reset-password", json={"token": token, "password": "brandnew2"})
    assert r.status_code == 200

    # New password works...
    server._rate.clear()
    assert client.post("/api/login", json={"team": team, "username": "alice",
                                           "password": "brandnew2"}).status_code == 200
    # ...and the same link can't be reused (bind no longer matches the new hash).
    r2 = client.post("/api/reset-password", json={"token": token, "password": "another3"})
    assert r2.status_code == 400


def test_reset_password_short_rejected(client, team, admin_headers):
    _set_users(client, admin_headers,
               [{"username": "alice", "role": "editor", "email": "a@frazil.com", "password": "oldpass1"}])
    token = server.make_password_token(team, "alice", "reset", _get_user(team, "alice")["password"], 3600)
    r = client.post("/api/reset-password", json={"token": token, "password": "123"})
    assert r.status_code == 400


# ── invite: admin-gated, needs email + configured mail ──────────────────────
def test_send_invite_admin_only(client, team, admin_headers, editor_headers, monkeypatch):
    sent = []
    monkeypatch.setattr(server, "mail_configured", lambda: True)
    monkeypatch.setattr(server, "send_email", lambda *a, **k: sent.append(a))
    # New user with an email but NO password = pending invite.
    _set_users(client, admin_headers,
               [{"username": "newbie", "role": "editor", "email": "newbie@frazil.com"}])

    assert client.post("/api/users/newbie/send-invite", headers=editor_headers).status_code == 403
    assert client.post("/api/users/newbie/send-invite", headers=admin_headers).status_code == 200
    assert len(sent) == 1 and "newbie@frazil.com" in sent[0][0]


def test_send_invite_requires_email(client, admin_headers, monkeypatch):
    monkeypatch.setattr(server, "mail_configured", lambda: True)
    monkeypatch.setattr(server, "send_email", lambda *a, **k: None)
    _set_users(client, admin_headers, [{"username": "noemail", "role": "editor", "password": "secret1"}])
    r = client.post("/api/users/noemail/send-invite", headers=admin_headers)
    assert r.status_code == 400


def test_send_invite_503_when_unconfigured(client, admin_headers, monkeypatch):
    monkeypatch.setattr(server, "mail_configured", lambda: False)
    _set_users(client, admin_headers,
               [{"username": "newbie", "role": "editor", "email": "newbie@frazil.com"}])
    r = client.post("/api/users/newbie/send-invite", headers=admin_headers)
    assert r.status_code == 503
