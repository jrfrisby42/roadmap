"""Login-by-email must mint a token keyed to the CANONICAL username, not the email.

Regression for the bug where logging in with an email produced a token whose username was the
email, so every username-keyed action mis-matched - most visibly the avatar save returned
404 "User not found".
"""
import json
import server


def test_login_by_email_mints_canonical_username_token(client, team):
    # Seed a user with a username + email + hashed password directly in config.users.
    pw = server.hash_password("secret123")
    with server.db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
        users = json.loads(row["value"])
        users.append({"username": "jdoe", "email": "jane@example.com",
                      "role": "editor", "password": pw})
        c.execute("UPDATE config SET value=? WHERE key='users'", (json.dumps(users),))
    server._rate.clear()

    # Log in BY EMAIL.
    r = client.post("/api/login", json={"team": team, "username": "jane@example.com",
                                        "password": "secret123"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == "jdoe"                       # response already canonical
    assert server.decode_token(body["token"])["username"] == "jdoe"   # token now canonical (the fix)

    # The reported symptom: avatar save (keyed on the token's username) must succeed, not 404.
    hdr = {"Authorization": "Bearer " + body["token"], "X-Team": team}
    ar = client.post("/api/users/self/avatar", json={"color": "#123abc"}, headers=hdr)
    assert ar.status_code == 200, ar.text
    assert ar.json()["avatarColor"] == "#123abc"


def test_login_by_username_still_canonical(client, team):
    # Username login unchanged - token carries the username.
    server._rate.clear()
    r = client.post("/api/login", json={"team": team, "username": "admin", "password": "frazil123"})
    assert r.status_code == 200, r.text
    assert server.decode_token(r.json()["token"])["username"] == "admin"
