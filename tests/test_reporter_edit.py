"""REPORTER-EDIT-1: set / change who an item is for.

Someone emails the team and staff create the item by hand: the reporter is that person. A reporter is a
Flow user (reporter = username) or someone outside Flow (name + email). An outside reporter makes the item a
ticket like a portal one (source "email"): /ticket reply, /my-tickets, the complete / deferred / @reporter
emails. Create takes `reporterPick` (confirmation email on by default); PUT /api/items/{pid}/reporter changes
it later (admin/editor), audits it, and points the linked Jira issue's reporter at the same person.
"""
import json
import pathlib

import server


def _cfg(team, key, val):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(val)))


def _users(team):
    _cfg(team, "users", [{"username": "admin", "role": "admin", "email": "admin@frazil.com"},
                         {"username": "amy", "role": "editor", "email": "amy@frazil.com", "firstName": "Amy", "lastName": "Ng"}])


def _stored(team, pid):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])


def _mail(monkeypatch):
    sent = []
    monkeypatch.setattr(server, "mail_configured", lambda: True)
    monkeypatch.setattr(server, "send_email", lambda to, subj, text, html=None: sent.append((to, subj)))
    return sent


def _create(client, headers, **fields):
    return client.post("/api/projects", json={"name": "Printer is down", "status": "Planned", **fields}, headers=headers)


# ── Create ───────────────────────────────────────────────────────────────────────────────

def test_create_for_someone_outside_flow_makes_a_ticket_and_confirms(client, team, admin_headers, monkeypatch):
    sent = _mail(monkeypatch)
    r = _create(client, admin_headers, reporterPick={"reporter": "Pat Lee", "email": "pat@example.com"})
    assert r.status_code == 200, r.text
    body = r.json()
    p = _stored(team, body["id"])
    assert (p["reporter"], p["reporterEmail"], p["source"]) == ("Pat Lee", "pat@example.com", "email")
    assert "reporterPick" not in p and "reporterEmailed" not in p
    assert body["reporterEmailed"] == "pat@example.com"
    assert len(sent) == 1 and sent[0][0] == "pat@example.com" and "Ticket received" not in sent[0][1]
    assert "We've received your ticket" in sent[0][1]
    with server.db(team) as c:
        ch = json.loads(c.execute("SELECT changes FROM audit_log WHERE project_id=? AND action='create'",
                                  (body["id"],)).fetchone()["changes"])
    assert ch["reporter"] == {"reporter": "Pat Lee", "email": "pat@example.com"}


def test_create_confirmation_can_be_turned_off(client, team, admin_headers, monkeypatch):
    sent = _mail(monkeypatch)
    r = _create(client, admin_headers, reporterPick={"reporter": "", "email": "pat@example.com", "notify": False})
    assert r.status_code == 200 and not sent
    assert _stored(team, r.json()["id"])["reporter"] == "pat@example.com"   # name falls back to the email


def test_create_for_a_flow_user_and_the_default(client, team, admin_headers, monkeypatch):
    _users(team)
    sent = _mail(monkeypatch)
    p = _stored(team, _create(client, admin_headers, reporterPick={"reporter": "amy"}).json()["id"])
    assert p["reporter"] == "amy" and "reporterEmail" not in p and "source" not in p
    assert _stored(team, _create(client, admin_headers).json()["id"])["reporter"] == "admin"
    assert not sent


def test_create_rejects_a_bad_pick(client, team, admin_headers):
    _users(team)
    assert _create(client, admin_headers, reporterPick={"reporter": "nobody"}).status_code == 422
    assert _create(client, admin_headers, reporterPick={"reporter": "Pat", "email": "not-an-email"}).status_code == 422
    _cfg(team, "intakeDomains", ["frazil.com"])
    assert _create(client, admin_headers, reporterPick={"reporter": "Pat", "email": "pat@gmail.com"}).status_code == 422
    assert _create(client, admin_headers, reporterPick={"reporter": "Pat", "email": "pat@frazil.com"}).status_code == 200


# ── Change ───────────────────────────────────────────────────────────────────────────────

def test_change_to_outside_then_back_to_a_user(client, team, admin_headers, monkeypatch):
    _users(team)
    sent = _mail(monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    r = client.put(f"/api/items/{pid}/reporter", json={"reporter": "Pat Lee", "email": "pat@example.com"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "email" and r.json()["emailed"] == "pat@example.com"
    assert [s[0] for s in sent] == ["pat@example.com"]
    # It is a ticket now: the reporter can reply from the status page.
    tok = server._ticket_token(team, pid)
    assert client.post("/api/ticket-reply", json={"team": team, "id": str(pid), "t": tok, "message": "Any news?"}).status_code == 200
    # Back to a Flow user: the email and the "email" source go.
    r = client.put(f"/api/items/{pid}/reporter", json={"reporter": "amy"}, headers=admin_headers)
    p = _stored(team, pid)
    assert p["reporter"] == "amy" and "reporterEmail" not in p and "source" not in p
    assert client.post("/api/ticket-reply", json={"team": team, "id": str(pid), "t": tok, "message": "x"}).status_code == 404
    with server.db(team) as c:
        rows = c.execute("SELECT changes FROM audit_log WHERE project_id=? AND action='reporter:change' ORDER BY id", (pid,)).fetchall()
        col = c.execute("SELECT reporter FROM projects WHERE id=?", (pid,)).fetchone()["reporter"]
    assert len(rows) == 2 and json.loads(rows[1]["changes"])["from"]["email"] == "pat@example.com"
    assert col == "amy"                                    # the indexed List column follows


def test_same_email_does_not_resend_and_unchanged_is_a_no_op(client, team, admin_headers, monkeypatch):
    sent = _mail(monkeypatch)
    pid = _create(client, admin_headers, reporterPick={"reporter": "Pat", "email": "pat@example.com"}).json()["id"]
    sent.clear()
    r = client.put(f"/api/items/{pid}/reporter", json={"reporter": "Pat Lee", "email": "PAT@example.com"}, headers=admin_headers)
    assert r.status_code == 200 and "emailed" not in r.json() and not sent    # a name fix, same person
    r = client.put(f"/api/items/{pid}/reporter", json={"reporter": "Pat Lee", "email": "PAT@example.com"}, headers=admin_headers)
    assert r.json().get("unchanged") is True


def test_portal_item_keeps_its_source(client, team, admin_headers):
    _users(team)
    pid = _create(client, admin_headers, reporter="Jo", reporterEmail="jo@example.com", source="portal").json()["id"]
    client.put(f"/api/items/{pid}/reporter", json={"reporter": "Joanne", "email": "joanne@example.com", "notify": False}, headers=admin_headers)
    assert _stored(team, pid)["source"] == "portal"
    client.put(f"/api/items/{pid}/reporter", json={"reporter": "amy"}, headers=admin_headers)
    p = _stored(team, pid)
    assert p["source"] == "portal" and "reporterEmail" not in p


def test_role_gate_and_generic_put_cannot_change_it(client, team, admin_headers, editor_headers, viewer_headers, contributor_headers):
    pid = _create(client, admin_headers, reporterPick={"reporter": "Pat", "email": "pat@example.com", "notify": False}).json()["id"]
    for h in (viewer_headers, contributor_headers):
        assert client.put(f"/api/items/{pid}/reporter", json={"reporter": "X", "email": "x@example.com"}, headers=h).status_code == 403
    assert client.put(f"/api/items/{pid}/reporter", json={"reporter": "Ed", "email": "ed@example.com", "notify": False},
                      headers=editor_headers).status_code == 200
    client.put(f"/api/projects/{pid}", json={"name": "Renamed", "reporter": "Mallory", "reporterEmail": "m@evil.com"}, headers=admin_headers)
    p = _stored(team, pid)
    assert (p["reporter"], p["reporterEmail"]) == ("Ed", "ed@example.com")


def test_emailed_in_ticket_gets_the_complete_email_and_my_tickets(client, team, admin_headers, monkeypatch):
    sent = _mail(monkeypatch)
    pid = _create(client, admin_headers, reporterPick={"reporter": "Pat", "email": "pat@example.com", "notify": False}).json()["id"]
    with server.db(team) as c:
        flags = json.loads(c.execute("SELECT value FROM config WHERE key='statusIsTerminal'").fetchone()["value"]) or {}
    term = next(s for s, v in flags.items() if v)
    client.put(f"/api/projects/{pid}", json={"status": term}, headers=admin_headers)
    assert any(to == "pat@example.com" and "complete" in subj for to, subj in sent)
    _cfg(team, "intakeEnabled", True)          # My Tickets lists Organizations with the portal on
    page = client.get(f"/my-tickets?email=pat@example.com&t={server._reporter_list_token('pat@example.com')}")
    assert page.status_code == 200 and "Printer is down" in page.text


# ── Jira ─────────────────────────────────────────────────────────────────────────────────

def test_change_updates_the_linked_jira_reporter(client, team, admin_headers, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_jira_account_id", lambda e: {"pat@example.com": "acc-pat"}.get(e, ""))
    monkeypatch.setattr(server, "_jira_req", lambda m, path, payload=None, timeout=10: calls.append((m, path, payload)) or {})
    pid = _create(client, admin_headers, jiraTickets=["FRAZ-9"]).json()["id"]
    r = client.put(f"/api/items/{pid}/reporter", json={"reporter": "Pat", "email": "pat@example.com", "notify": False}, headers=admin_headers)
    assert r.json()["jiraUpdated"] == "FRAZ-9"
    assert calls == [("PUT", "/rest/api/3/issue/FRAZ-9", {"fields": {"reporter": {"id": "acc-pat"}}})]
    r = client.put(f"/api/items/{pid}/reporter", json={"reporter": "Nobody", "email": "nobody@example.com", "notify": False}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["jiraWarning"] == "no Jira account for that person"
    assert _stored(team, pid)["reporterEmail"] == "nobody@example.com"   # Flow still saved


# ── Frontend wiring guards (behaviour verified headlessly during the build) ──────────────

_HTML = (pathlib.Path(__file__).resolve().parent.parent / "roadmap.html").read_text(encoding="utf-8")


def test_frontend_wiring():
    assert 'id="fReporterRow"' in _HTML and "_reporterCreateMount(id);" in _HTML
    assert "...(_repPick ? { reporterPick: _repPick } : {})" in _HTML
    assert "API.put('/api/items/'+id+'/reporter'" in _HTML
    assert _HTML.count("openReporterDialog('+p.id+')") == 2        # item page + modal Item Info
    assert "(p.source==='portal' || p.source==='email')" in _HTML    # @reporter typeahead
