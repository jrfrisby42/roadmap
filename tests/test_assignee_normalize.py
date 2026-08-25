"""Assignee normalization: a display name is healed to its username at the write chokepoint.

`assignee` is username-keyed everywhere (list/group/filter, contributor scope, notifications,
owner-pod bucketing). Every in-app picker already writes a username, but JSON import (verbatim
blobs) and direct API writes could put a DISPLAY NAME in, which then groups/filters as a second
phantom person (the "two Jake Smiths" incident: one user jacob.smith, plus 425 items literally
holding "Jake Smith"). `_resolve_assignee` heals it on create, update, and import.
"""
import json

import server

JAKE = [{"username": "admin", "role": "admin"},
        {"username": "jacob.smith", "firstName": "Jake", "lastName": "Smith", "role": "editor"}]

TWO_JAKES = JAKE + [{"username": "jake.s", "firstName": "Jake", "lastName": "Smith", "role": "editor"}]


def _set_users(team, users):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES('users',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(users),))


def _create(client, headers, **f):
    return client.post("/api/projects", json={"name": "Item", "status": "Planned", **f}, headers=headers)


def _stored(team, pid):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])


# ── Unit: the resolver itself (users passed explicitly, c unused) ──────────────────

def test_resolve_unit():
    u = [{"username": "jacob.smith", "firstName": "Jake", "lastName": "Smith"}]
    assert server._resolve_assignee(None, "Jake Smith", users=u) == "jacob.smith"   # display -> username
    assert server._resolve_assignee(None, "jacob.smith", users=u) == "jacob.smith"  # username kept
    assert server._resolve_assignee(None, "  jake   smith ", users=u) == "jacob.smith"  # case/space
    assert server._resolve_assignee(None, "Ghost User", users=u) == "Ghost User"    # unknown kept
    assert server._resolve_assignee(None, "", users=u) == ""
    assert server._resolve_assignee(None, None, users=u) is None


def test_resolve_unit_ambiguous_left_alone():
    u = [{"username": "jacob.smith", "firstName": "Jake", "lastName": "Smith"},
         {"username": "jake.s", "firstName": "Jake", "lastName": "Smith"}]
    assert server._resolve_assignee(None, "Jake Smith", users=u) == "Jake Smith"    # two matches -> no guess


# ── Create ─────────────────────────────────────────────────────────────────────────

def test_create_maps_display_name(client, team, admin_headers):
    _set_users(team, JAKE)
    pid = _create(client, admin_headers, assignee="Jake Smith").json()["id"]
    assert _stored(team, pid)["assignee"] == "jacob.smith"


def test_create_keeps_username(client, team, admin_headers):
    _set_users(team, JAKE)
    pid = _create(client, admin_headers, assignee="jacob.smith").json()["id"]
    assert _stored(team, pid)["assignee"] == "jacob.smith"


def test_create_case_and_whitespace(client, team, admin_headers):
    _set_users(team, JAKE)
    pid = _create(client, admin_headers, assignee="  jake   smith ").json()["id"]
    assert _stored(team, pid)["assignee"] == "jacob.smith"


def test_create_leaves_unknown(client, team, admin_headers):
    _set_users(team, JAKE)
    pid = _create(client, admin_headers, assignee="Somebody Else").json()["id"]
    assert _stored(team, pid)["assignee"] == "Somebody Else"


def test_create_ambiguous_unchanged(client, team, admin_headers):
    _set_users(team, TWO_JAKES)
    pid = _create(client, admin_headers, assignee="Jake Smith").json()["id"]
    assert _stored(team, pid)["assignee"] == "Jake Smith"   # two Jakes -> never guess


def test_create_blank_unchanged(client, team, admin_headers):
    _set_users(team, JAKE)
    pid = _create(client, admin_headers, assignee="").json()["id"]
    assert (_stored(team, pid).get("assignee") or "") == ""


# ── Update (PUT) ─────────────────────────────────────────────────────────────────────

def test_update_maps_display_name(client, team, admin_headers):
    _set_users(team, JAKE)
    pid = _create(client, admin_headers, assignee="jacob.smith").json()["id"]
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "Planned", "assignee": "Jake Smith"}, headers=admin_headers)
    assert r.status_code == 200
    assert _stored(team, pid)["assignee"] == "jacob.smith"


def test_update_does_not_add_assignee_when_never_set(client, team, admin_headers):
    """An item with no assignee must not gain an assignee:null key from the guard."""
    _set_users(team, JAKE)
    pid = _create(client, admin_headers).json()["id"]
    assert "assignee" not in _stored(team, pid) or _stored(team, pid).get("assignee") in (None, "")
    r = client.put(f"/api/projects/{pid}", json={"name": "Item", "status": "Planned"}, headers=admin_headers)
    assert r.status_code == 200


# ── Bulk import (JSON restore) - the primary re-introduction vector ────────────────────

def test_bulk_import_heals_display_name(client, team, admin_headers):
    body = {"projects": [{"name": "A", "status": "Planned", "assignee": "Jake Smith"},
                         {"name": "B", "status": "Planned", "assignee": "jacob.smith"}],
            "users": JAKE}
    r = client.post("/api/import", json=body, headers=admin_headers)
    assert r.status_code == 200
    with server.db(team) as c:
        got = sorted(json.loads(x["data"])["assignee"] for x in c.execute("SELECT data FROM projects").fetchall())
    assert got == ["jacob.smith", "jacob.smith"]   # both normalized/kept as the username
