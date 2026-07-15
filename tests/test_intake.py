"""Public intake portal (Tier 2): unauthenticated /report ticket creation.

A team opts in via `intakeEnabled`; `intakeTypes` restricts the offered Types
(empty = all). The three /api/intake/* endpoints are PUBLIC (no auth). Submissions
are rate-limited, type-restricted, and land at the team's default status.
"""
import json

import server


def _set(client, admin_headers, key, value):
    return client.put(f"/api/config/{key}", json=value, headers=admin_headers)


def _expose(client, admin_headers, types=None):
    _set(client, admin_headers, "statuses", ["New", "In Progress", "Released"])
    _set(client, admin_headers, "statusIsDefault", {"New": True})
    _set(client, admin_headers, "types", [{"name": "Bug"}, {"name": "Feature"}, {"name": "Request"}])
    if types is not None:
        _set(client, admin_headers, "intakeTypes", types)
    _set(client, admin_headers, "intakeEnabled", True)


# ── config plumbing ───────────────────────────────────────────────────────────
def test_intake_keys_are_settable(client, admin_headers):
    assert _set(client, admin_headers, "intakeEnabled", True).status_code == 200
    assert _set(client, admin_headers, "intakeTypes", ["Bug"]).status_code == 200


def test_intake_config_is_admin_only(client, editor_headers):
    assert _set(client, editor_headers, "intakeEnabled", True).status_code == 403


# ── discovery endpoints ───────────────────────────────────────────────────────
def test_disabled_team_not_listed_and_config_404(client, team, admin_headers):
    # Default: not exposed.
    teams = client.get("/api/intake/teams").json()["teams"]
    assert not any(t["slug"] == team for t in teams)
    assert client.get(f"/api/intake/config/{team}").status_code == 404


def test_exposed_team_listed_with_types(client, team, admin_headers):
    _expose(client, admin_headers, types=["Bug", "Request"])
    teams = client.get("/api/intake/teams").json()["teams"]         # PUBLIC, no auth
    assert any(t["slug"] == team for t in teams)
    cfg = client.get(f"/api/intake/config/{team}").json()
    assert cfg["types"] == ["Bug", "Request"]                       # restricted allowlist


def test_empty_allowlist_offers_all_types(client, team, admin_headers):
    _expose(client, admin_headers, types=[])
    cfg = client.get(f"/api/intake/config/{team}").json()
    assert cfg["types"] == ["Bug", "Feature", "Request"]


# ── submission ────────────────────────────────────────────────────────────────
def test_public_submit_creates_item_at_default_status(client, team, admin_headers):
    _expose(client, admin_headers, types=["Bug"])
    server._rate.clear()
    r = client.post(f"/api/intake/{team}",                          # NO auth headers
                    json={"title": "Login is broken", "description": "500 on submit",
                          "type": "Bug", "email": "reporter@example.com", "name": "Pat"})
    assert r.status_code == 200, r.text
    assert r.json()["itemKey"]
    # The item exists, at the default status, with reporter contact captured.
    allr = client.get("/api/all", headers=admin_headers).json()
    it = next(p for p in allr["projects"] if p["name"] == "Login is broken")
    assert it["status"] == "New" and it["type"] == "Bug"
    assert it["reporter"] == "Pat" and it["reporterEmail"] == "reporter@example.com"
    assert it["source"] == "portal"


def test_submit_requires_title_and_valid_email(client, team, admin_headers):
    _expose(client, admin_headers, types=["Bug"])
    server._rate.clear()
    assert client.post(f"/api/intake/{team}",
                       json={"title": "", "email": "a@b.com"}).status_code == 422
    server._rate.clear()
    assert client.post(f"/api/intake/{team}",
                       json={"title": "x", "email": "not-an-email"}).status_code == 422


def test_submit_to_disabled_team_404(client, team, admin_headers):
    server._rate.clear()
    assert client.post(f"/api/intake/{team}",
                       json={"title": "x", "email": "a@b.com"}).status_code == 404


def test_submit_coerces_disallowed_type(client, team, admin_headers):
    _expose(client, admin_headers, types=["Bug"])           # only Bug offered
    server._rate.clear()
    r = client.post(f"/api/intake/{team}",
                    json={"title": "T", "email": "a@b.com", "type": "Feature"})  # not allowed
    assert r.status_code == 200
    allr = client.get("/api/all", headers=admin_headers).json()
    it = next(p for p in allr["projects"] if p["name"] == "T")
    assert it["type"] == "Bug"                              # coerced to an allowed Type


def test_report_page_served(client):
    r = client.get("/report")
    assert r.status_code == 200
    assert "Submit a ticket" in r.text


# ── 4.14.1: priority flag on the portal ───────────────────────────────────────
def test_submit_captures_priority(client, team, admin_headers):
    _expose(client, admin_headers, types=["Bug"])
    server._rate.clear()
    r = client.post(f"/api/intake/{team}",
                    json={"title": "Urgent thing", "email": "a@b.com", "priority": "1"})
    assert r.status_code == 200
    it = next(p for p in client.get("/api/all", headers=admin_headers).json()["projects"]
              if p["name"] == "Urgent thing")
    assert it["priority"] == "1"


def test_submit_ignores_invalid_priority(client, team, admin_headers):
    _expose(client, admin_headers, types=["Bug"])
    server._rate.clear()
    r = client.post(f"/api/intake/{team}",
                    json={"title": "Bad prio", "email": "a@b.com", "priority": "99"})
    assert r.status_code == 200
    it = next(p for p in client.get("/api/all", headers=admin_headers).json()["projects"]
              if p["name"] == "Bad prio")
    assert it["priority"] == ""
