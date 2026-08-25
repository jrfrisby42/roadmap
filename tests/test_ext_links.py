"""LINKS-1: external reference links on an item (the "& Links" half of Attachments & Links).

`extLinks` is a user-supplied blob array written ONLY through the dedicated endpoints
(POST/PATCH/DELETE /api/items/{pid}/links). It is in SERVER_OWNED_FIELDS, so the generic item
PUT can neither forge nor wipe it - which makes the save-time http/https scheme whitelist the
single, unbypassable gate. The endpoints are admin/editor only, so a contributor (an external
contractor) can read links but never add one (a phishing path). Recurrence CARRIES links to the
next occurrence (RECURRENCE_INHERITED).
"""
import json

import server


def _create(client, headers, **fields):
    return client.post("/api/projects", json={"name": "Item", "status": "Planned", **fields}, headers=headers)


def _stored(team, pid):
    with server.db(team) as c:
        row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
    return json.loads(row["data"]) if row else None


def _patch(team, pid, **kv):
    """Set fields on a stored blob directly (stand in for server-set state like extLinks)."""
    with server.db(team) as c:
        p = json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])
        p.update(kv)
        server._save_project(c, pid, p)


# ── Add ──────────────────────────────────────────────────────────────────────────

def test_add_link_happy(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    r = client.post(f"/api/items/{pid}/links",
                    json={"label": "Runbook", "url": "https://example.com/run"}, headers=admin_headers)
    assert r.status_code == 200
    links = r.json()["extLinks"]
    assert len(links) == 1
    row = links[0]
    assert row["label"] == "Runbook"
    assert row["url"] == "https://example.com/run"
    assert row["id"] and row["addedBy"] == "admin" and row["addedAt"]
    # Persisted to the blob.
    assert _stored(team, pid)["extLinks"][0]["url"] == "https://example.com/run"


def test_add_link_rejects_non_http_scheme(client, team, admin_headers):
    """The save-time guard: a javascript: URL is refused with 422 and nothing is stored."""
    pid = _create(client, admin_headers).json()["id"]
    r = client.post(f"/api/items/{pid}/links",
                    json={"label": "x", "url": "javascript:alert(1)"}, headers=admin_headers)
    assert r.status_code == 422
    assert not (_stored(team, pid).get("extLinks") or [])


def test_add_link_rejects_scheme_relative_and_bare(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    for bad in ("ftp://example.com/x", "//example.com/x", "example.com", "mailto:a@b.com", ""):
        r = client.post(f"/api/items/{pid}/links", json={"label": "x", "url": bad}, headers=admin_headers)
        assert r.status_code == 422, bad


def test_blank_label_falls_back_to_hostname(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    r = client.post(f"/api/items/{pid}/links",
                    json={"label": "", "url": "https://portal.sharepoint.com/a/b/c?d=1"}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["extLinks"][0]["label"] == "portal.sharepoint.com"   # hostname, not the full URL


def test_link_cap_enforced(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    for i in range(server._EXT_LINK_CAP):
        assert client.post(f"/api/items/{pid}/links",
                           json={"label": f"L{i}", "url": f"https://e.com/{i}"}, headers=admin_headers).status_code == 200
    over = client.post(f"/api/items/{pid}/links",
                       json={"label": "one too many", "url": "https://e.com/x"}, headers=admin_headers)
    assert over.status_code == 422


# ── Edit ─────────────────────────────────────────────────────────────────────────

def test_edit_link_changes_label_and_url(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    lid = client.post(f"/api/items/{pid}/links",
                      json={"label": "old", "url": "https://old.com"}, headers=admin_headers).json()["extLinks"][0]["id"]
    r = client.patch(f"/api/items/{pid}/links/{lid}",
                     json={"label": "new", "url": "https://new.com"}, headers=admin_headers)
    assert r.status_code == 200
    row = r.json()["extLinks"][0]
    assert row["id"] == lid and row["label"] == "new" and row["url"] == "https://new.com"


def test_edit_link_rejects_bad_scheme(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    lid = client.post(f"/api/items/{pid}/links",
                      json={"label": "ok", "url": "https://ok.com"}, headers=admin_headers).json()["extLinks"][0]["id"]
    r = client.patch(f"/api/items/{pid}/links/{lid}",
                     json={"label": "ok", "url": "javascript:alert(1)"}, headers=admin_headers)
    assert r.status_code == 422
    assert _stored(team, pid)["extLinks"][0]["url"] == "https://ok.com"   # unchanged


def test_edit_missing_link_404(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    r = client.patch(f"/api/items/{pid}/links/deadbeef",
                     json={"label": "x", "url": "https://x.com"}, headers=admin_headers)
    assert r.status_code == 404


# ── Delete ───────────────────────────────────────────────────────────────────────

def test_delete_link(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    lid = client.post(f"/api/items/{pid}/links",
                      json={"label": "x", "url": "https://x.com"}, headers=admin_headers).json()["extLinks"][0]["id"]
    r = client.delete(f"/api/items/{pid}/links/{lid}", headers=admin_headers)
    assert r.status_code == 200 and r.json()["extLinks"] == []
    assert not (_stored(team, pid).get("extLinks") or [])


def test_delete_missing_link_404(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    assert client.delete(f"/api/items/{pid}/links/nope", headers=admin_headers).status_code == 404


# ── Role gating (the contributor guard) ────────────────────────────────────────────

def test_editor_can_add_link(client, team, editor_headers):
    pid = _create(client, editor_headers).json()["id"]
    assert client.post(f"/api/items/{pid}/links",
                       json={"label": "x", "url": "https://x.com"}, headers=editor_headers).status_code == 200


def test_contributor_cannot_add_link(client, team, admin_headers, contributor_headers):
    """The phishing guard, server-side: a contributor is refused by the role gate. This is the
    falsifiable test - if the endpoint's require_role were widened to include 'contributor',
    this would fail (verified by temporarily widening it during the build)."""
    pid = _create(client, admin_headers).json()["id"]
    r = client.post(f"/api/items/{pid}/links",
                    json={"label": "x", "url": "https://x.com"}, headers=contributor_headers)
    assert r.status_code == 403
    assert not (_stored(team, pid).get("extLinks") or [])


def test_viewer_cannot_add_link(client, team, admin_headers, viewer_headers):
    pid = _create(client, admin_headers).json()["id"]
    assert client.post(f"/api/items/{pid}/links",
                       json={"label": "x", "url": "https://x.com"}, headers=viewer_headers).status_code == 403


# ── The generic item PUT cannot forge or wipe extLinks (why one write path holds) ──

def test_generic_put_cannot_forge_extlinks(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "Planned",
                         "extLinks": [{"id": "forged", "label": "evil", "url": "javascript:alert(1)"}]},
                   headers=admin_headers)
    assert r.status_code == 200
    assert not (_stored(team, pid).get("extLinks") or [])   # forged value dropped, never stored


def test_generic_put_cannot_wipe_extlinks(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    client.post(f"/api/items/{pid}/links", json={"label": "keep", "url": "https://keep.com"}, headers=admin_headers)
    # A wholesale PUT that omits or clears extLinks must not delete the endpoint-written link.
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "Planned", "extLinks": []}, headers=admin_headers)
    assert r.status_code == 200
    stored = _stored(team, pid)["extLinks"]
    assert len(stored) == 1 and stored[0]["url"] == "https://keep.com"


# ── Recurrence carries links (RECURRENCE_INHERITED) ────────────────────────────────

def test_spawn_inherits_extlinks(client, team, admin_headers):
    pid = _create(client, admin_headers, recurrence="weekly", start="2026-01-01", dueWeeks=2).json()["id"]
    _patch(team, pid, extLinks=[{"id": "l1", "label": "Runbook", "url": "https://run.com",
                                 "addedBy": "admin", "addedAt": "2026-01-01T00:00:00+00:00"}])
    child = client.post(f"/api/projects/{pid}/recur", json={}, headers=admin_headers).json()
    stored = _stored(team, child["id"])
    assert stored.get("extLinks") and stored["extLinks"][0]["url"] == "https://run.com"


def test_extlinks_is_server_owned_and_inherited():
    """Membership invariant (mirrors test_recurrence_inheritance for this field)."""
    assert "extLinks" in server.SERVER_OWNED_FIELDS
    assert "extLinks" in server.RECURRENCE_INHERITED
    assert "extLinks" not in server.CONTRIBUTOR_EDITABLE_FIELDS
