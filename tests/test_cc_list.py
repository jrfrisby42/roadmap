"""CC-1: external notify / CC list (reporter-equivalent access for a non-account address).

`ccList` is server-owned + endpoint-mediated (like extLinks): admin/editor only, domain-gated by
intakeDomains, capped, in SERVER_OWNED_FIELDS + RECURRENCE_INHERITED. Recipients ride the EXISTING
reporter status/comment emails (no new mailer), each with a per-item unsubscribe link. Unsubscribe
is stateless and affects that item only.
"""
import base64
import json

import server


def _create(client, headers, **fields):
    return client.post("/api/projects", json={"name": "Item", "status": "Planned", **fields}, headers=headers)


def _stored(team, pid):
    with server.db(team) as c:
        row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
    return json.loads(row["data"]) if row else None


def _patch(team, pid, **kv):
    with server.db(team) as c:
        p = json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])
        p.update(kv)
        server._save_project(c, pid, p)


def _set_cfg(team, key, val):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(val)))


# ── Add / validate / gate ─────────────────────────────────────────────────────────

def test_add_cc_happy(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    r = client.post(f"/api/items/{pid}/cc", json={"email": "vendor@example.com"}, headers=admin_headers)
    assert r.status_code == 200
    cc = r.json()["ccList"]
    assert len(cc) == 1 and cc[0]["email"] == "vendor@example.com" and cc[0]["id"] and cc[0]["addedBy"] == "admin"
    assert _stored(team, pid)["ccList"][0]["email"] == "vendor@example.com"


def test_add_cc_rejects_bad_email(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    for bad in ("notanemail", "a@b", "@x.com", "a b@x.com", ""):
        assert client.post(f"/api/items/{pid}/cc", json={"email": bad}, headers=admin_headers).status_code == 422, bad


def test_add_cc_domain_allowlist(client, team, admin_headers):
    _set_cfg(team, "intakeDomains", ["example.com"])
    pid = _create(client, admin_headers).json()["id"]
    assert client.post(f"/api/items/{pid}/cc", json={"email": "ok@example.com"}, headers=admin_headers).status_code == 200
    assert client.post(f"/api/items/{pid}/cc", json={"email": "no@other.com"}, headers=admin_headers).status_code == 422


def test_add_cc_duplicate_and_cap(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    client.post(f"/api/items/{pid}/cc", json={"email": "a@example.com"}, headers=admin_headers)
    assert client.post(f"/api/items/{pid}/cc", json={"email": "A@EXAMPLE.COM"}, headers=admin_headers).status_code == 409
    for i in range(1, server._CC_LIST_CAP):
        client.post(f"/api/items/{pid}/cc", json={"email": f"u{i}@example.com"}, headers=admin_headers)
    assert client.post(f"/api/items/{pid}/cc", json={"email": "over@example.com"}, headers=admin_headers).status_code == 422


def test_editor_can_add_contributor_and_viewer_cannot(client, team, admin_headers, editor_headers,
                                                      contributor_headers, viewer_headers):
    pid = _create(client, admin_headers).json()["id"]
    assert client.post(f"/api/items/{pid}/cc", json={"email": "e@example.com"}, headers=editor_headers).status_code == 200
    r = client.post(f"/api/items/{pid}/cc", json={"email": "c@example.com"}, headers=contributor_headers)
    assert r.status_code == 403      # phishing guard: contributors cannot direct item content outward
    assert client.post(f"/api/items/{pid}/cc", json={"email": "v@example.com"}, headers=viewer_headers).status_code == 403


def test_remove_cc(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    cid = client.post(f"/api/items/{pid}/cc", json={"email": "a@example.com"}, headers=admin_headers).json()["ccList"][0]["id"]
    r = client.delete(f"/api/items/{pid}/cc/{cid}", headers=admin_headers)
    assert r.status_code == 200 and r.json()["ccList"] == []


# ── Server-owned: the generic PUT cannot forge or wipe it ───────────────────────────

def test_generic_put_cannot_forge_cclist(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    r = client.put(f"/api/projects/{pid}", json={"name": "Item", "status": "Planned",
                    "ccList": [{"id": "x", "email": "evil@example.com"}]}, headers=admin_headers)
    assert r.status_code == 200
    assert not (_stored(team, pid).get("ccList") or [])


def test_generic_put_cannot_wipe_cclist(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    client.post(f"/api/items/{pid}/cc", json={"email": "keep@example.com"}, headers=admin_headers)
    r = client.put(f"/api/projects/{pid}", json={"name": "Item", "status": "Planned", "ccList": []}, headers=admin_headers)
    assert r.status_code == 200
    assert len(_stored(team, pid)["ccList"]) == 1


# ── Recurrence inheritance ───────────────────────────────────────────────────────────

def test_cclist_server_owned_and_inherited():
    assert "ccList" in server.SERVER_OWNED_FIELDS
    assert "ccList" in server.RECURRENCE_INHERITED


def test_spawn_inherits_cclist(client, team, admin_headers):
    pid = _create(client, admin_headers, recurrence="weekly", start="2026-01-01", dueWeeks=2).json()["id"]
    _patch(team, pid, ccList=[{"id": "c1", "email": "vendor@example.com", "addedBy": "admin", "addedAt": "2026-01-01T00:00:00+00:00"}])
    child = client.post(f"/api/projects/{pid}/recur", json={}, headers=admin_headers).json()
    assert _stored(team, child["id"])["ccList"][0]["email"] == "vendor@example.com"


# ── Unsubscribe (stateless, this-item-only) ────────────────────────────────────────

def test_unsubscribe_removes_only_that_item(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    other = _create(client, admin_headers).json()["id"]
    client.post(f"/api/items/{pid}/cc", json={"email": "v@example.com"}, headers=admin_headers)
    client.post(f"/api/items/{other}/cc", json={"email": "v@example.com"}, headers=admin_headers)
    tok = server._cc_unsub_token(team, pid, "v@example.com")
    r = client.get(f"/cc-unsubscribe?team={team}&id={pid}&email=v@example.com&t={tok}")
    assert r.status_code == 200
    assert not (_stored(team, pid).get("ccList") or [])          # removed from this item
    assert len(_stored(team, other)["ccList"]) == 1              # untouched on the other


def test_unsubscribe_bad_token_400(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    client.post(f"/api/items/{pid}/cc", json={"email": "v@example.com"}, headers=admin_headers)
    r = client.get(f"/cc-unsubscribe?team={team}&id={pid}&email=v@example.com&t=deadbeef")
    assert r.status_code == 400
    assert len(_stored(team, pid)["ccList"]) == 1               # not removed


# ── Duplicate opt-in (2.5) ──────────────────────────────────────────────────────────

def test_duplicate_optin_adds_loser_reporter_to_survivor(client, team, admin_headers):
    surv = _create(client, admin_headers).json()["id"]
    loser = _create(client, admin_headers).json()["id"]
    _patch(team, loser, reporterEmail="reporter@example.com", source="portal")
    r = client.post(f"/api/items/{loser}/mark-duplicate",
                    json={"survivorId": surv, "notifyReporter": True}, headers=admin_headers)
    assert r.status_code == 200
    cc = _stored(team, surv).get("ccList") or []
    assert any(x["email"] == "reporter@example.com" for x in cc)


def test_duplicate_without_optin_adds_nobody(client, team, admin_headers):
    surv = _create(client, admin_headers).json()["id"]
    loser = _create(client, admin_headers).json()["id"]
    _patch(team, loser, reporterEmail="reporter@example.com", source="portal")
    r = client.post(f"/api/items/{loser}/mark-duplicate", json={"survivorId": surv}, headers=admin_headers)
    assert r.status_code == 200
    assert not (_stored(team, surv).get("ccList") or [])


# ── Email integration: CC rides the existing status mailer ──────────────────────────

def test_cc_receives_status_email(client, team, admin_headers, monkeypatch):
    sent = []
    monkeypatch.setattr(server, "mail_configured", lambda: True)
    monkeypatch.setattr(server, "send_email", lambda to, subj, text, html=None: sent.append((to, subj, text)))
    _set_cfg(team, "statuses", ["Planned", "Done"])
    _set_cfg(team, "statusIsTerminal", {"Done": True})
    pid = _create(client, admin_headers).json()["id"]
    client.post(f"/api/items/{pid}/cc", json={"email": "vendor@example.com"}, headers=admin_headers)
    r = client.put(f"/api/projects/{pid}", json={"name": "Item", "status": "Done"}, headers=admin_headers)
    assert r.status_code == 200
    cc_msgs = [m for m in sent if m[0] == "vendor@example.com"]
    assert cc_msgs, "CC address should receive the completed-status email"
    assert "cc-unsubscribe" in cc_msgs[0][2]                    # carries an unsubscribe link
    assert "my-tickets" not in cc_msgs[0][2]                    # but NOT the aggregate-list link
