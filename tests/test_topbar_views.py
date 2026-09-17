"""SHELL-IA-1 Stage 3 - the per-user topbarViews flag (server side).

/api/all returns a per-user `topbarViews` boolean read from the caller's own record in
config['users']. TOPBAR-DEFAULT-1 flipped the default to ON (opt-OUT): an absent flag reads TRUE
(top-bar views for everyone, all Organizations, now and for new users/teams); only an explicit
`false` gives the old left-rail. It is exposed per-user in the users list, and an admin edit that
doesn't carry the flag must not silently drop an explicit value - true OR false - (inherited on save).
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


def test_topbar_views_defaults_true(client, team, admin_headers):
    """TOPBAR-DEFAULT-1: absent flag => ON for everyone. Revert: change the read default back to False and
    this fails (a fresh user would be back on the left rail)."""
    assert client.get("/api/all", headers=admin_headers).json()["topbarViews"] is True


def test_topbar_views_explicit_false_is_the_opt_out(client, team):
    # per-user, from the caller's own record: an explicit false is the ONLY way back to the left rail;
    # any other user (no flag) gets the new default ON.
    _set_user(team, "optout", topbarViews=False)
    assert client.get("/api/all", headers=_hdr(team, "optout")).json()["topbarViews"] is False
    assert client.get("/api/all", headers=_hdr(team, "someone")).json()["topbarViews"] is True


def test_topbar_views_exposed_per_user_in_the_users_list(client, team, admin_headers):
    _set_user(team, "jr", topbarViews=True)
    _set_user(team, "optout", topbarViews=False)
    users = client.get("/api/all", headers=admin_headers).json()["users"]
    assert next(u for u in users if u["username"] == "jr")["topbarViews"] is True
    assert next(u for u in users if u["username"] == "optout")["topbarViews"] is False
    # admin (no explicit flag) shows the new default ON in the list too
    assert next(u for u in users if u["username"] == "admin")["topbarViews"] is True


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


def test_topbar_views_explicit_optout_survives_unrelated_edit(client, team, admin_headers):
    """Under the ON-by-default flip, an explicit false (opt-out) is the load-bearing stored value. An
    unrelated admin edit that doesn't carry the flag must NOT silently wipe it back to the ON default.
    Revert: restore the truthy-only inherit guard and this fails (false gets dropped -> reads ON)."""
    _set_user(team, "optout", topbarViews=False)
    users = _users(team)
    for u in users:
        if u["username"] == "optout":
            u["email"] = "optout@x.com"
            u.pop("topbarViews", None)   # the standard user form doesn't carry it
    assert client.put("/api/config/users", json=users, headers=admin_headers).status_code == 200
    optout = next(u for u in _users(team) if u["username"] == "optout")
    assert optout.get("topbarViews") is False   # opt-out preserved, not wiped to the default
    assert client.get("/api/all", headers=_hdr(team, "optout")).json()["topbarViews"] is False
