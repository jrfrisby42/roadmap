"""AssetHub integration - PR1 (contract-independent plumbing).

`externalRefs` is a server-owned link list on the item blob: a client can neither
forge it (on create or update) nor wipe it (by omitting it on a full-blob PUT). Only a
server-side hook may set it, via the single mutation path ``server._append_external_ref``.
The field is INERT in this PR - nothing in the app populates it - so these tests set it
server-side (exactly the way a later technician action will) and pin the guards around it.

Also covers the new ``externalRequestCategories`` config key: default empty, readable
through ``/api/all``, and never re-seeded once an admin has set a value (presence-only).
"""
import json

import server


def _create(client, headers, **fields):
    body = {"name": "Item", "status": "Planned", **fields}
    return client.post("/api/projects", json=body, headers=headers)


def _stored(team, pid):
    with server.db(team) as c:
        row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
    return json.loads(row["data"]) if row else None


def _set_ref_server_side(team, pid, entry):
    """Set externalRefs the way the future technician action will: read the blob, append
    through the single mutation path, and _save_project directly (NOT through a PUT)."""
    with server.db(team) as c:
        p = json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])
        server._append_external_ref(p, entry)
        server._save_project(c, pid, p)


_REF = {"system": "assethub", "kind": "request", "id": "42",
        "number": "REQ-42", "url": "https://assethub.example/requests/42",
        "status": "linked", "at": "2026-07-28T12:00:00+00:00"}


# ── Write-path guards ─────────────────────────────────────────────────────────

def test_client_put_cannot_forge_external_refs(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    forged = [{"system": "assethub", "kind": "request", "number": "REQ-999",
               "url": "https://evil.example", "status": "linked", "at": "x"}]
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "Planned", "externalRefs": forged},
                   headers=admin_headers)
    assert r.status_code == 200
    assert not r.json().get("externalRefs")           # response is clean
    assert "externalRefs" not in _stored(team, pid)   # nothing persisted


def test_client_put_cannot_wipe_external_refs(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    _set_ref_server_side(team, pid, dict(_REF))
    # A full-blob PUT that omits externalRefs (or sends a different one) must not wipe it.
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Renamed", "status": "Planned", "externalRefs": []},
                   headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["externalRefs"] == [_REF]
    assert _stored(team, pid)["externalRefs"] == [_REF]


def test_client_post_create_strips_external_refs(client, team, admin_headers):
    r = _create(client, admin_headers,
                externalRefs=[{"system": "assethub", "kind": "request", "status": "linked", "at": "x"}])
    assert r.status_code == 200
    assert not r.json().get("externalRefs")            # stripped from the returned body
    pid = r.json()["id"]
    assert "externalRefs" not in _stored(team, pid)    # and never inserted


def test_server_set_external_refs_survive_two_cycles(client, team, admin_headers):
    """Two full save-and-reload cycles - one is not enough to sign off persistence here."""
    pid = _create(client, admin_headers).json()["id"]
    _set_ref_server_side(team, pid, dict(_REF))
    for i in range(2):
        r = client.put(f"/api/projects/{pid}",
                       json={"name": f"Edit {i}", "status": "Planned"}, headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["externalRefs"] == [_REF]
    # Fresh fetch through the real read path, not the PUT echo.
    allr = client.get("/api/all", headers=admin_headers).json()
    item = next(p for p in allr["projects"] if p["id"] == pid)
    assert item["externalRefs"] == [_REF]


def test_recurrence_spawn_does_not_inherit_external_refs(client, team, admin_headers):
    """The spawn is a blocklist COPY of the parent blob, so externalRefs must be in
    skip_keys - a new occurrence is a new ticket and must not carry the parent's link."""
    pid = _create(client, admin_headers,
                  recurrence="weekly", start="2026-01-01", dueWeeks=2).json()["id"]
    _set_ref_server_side(team, pid, dict(_REF))
    r = client.post(f"/api/projects/{pid}/recur", json={}, headers=admin_headers)
    assert r.status_code == 200
    child = r.json()
    assert not child.get("externalRefs")
    assert "externalRefs" not in _stored(team, child["id"])
    # Parent keeps its own link.
    assert _stored(team, pid)["externalRefs"] == [_REF]


def test_append_helper_is_the_single_mutation_path(team):
    """_append_external_ref creates the list when absent and appends when present."""
    item = {}
    server._append_external_ref(item, {"system": "assethub", "status": "queued", "at": "x"})
    assert item["externalRefs"] == [{"system": "assethub", "status": "queued", "at": "x"}]
    server._append_external_ref(item, {"system": "assethub", "status": "linked", "at": "y"})
    assert len(item["externalRefs"]) == 2


# ── Config key: externalRequestCategories ───────────────────────────────────────

def test_external_request_categories_default_empty_and_in_all(client, team, admin_headers):
    allr = client.get("/api/all", headers=admin_headers).json()
    assert allr["externalRequestCategories"] == {}


def test_external_request_categories_not_reseeded_after_set(client, team, admin_headers):
    val = {"assethub": [{"id": "COMP", "label": "Computers"}]}
    r = client.put("/api/config/externalRequestCategories", json=val, headers=admin_headers)
    assert r.status_code == 200
    # Re-run team init (simulates a boot/migration pass): presence-only means the admin's
    # value is never overwritten back to {}.
    server.init_team_db(team)
    allr = client.get("/api/all", headers=admin_headers).json()
    assert allr["externalRequestCategories"] == val
