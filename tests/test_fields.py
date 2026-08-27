"""FIELDS-1: coded Location and Resolution Type fields.

Two per-team value lists (`locations`, `resolutionTypes`) with coded item fields (`location`,
`resolutionType`), applying the blockedReasons shape twice. Server surface covered here: config
(VALID_KEYS, default [], /api/all, restart survival), the group-by row-fetch filter, the recurrence
dual-list split (location inherited, resolutionType not), not-server-owned, and the transfer rule
(location carried only if the site exists on the target; resolutionType always dropped).

The hidden-when-empty rule across the six UI surfaces is frontend-only (verified in the browser).
"""
import json

import server


def _headers(team, username="admin", role="admin"):
    return {"Authorization": f"Bearer {server.create_token(team, username, role)}", "X-Team": team}


def _fresh_team(slug):
    import os
    os.makedirs(os.path.join(server.TENANTS_DIR, slug), exist_ok=True)
    server.init_team_db(slug)
    return slug


def _set_cfg(team, key, val):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(val)))


def _stored(team, pid):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])


# ── Config ──────────────────────────────────────────────────────────────────

def test_keys_in_valid_keys():
    assert "locations" in server.VALID_KEYS
    assert "resolutionTypes" in server.VALID_KEYS


def test_new_team_seeds_empty(client, team, admin_headers):
    d = client.get("/api/all", headers=admin_headers).json()
    assert d.get("locations") == []
    assert d.get("resolutionTypes") == []


def test_put_persists(client, team, admin_headers):
    client.put("/api/config/locations", json=["HQ", "WH2"], headers=admin_headers)
    client.put("/api/config/resolutionTypes", json=["Fixed", "Access Granted"], headers=admin_headers)
    d = client.get("/api/all", headers=admin_headers).json()
    assert d["locations"] == ["HQ", "WH2"]
    assert d["resolutionTypes"] == ["Fixed", "Access Granted"]


def test_put_requires_admin(client, team, editor_headers):
    assert client.put("/api/config/locations", json=["HQ"], headers=editor_headers).status_code == 403
    assert client.put("/api/config/resolutionTypes", json=["Fixed"], headers=editor_headers).status_code == 403


def test_emptied_lists_survive_restart(client, team, admin_headers):
    # Absent from _migrate_config_keys (the blockedReasons mechanism): a populated OR emptied list is
    # never re-seeded on boot.
    client.put("/api/config/locations", json=["HQ"], headers=admin_headers)
    client.put("/api/config/resolutionTypes", json=[], headers=admin_headers)
    server._migrated_teams.discard(team)
    server._migrate_config_keys(team)
    d = client.get("/api/all", headers=admin_headers).json()
    assert d["locations"] == ["HQ"]
    assert d["resolutionTypes"] == []


# ── Dual-list: recurrence + server-owned ─────────────────────────────────────

def test_resolutiontype_not_inherited_location_is(client, team, admin_headers):
    pid = client.post("/api/projects",
                      json={"name": "Item", "status": "Planned", "recurrence": "weekly",
                            "start": "2026-01-01", "dueWeeks": 2,
                            "location": "WH2", "resolutionType": "Fixed"},
                      headers=admin_headers).json()["id"]
    child = client.post(f"/api/projects/{pid}/recur", json={}, headers=admin_headers).json()
    stored = _stored(team, child["id"])
    assert stored.get("location") == "WH2"          # a new occurrence is at the same site
    assert "resolutionType" not in stored           # a new occurrence is NOT resolved


def test_resolutiontype_in_recurrence_skip_location_not():
    assert "resolutionType" in server.RECURRENCE_SKIP_KEYS
    assert "location" not in server.RECURRENCE_SKIP_KEYS


def test_neither_field_is_server_owned():
    assert "location" not in server.SERVER_OWNED_FIELDS
    assert "resolutionType" not in server.SERVER_OWNED_FIELDS


# ── Group-by row-fetch filter (json_extract) ─────────────────────────────────

def test_group_row_filter_by_location(client, team, admin_headers):
    for loc in ("HQ", "WH2", "HQ", ""):
        client.post("/api/projects", json={"name": "I", "status": "New", "location": loc}, headers=admin_headers)
    r = client.get("/api/items?location=HQ", headers=admin_headers).json()
    assert r["total"] == 2 and all(it.get("location") == "HQ" for it in r["items"])
    # __none__ loads the unset bucket
    r2 = client.get("/api/items?location=__none__", headers=admin_headers).json()
    assert all((it.get("location") or "") == "" for it in r2["items"]) and r2["total"] >= 1


def test_group_row_filter_by_resolution_type(client, team, admin_headers):
    for rt in ("Fixed", "Access Granted", "Fixed"):
        client.post("/api/projects", json={"name": "I", "status": "New", "resolutionType": rt}, headers=admin_headers)
    r = client.get("/api/items?resolutionType=Fixed", headers=admin_headers).json()
    assert r["total"] == 2 and all(it.get("resolutionType") == "Fixed" for it in r["items"])


# ── Transfer: location carry-if-exists, resolutionType dropped ────────────────

def _make_item(team, **fields):
    data = {"name": "Ticket", "status": "New", "product": ""}
    data.update(fields)
    with server.db(team) as c:
        server._assign_item_key(c, data)
        data["id"] = server._insert_project(c, data)
    return data["id"]


def _new_target_item(tgt):
    # the transfer creates one item on the target; return its blob
    with server.db(tgt) as c:
        rows = c.execute("SELECT data FROM projects").fetchall()
    return json.loads(rows[-1]["data"]) if rows else None


def _target(name):
    t = _fresh_team(name)
    _set_cfg(t, "intakeEnabled", True)
    _set_cfg(t, "products", [{"name": "Websites"}])
    _set_cfg(t, "intakeProjects", [])
    _set_cfg(t, "statuses", ["New", "Done"])
    _set_cfg(t, "statusIsDefault", {"New": True})
    return t


def test_transfer_carries_location_only_if_target_has_it(client, team, admin_headers):
    tgt = _target("tgtlocmatch")
    _set_cfg(tgt, "locations", ["HQ", "WH2"])            # target knows WH2
    pid = _make_item(team, location="WH2", resolutionType="Fixed")
    r = client.post(f"/api/items/{pid}/transfer",
                    json={"targetTeam": tgt, "targetProject": "Websites"}, headers=admin_headers)
    assert r.status_code == 200
    ti = _new_target_item(tgt)
    assert ti.get("location") == "WH2"                  # physical fact, exact site exists on target -> carried
    assert (ti.get("resolutionType") or "") == ""       # a rerouted ticket is not resolved -> dropped


def test_transfer_drops_location_when_target_lacks_it(client, team, admin_headers):
    tgt = _target("tgtlocnomatch")
    _set_cfg(tgt, "locations", ["Depot"])               # target does NOT have WH2
    pid = _make_item(team, location="WH2", resolutionType="Fixed")
    r = client.post(f"/api/items/{pid}/transfer",
                    json={"targetTeam": tgt, "targetProject": "Websites"}, headers=admin_headers)
    assert r.status_code == 200
    ti = _new_target_item(tgt)
    assert (ti.get("location") or "") == ""             # no matching site -> dropped (Department rule)
    assert (ti.get("resolutionType") or "") == ""       # always dropped
