"""FIELDS-2 server surface.

Two server-side pieces: the `blockedReason` row filter in list_items (was missing, so
blockedReason=<value> matched every row - a live defect + the group-expansion gap), and the
`hasSprints` / `hasReleases` booleans in /api/all that let the client gate the Sprint / Release
fields at boot without a lazy-load flash. The rest of FIELDS-2 (chips, group-by removal, the
Planning-off gate) is frontend-only and verified in the browser.
"""
import json

import server


def _mk(client, headers, **f):
    return client.post("/api/projects", json={"name": "I", "status": "New", **f}, headers=headers).json()


# ── blockedReason row filter (the defect) ────────────────────────────────────

def test_blocked_reason_filter_returns_only_matches(client, team, admin_headers):
    _mk(client, admin_headers, blockedReason="Vendor")
    _mk(client, admin_headers, blockedReason="Vendor")
    _mk(client, admin_headers, blockedReason="User")
    _mk(client, admin_headers)  # no reason
    allc = client.get("/api/items", headers=admin_headers).json()["total"]
    vend = client.get("/api/items?blockedReason=Vendor", headers=admin_headers).json()
    assert allc == 4                                   # baseline
    assert vend["total"] == 2                          # NOT all 4 (the pre-fix bug returned everything)
    assert all(it.get("blockedReason") == "Vendor" for it in vend["items"])


def test_blocked_reason_filter_none_bucket(client, team, admin_headers):
    _mk(client, admin_headers, blockedReason="Vendor")
    _mk(client, admin_headers)  # unset
    r = client.get("/api/items?blockedReason=__none__", headers=admin_headers).json()
    assert r["total"] == 1 and (r["items"][0].get("blockedReason") or "") == ""


def test_blocked_reason_filter_multi(client, team, admin_headers):
    for rn in ("Vendor", "User", "Approval"):
        _mk(client, admin_headers, blockedReason=rn)
    r = client.get("/api/items?blockedReason=Vendor,User", headers=admin_headers).json()
    assert r["total"] == 2 and {it["blockedReason"] for it in r["items"]} == {"Vendor", "User"}


# ── hasSprints / hasReleases in /api/all ─────────────────────────────────────

def test_has_sprints_releases_default_false(client, team, admin_headers):
    d = client.get("/api/all", headers=admin_headers).json()
    assert d.get("hasSprints") is False
    assert d.get("hasReleases") is False


def _set_cfg(team, key, val):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(val)))


def test_has_sprints_true_when_one_exists(client, team, admin_headers):
    _set_cfg(team, "sprints", [{"id": "s1", "name": "Sprint 1"}])
    d = client.get("/api/all", headers=admin_headers).json()
    assert d["hasSprints"] is True
    assert d["hasReleases"] is False   # releases still empty - keyed independently


def test_has_releases_true_when_one_exists(client, team, admin_headers):
    _set_cfg(team, "releases", [{"id": "r1", "name": "R1"}])
    d = client.get("/api/all", headers=admin_headers).json()
    assert d["hasReleases"] is True
    assert d["hasSprints"] is False
