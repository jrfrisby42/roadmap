"""SHELL-IA-1 Stage 3 - the per-user topbarViews flag (server side).

/api/all returns a per-user `topbarViews` boolean read from the caller's own record in
config['users']; it defaults false (byte-identical to today); it is exposed per-user in the
users list so an admin toggle can read it; and an admin edit that doesn't carry the flag must
not silently drop it (inherited on save).
"""
import json

import server


def _hdr(team, user, role="admin"):
    return {"Authorization": f"Bearer {server.create_token(team, user, role)}", "X-Team": team}


def _set_user(team, username, **fields):
    with server.db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
        users = json.loads(row["value"]) if row else []
        u = next((x for x in users if x.get("username") == username), None)
        if u is None:
            u = {"username": username, "role": "admin"}
            users.append(u)
        u.update(fields)
        c.execute("INSERT INTO config(key,value) VALUES('users',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(users),))


def _users(team):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT value FROM config WHERE key='users'").fetchone()["value"])


def test_topbar_views_defaults_false(client, team, admin_headers):
    assert client.get("/api/all", headers=admin_headers).json()["topbarViews"] is False


def test_topbar_views_reflects_the_caller_record(client, team):
    _set_user(team, "jr", topbarViews=True)
    assert client.get("/api/all", headers=_hdr(team, "jr")).json()["topbarViews"] is True
    # a DIFFERENT user (no flag) still gets false - it is per-user, from the caller's own record
    assert client.get("/api/all", headers=_hdr(team, "someone")).json()["topbarViews"] is False


def test_topbar_views_exposed_per_user_in_the_users_list(client, team, admin_headers):
    _set_user(team, "jr", topbarViews=True)
    users = client.get("/api/all", headers=admin_headers).json()["users"]
    jr = next(u for u in users if u["username"] == "jr")
    assert jr["topbarViews"] is True


def test_topbar_views_inherited_on_an_unrelated_admin_edit(client, team, admin_headers):
    # jr has the flag on. An admin edits the roster (e.g. changes jr's email) WITHOUT sending
    # topbarViews - the flag must survive rather than be silently wiped.
    _set_user(team, "jr", topbarViews=True)
    users = _users(team)
    for u in users:
        if u["username"] == "jr":
            u["email"] = "jr@x.com"
            u.pop("topbarViews", None)   # the standard user form doesn't carry it
    r = client.put("/api/config/users", json=users, headers=admin_headers)
    assert r.status_code == 200
    jr = next(u for u in _users(team) if u["username"] == "jr")
    assert jr.get("topbarViews") is True   # inherited, not dropped
