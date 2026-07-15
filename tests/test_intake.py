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


# ── 4.16.1: reporter emails on completion / deferral / @reporter comment ──────
import pytest


@pytest.fixture
def mailbox(monkeypatch):
    sent = []
    monkeypatch.setattr(server, "mail_configured", lambda: True)
    monkeypatch.setattr(server, "send_email",
                        lambda to, subj, text, html=None: sent.append((to, subj)))
    return sent


def _portal_ticket(client, team, admin_headers, mailbox, **extra):
    server._rate.clear()
    pid = client.post(f"/api/intake/{team}",
                      json={"title": "T", "email": "rep@x.com", **extra}).json()["id"]
    mailbox.clear()   # drop the creation emails
    return pid


def test_reporter_emailed_on_completion(client, team, admin_headers, mailbox):
    _expose(client, admin_headers, types=["Bug"])
    _set(client, admin_headers, "statusIsTerminal", {"Released": True})
    pid = _portal_ticket(client, team, admin_headers, mailbox)
    r = client.put(f"/api/projects/{pid}", json={"name": "T", "status": "Released"},
                   headers=admin_headers)
    assert r.status_code == 200
    assert any(to == "rep@x.com" and "complete" in subj.lower() for to, subj in mailbox)


def test_reporter_emailed_on_deferral(client, team, admin_headers, mailbox):
    _expose(client, admin_headers, types=["Bug"])
    _set(client, admin_headers, "statuses", ["New", "Deferred"])
    _set(client, admin_headers, "statusIsDeferred", {"Deferred": True})
    pid = _portal_ticket(client, team, admin_headers, mailbox)
    client.put(f"/api/projects/{pid}", json={"name": "T", "status": "Deferred"}, headers=admin_headers)
    assert any(to == "rep@x.com" and "deferred" in subj.lower() for to, subj in mailbox)


def test_reporter_emailed_on_at_reporter_comment(client, team, admin_headers, mailbox):
    _expose(client, admin_headers, types=["Bug"])
    pid = _portal_ticket(client, team, admin_headers, mailbox)
    r = client.post("/api/comments",
                    json={"item_id": pid, "body": "Hi @reporter — which browser were you using?"},
                    headers=admin_headers)
    assert r.status_code == 200
    assert any(to == "rep@x.com" for to, subj in mailbox)


def test_no_reporter_email_without_at_reporter(client, team, admin_headers, mailbox):
    _expose(client, admin_headers, types=["Bug"])
    pid = _portal_ticket(client, team, admin_headers, mailbox)
    client.post("/api/comments", json={"item_id": pid, "body": "internal note, no mention"},
                headers=admin_headers)
    assert not any(to == "rep@x.com" for to, subj in mailbox)


def test_non_portal_item_no_completion_email(client, team, admin_headers, mailbox):
    _set(client, admin_headers, "statuses", ["New", "Released"])
    _set(client, admin_headers, "statusIsTerminal", {"Released": True})
    pid = client.post("/api/projects", json={"name": "Normal", "status": "New"},
                      headers=admin_headers).json()["id"]
    mailbox.clear()
    client.put(f"/api/projects/{pid}", json={"name": "Normal", "status": "Released"},
               headers=admin_headers)
    assert mailbox == []   # not a portal ticket → no reporter email


# ── 4.17.0: "My Tickets" list (signed link) + track-by-email ──────────────────
def test_my_tickets_token_gated_and_scoped(client, team, admin_headers):
    _expose(client, admin_headers, types=["Bug"])
    for title, em in [("Mine A", "me@x.com"), ("Mine B", "me@x.com"), ("Not mine", "other@x.com")]:
        server._rate.clear()
        client.post(f"/api/intake/{team}", json={"title": title, "email": em})
    tok = server._reporter_list_token("me@x.com")
    r = client.get(f"/my-tickets?email=me@x.com&t={tok}")
    assert r.status_code == 200
    assert "Mine A" in r.text and "Mine B" in r.text
    assert "Not mine" not in r.text                              # only the requester's tickets
    # Without a valid token: self-service landing (200), but it must NOT leak any tickets.
    for bad in (f"/my-tickets?email=me@x.com&t=bad", "/my-tickets?email=me@x.com", "/my-tickets"):
        rb = client.get(bad)
        assert rb.status_code == 200
        assert "Email me my tickets link" in rb.text             # the request-a-link landing
        assert "Mine A" not in rb.text and "Mine B" not in rb.text  # no ticket leak without a token


def test_intake_track_validates_email(client):
    server._rate.clear()
    assert client.post("/api/intake-track", json={"email": "not-an-email"}).status_code == 422
    server._rate.clear()
    assert client.post("/api/intake-track", json={"email": "ok@x.com"}).status_code == 200  # uniform 200


# ── 4.18.0: reporter reply thread on the status page (1B) ─────────────────────
def test_ticket_reply_token_gated_and_stored(client, team, admin_headers):
    _expose(client, admin_headers, types=["Bug"])
    server._rate.clear()
    pid = client.post(f"/api/intake/{team}", json={"title": "Reply me", "email": "rep@x.com"}).json()["id"]
    tok = server._ticket_token(team, pid)
    server._rate.clear()
    assert client.post("/api/ticket-reply",
                       json={"team": team, "id": pid, "t": "bad", "message": "hi"}).status_code == 404
    server._rate.clear()
    assert client.post("/api/ticket-reply",
                       json={"team": team, "id": pid, "t": tok, "message": "   "}).status_code == 422
    server._rate.clear()
    assert client.post("/api/ticket-reply",
                       json={"team": team, "id": pid, "t": tok, "message": "Chrome 120"}).status_code == 200
    with server.db(team) as c:
        row = c.execute("SELECT author, body, source FROM comments WHERE item_id=? ORDER BY id DESC LIMIT 1",
                        (pid,)).fetchone()
    assert row["source"] == "portal" and "Chrome 120" in row["body"] and "(reporter)" in row["author"]


def test_ticket_page_shows_reporter_thread_only(client, team, admin_headers):
    _expose(client, admin_headers, types=["Bug"])
    server._rate.clear()
    pid = client.post(f"/api/intake/{team}", json={"title": "Thread", "email": "rep@x.com"}).json()["id"]
    client.post("/api/comments", json={"item_id": pid, "body": "@reporter what version?"}, headers=admin_headers)
    client.post("/api/comments", json={"item_id": pid, "body": "internal only note"}, headers=admin_headers)
    server._rate.clear()
    client.post("/api/ticket-reply",
                json={"team": team, "id": pid, "t": server._ticket_token(team, pid), "message": "version 120"})
    r = client.get(f"/ticket?team={team}&id={pid}&t={server._ticket_token(team, pid)}")
    assert r.status_code == 200
    assert "what version?" in r.text          # team's @reporter note is shown
    assert "version 120" in r.text            # reporter reply is shown
    assert "internal only note" not in r.text  # internal comment stays hidden


# ── 4.19.0: intake config persistence + optional per-project notify email ─────
def test_intake_project_emails_settable_and_returned(client, team, admin_headers):
    # Regression for the "settings revert on refresh" bug: the key round-trips
    # through set_config → /api/all (boot reads it from there).
    assert _set(client, admin_headers, "intakeProjectEmails",
                {"Fraznet": "net@x.com"}).status_code == 200
    allr = client.get("/api/all", headers=admin_headers).json()
    assert allr["intakeProjectEmails"] == {"Fraznet": "net@x.com"}


def test_all_returns_intake_config_for_boot(client, team, admin_headers):
    # /api/all must expose every intake key so boot() can hydrate the settings UI
    # (the reverting-on-refresh symptom was boot never loading these).
    _set(client, admin_headers, "intakeEnabled", True)
    _set(client, admin_headers, "intakeNotifyEmail", "ops@x.com")
    _set(client, admin_headers, "intakeTypes", ["Bug"])
    allr = client.get("/api/all", headers=admin_headers).json()
    for k in ("intakeEnabled", "intakeProjects", "intakeTypes", "intakeNotifyEmail", "intakeProjectEmails"):
        assert k in allr, f"/api/all missing {k}"
    assert allr["intakeEnabled"] is True and allr["intakeNotifyEmail"] == "ops@x.com"


def test_notify_email_resolver_prefers_project_override(client, team, admin_headers):
    _set(client, admin_headers, "intakeNotifyEmail", "team@x.com")
    _set(client, admin_headers, "intakeProjectEmails", {"Fraznet": "net@x.com"})
    assert server._intake_notify_email(team, "Fraznet") == "net@x.com"   # override wins
    assert server._intake_notify_email(team, "HubSpot") == "team@x.com"  # falls back to team
    assert server._intake_notify_email(team, "") == "team@x.com"         # no product → team


def test_brand_mark_route_serves_png(client):
    r = client.get("/brand-mark.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"      # real PNG bytes


def test_email_header_uses_hosted_brand_mark(client, team, admin_headers, mailbox):
    # The email logo must be a hosted URL (data: URIs are blocked by Gmail/Outlook).
    html = server._intake_email_html({"name": "X", "description": ""}, [],
                                     "Head", "Intro", "CTA", "https://x/y")
    assert "/brand-mark.png" in html
    assert "data:image/png" not in html               # not an inline data URI


def test_submit_returns_ticket_url(client, team, admin_headers):
    # The created-page "View your ticket" link needs a token-bearing status URL.
    _expose(client, admin_headers, types=["Bug"])
    server._rate.clear()
    d = client.post(f"/api/intake/{team}", json={"title": "Linkme", "email": "a@b.com"}).json()
    assert "url" in d and f"/ticket?team={team}&id={d['id']}" in d["url"]
    assert f"t={server._ticket_token(team, d['id'])}" in d["url"]   # valid token embedded


def test_my_tickets_submit_link_keeps_email(client, team, admin_headers):
    # Submitting a new ticket from the My Tickets list should carry the reporter's
    # email so /report pre-fills it.
    _expose(client, admin_headers, types=["Bug"])
    server._rate.clear()
    client.post(f"/api/intake/{team}", json={"title": "T", "email": "me@x.com"})
    tok = server._reporter_list_token("me@x.com")
    r = client.get(f"/my-tickets?email=me@x.com&t={tok}")
    assert "/report?email=me%40x.com" in r.text          # list page link carries email
    # Landing (no token) also carries the email through when provided.
    assert "/report?email=me%40x.com" in client.get("/my-tickets?email=me@x.com").text


def test_creation_email_routes_to_project_override(client, team, admin_headers, mailbox):
    _expose(client, admin_headers, types=["Bug"], projects=["Fraznet", "HubSpot"])
    _set(client, admin_headers, "intakeNotifyEmail", "team@x.com")
    _set(client, admin_headers, "intakeProjectEmails", {"Fraznet": "net@x.com"})
    server._rate.clear()
    client.post(f"/api/intake/{team}",
                json={"title": "Routed", "email": "rep@x.com", "product": "Fraznet"})
    team_recips = [to for to, subj in mailbox if "new portal ticket" in subj.lower()]
    assert "net@x.com" in team_recips        # routed to the project's inbox
    assert "team@x.com" not in team_recips   # not the team default when an override exists
