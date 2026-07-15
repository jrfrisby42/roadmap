"""Public intake portal (Tier 2): unauthenticated /report ticket creation.

A team opts in via `intakeEnabled`; `intakeTypes` restricts the offered Types
(empty = all). The three /api/intake/* endpoints are PUBLIC (no auth). Submissions
are rate-limited, type-restricted, and land at the team's default status.
"""
import json

import server


def _set(client, admin_headers, key, value):
    return client.put(f"/api/config/{key}", json=value, headers=admin_headers)


def _expose(client, admin_headers, types=None, projects=None):
    _set(client, admin_headers, "statuses", ["New", "In Progress", "Released"])
    _set(client, admin_headers, "statusIsDefault", {"New": True})
    _set(client, admin_headers, "types", [{"name": "Bug"}, {"name": "Feature"}, {"name": "Request"}])
    _set(client, admin_headers, "products", [{"name": "Fraznet"}, {"name": "HubSpot"}])
    _set(client, admin_headers, "departments", ["Sales", "Ops"])
    if types is not None:
        _set(client, admin_headers, "intakeTypes", types)
    if projects is not None:
        _set(client, admin_headers, "intakeProjects", projects)
    _set(client, admin_headers, "intakeEnabled", True)


# ── config plumbing ───────────────────────────────────────────────────────────
def test_intake_keys_are_settable(client, admin_headers):
    assert _set(client, admin_headers, "intakeEnabled", True).status_code == 200
    assert _set(client, admin_headers, "intakeTypes", ["Bug"]).status_code == 200


def test_intake_config_is_admin_only(client, editor_headers):
    assert _set(client, editor_headers, "intakeEnabled", True).status_code == 403


# ── discovery endpoints ───────────────────────────────────────────────────────
def test_disabled_team_not_listed_and_config_404(client, team, admin_headers):
    # Default: not exposed → none of this team's projects appear.
    projs = client.get("/api/intake/projects").json()["projects"]
    assert not any(p["team"] == team for p in projs)
    assert client.get(f"/api/intake/config/{team}").status_code == 404


def test_exposed_projects_listed_with_config(client, team, admin_headers):
    _expose(client, admin_headers, types=["Bug", "Request"], projects=["Fraznet"])
    projs = client.get("/api/intake/projects").json()["projects"]   # PUBLIC, no auth
    mine = [p for p in projs if p["team"] == team]
    assert [p["product"] for p in mine] == ["Fraznet"]              # only the exposed project
    cfg = client.get(f"/api/intake/config/{team}").json()
    assert cfg["types"] == ["Bug", "Request"]                       # restricted Types allowlist
    assert cfg["departments"] == ["Sales", "Ops"]                   # driven by the team's departments
    assert cfg["projects"] == ["Fraznet"]


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
                    json={"title": "High thing", "email": "a@b.com", "priority": "2"})
    assert r.status_code == 200
    it = next(p for p in client.get("/api/all", headers=admin_headers).json()["projects"]
              if p["name"] == "High thing")
    assert it["priority"] == "2"


def test_submit_rejects_urgent_and_invalid_priority(client, team, admin_headers):
    # The portal only offers High(2)/Medium(3)/Low(4) — Urgent(1) and junk are dropped.
    _expose(client, admin_headers, types=["Bug"])
    for bad in ("1", "99"):
        server._rate.clear()
        r = client.post(f"/api/intake/{team}",
                        json={"title": f"prio {bad}", "email": "a@b.com", "priority": bad})
        assert r.status_code == 200
        it = next(p for p in client.get("/api/all", headers=admin_headers).json()["projects"]
                  if p["name"] == f"prio {bad}")
        assert it["priority"] == ""


# ── 4.14.2: portal attachments (public presign + submit-records) ──────────────
def test_intake_presign_validation(client, team, admin_headers):
    # Disabled team → 404 (before any S3 call).
    server._rate.clear()
    assert client.post(f"/api/intake/{team}/attach",
                       json={"filename": "a.png", "contentType": "image/png", "size": 10}).status_code == 404
    _expose(client, admin_headers, types=["Bug"])
    server._rate.clear()
    # Disallowed content-type → 415.
    assert client.post(f"/api/intake/{team}/attach",
                       json={"filename": "a.exe", "contentType": "application/x-msdownload",
                             "size": 10}).status_code == 415
    server._rate.clear()
    # Oversized → 413.
    assert client.post(f"/api/intake/{team}/attach",
                       json={"filename": "big.png", "contentType": "image/png",
                             "size": 99_000_000}).status_code == 413


def test_submit_records_intake_attachment_and_rejects_foreign_key(client, team, admin_headers):
    _expose(client, admin_headers, types=["Bug"])
    server._rate.clear()
    good = {"attId": "abc123", "key": f"intake/{team}/abc123/shot.png",
            "name": "shot.png", "contentType": "image/png", "size": 100}
    foreign = {"attId": "x", "key": "items/5/x/secret.png", "name": "secret.png", "size": 1}
    r = client.post(f"/api/intake/{team}",
                    json={"title": "With shot", "email": "a@b.com", "attachments": [good, foreign]})
    assert r.status_code == 200
    it = next(p for p in client.get("/api/all", headers=admin_headers).json()["projects"]
              if p["name"] == "With shot")
    keys = [a["key"] for a in (it.get("attachments") or [])]
    assert f"intake/{team}/abc123/shot.png" in keys        # our-prefix key kept
    assert not any(k.startswith("items/") for k in keys)   # foreign key dropped


# ── 4.15.0: project + department on submission ────────────────────────────────
def test_submit_records_project_and_department(client, team, admin_headers):
    _expose(client, admin_headers, types=["Bug"], projects=["Fraznet", "HubSpot"])
    server._rate.clear()
    r = client.post(f"/api/intake/{team}",
                    json={"title": "P item", "email": "a@b.com",
                          "product": "HubSpot", "department": "Sales"})
    assert r.status_code == 200
    it = next(p for p in client.get("/api/all", headers=admin_headers).json()["projects"]
              if p["name"] == "P item")
    assert it["product"] == "HubSpot"
    assert it["departments"] == ["Sales"]


def test_submit_rejects_unexposed_project(client, team, admin_headers):
    _expose(client, admin_headers, types=["Bug"], projects=["Fraznet"])   # only Fraznet exposed
    server._rate.clear()
    r = client.post(f"/api/intake/{team}",
                    json={"title": "x", "email": "a@b.com", "product": "HubSpot"})
    assert r.status_code == 422


def test_submit_ignores_unknown_department(client, team, admin_headers):
    _expose(client, admin_headers, types=["Bug"])
    server._rate.clear()
    r = client.post(f"/api/intake/{team}",
                    json={"title": "dept x", "email": "a@b.com", "department": "Nope"})
    assert r.status_code == 200
    it = next(p for p in client.get("/api/all", headers=admin_headers).json()["projects"]
              if p["name"] == "dept x")
    assert it["departments"] == []


# ── 4.16.0: confirmation emails + public ticket status page ───────────────────
def test_intake_notify_email_settable(client, admin_headers):
    assert client.put("/api/config/intakeNotifyEmail", json="ops@example.com",
                      headers=admin_headers).status_code == 200


def test_ticket_status_page_token_gated(client, team, admin_headers):
    _expose(client, admin_headers, types=["Bug"])
    server._rate.clear()
    pid = client.post(f"/api/intake/{team}",
                      json={"title": "Track me", "email": "a@b.com"}).json()["id"]
    tok = server._ticket_token(team, pid)
    ok = client.get(f"/ticket?team={team}&id={pid}&t={tok}")
    assert ok.status_code == 200 and "Track me" in ok.text        # valid token shows the ticket
    assert client.get(f"/ticket?team={team}&id={pid}&t=deadbeef").status_code == 404  # bad token
    assert client.get(f"/ticket?team={team}&id={pid}").status_code == 404             # no token


def test_ticket_status_page_hides_internal_fields(client, team, admin_headers):
    # A submitter-safe view: it must not leak internal-only fields like the owner.
    _expose(client, admin_headers, types=["Bug"])
    server._rate.clear()
    pid = client.post(f"/api/intake/{team}",
                      json={"title": "NoLeak", "email": "a@b.com"}).json()["id"]
    # stamp an owner on the item, then confirm it isn't rendered on the public page
    with server.db(team) as c:
        import json as _json
        d = _json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])
        d["dev"] = "SecretOwnerName"
        server._save_project(c, pid, d)
    r = client.get(f"/ticket?team={team}&id={pid}&t={server._ticket_token(team, pid)}")
    assert r.status_code == 200
    assert "SecretOwnerName" not in r.text
