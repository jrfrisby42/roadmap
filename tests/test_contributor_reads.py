"""Contributor Phase B (5.1.0): server-side READ scoping.

A Contributor may READ an item only if it is in their write scope (assignee == them OR
item.dev == their ownerFilter) OR an explicit grant exists (insider @mention). These tests
attack the read paths API-direct with a contributor token and assert nothing out of scope
leaks - through the bulk payload, the paginated list, per-item fetches, feeds, or grants.
Admin/editor/viewer reads must be unchanged.
"""
import json

import server


def _mk(client, admin_headers, **fields):
    body = {"name": "Item", "status": "Planned", **fields}
    r = client.post("/api/projects", json=body, headers=admin_headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _seed_contrib_user(client, admin_headers, owner_filter=""):
    """Put contrib1 in the users config (needed for pod scope and for @mention parsing)."""
    client.put("/api/config/users",
               json=[{"username": "contrib1", "role": "contributor", "ownerFilter": owner_filter}],
               headers=admin_headers)


# ── B1.1 bulk payload (/api/all) ────────────────────────────────────────────────
def test_all_returns_only_in_scope_items(client, team, admin_headers, contributor_headers):
    mine   = _mk(client, admin_headers, assignee="contrib1")
    theirs = _mk(client, admin_headers, assignee="someone_else", dev="OtherPod")
    got = client.get("/api/all", headers=contributor_headers).json()
    ids = {p["id"] for p in got["projects"]}
    assert mine in ids and theirs not in ids
    # Config/lists stay unfiltered so the client can render.
    assert "statuses" in got and "users" in got


def test_all_empty_ownerfilter_no_assignments_sees_zero(client, team, admin_headers, contributor_headers):
    # CRITICAL: empty ownerFilter must mean assignee-only, never "everything".
    _mk(client, admin_headers, assignee="someone_else", dev="OtherPod")
    _mk(client, admin_headers, dev="")   # blank-owner item must NOT match an empty pod
    got = client.get("/api/all", headers=contributor_headers).json()
    assert got["projects"] == []


def test_all_pod_scope(client, team, admin_headers, contributor_headers):
    _seed_contrib_user(client, admin_headers, owner_filter="Weto")
    mine   = _mk(client, admin_headers, dev="Weto", assignee="")
    theirs = _mk(client, admin_headers, dev="Everest", assignee="")
    ids = {p["id"] for p in client.get("/api/all", headers=contributor_headers).json()["projects"]}
    assert mine in ids and theirs not in ids


# ── B1.1 paginated list (/api/items) - scoped rows AND scoped total ──────────────
def test_items_list_scoped_rows_and_total(client, team, admin_headers, contributor_headers):
    a = _mk(client, admin_headers, assignee="contrib1")
    b = _mk(client, admin_headers, assignee="contrib1")
    _mk(client, admin_headers, assignee="other", dev="OtherPod")
    _mk(client, admin_headers, assignee="other2", dev="OtherPod")
    res = client.get("/api/items", headers=contributor_headers).json()
    assert res["total"] == 2
    assert {it["id"] for it in res["items"]} == {a, b}


# ── B1.2 per-item read gates (404, not 403) ─────────────────────────────────────
def test_out_of_scope_item_reads_404(client, team, admin_headers, contributor_headers):
    theirs = _mk(client, admin_headers, assignee="other", dev="OtherPod")
    assert client.get(f"/api/comments/{theirs}", headers=contributor_headers).status_code == 404
    assert client.get(f"/api/items/{theirs}/attachments", headers=contributor_headers).status_code == 404
    assert client.get(f"/api/items/{theirs}/watchers", headers=contributor_headers).status_code == 404


def test_in_scope_item_reads_ok(client, team, admin_headers, contributor_headers):
    mine = _mk(client, admin_headers, assignee="contrib1")
    assert client.get(f"/api/comments/{mine}", headers=contributor_headers).status_code == 200
    assert client.get(f"/api/items/{mine}/attachments", headers=contributor_headers).status_code == 200


# ── B1.3 activities ──────────────────────────────────────────────────────────────
def test_activities_hide_out_of_scope(client, team, admin_headers, editor_headers, contributor_headers):
    mine   = _mk(client, admin_headers, assignee="contrib1")
    theirs = _mk(client, admin_headers, assignee="other", dev="OtherPod")
    for pid, name in ((mine, "MINE-ACT"), (theirs, "THEIRS-ACT")):
        client.post("/api/activities",
                    json={"activity_type": "Flagged Issue", "item_id": pid, "item_name": name,
                          "source": "User"}, headers=editor_headers)
    acts = client.get("/api/activities", headers=contributor_headers).json()
    names = {a.get("item_name") for a in acts}
    assert "THEIRS-ACT" not in names
    ids = {a.get("item_id") for a in acts}
    assert theirs not in ids


# ── B1.4 grants: a mention makes an out-of-scope item readable; no self-grant ─────
def test_grant_via_direct_helper_makes_readable(client, team, admin_headers, contributor_headers):
    theirs = _mk(client, admin_headers, assignee="other", dev="OtherPod")
    assert client.get(f"/api/comments/{theirs}", headers=contributor_headers).status_code == 404
    server._grant_item_access(team, theirs, ["contrib1"], "mention", "editor1")
    # Now readable: per-item gate opens and the bulk payload includes it.
    assert client.get(f"/api/comments/{theirs}", headers=contributor_headers).status_code == 200
    ids = {p["id"] for p in client.get("/api/all", headers=contributor_headers).json()["projects"]}
    assert theirs in ids


def test_grant_via_editor_comment_mention(client, team, admin_headers, editor_headers, contributor_headers):
    _seed_contrib_user(client, admin_headers)   # contrib1 must be a known username to be mentioned
    theirs = _mk(client, admin_headers, assignee="other", dev="OtherPod")
    r = client.post("/api/comments",
                    json={"item_id": theirs, "body": "@contrib1 please take a look"},
                    headers=editor_headers)
    assert r.status_code == 200
    # The mention granted read access; the notification link resolves.
    assert client.get(f"/api/comments/{theirs}", headers=contributor_headers).status_code == 200


def test_watching_is_not_a_self_grant(client, team, admin_headers, contributor_headers):
    theirs = _mk(client, admin_headers, assignee="other", dev="OtherPod")
    # A pre-existing watcher row (as could exist from the Phase A window) grants nothing.
    server._add_watchers(team, theirs, ["contrib1"])
    ids = {p["id"] for p in client.get("/api/all", headers=contributor_headers).json()["projects"]}
    assert theirs not in ids
    assert client.get(f"/api/comments/{theirs}", headers=contributor_headers).status_code == 404
    # And it does not surface in their watch list.
    assert theirs not in client.get("/api/my/watching", headers=contributor_headers).json()["items"]


# ── B1.5 watch gating ────────────────────────────────────────────────────────────
def test_cannot_watch_out_of_scope(client, team, admin_headers, contributor_headers):
    theirs = _mk(client, admin_headers, assignee="other", dev="OtherPod")
    assert client.post(f"/api/items/{theirs}/watch", headers=contributor_headers).status_code == 404
    mine = _mk(client, admin_headers, assignee="contrib1")
    assert client.post(f"/api/items/{mine}/watch", headers=contributor_headers).status_code == 200


# ── B1.6 notifications ───────────────────────────────────────────────────────────
def test_notifications_hide_out_of_scope(client, team, admin_headers, contributor_headers):
    theirs = _mk(client, admin_headers, assignee="other", dev="OtherPod")
    server._notify(team, ["contrib1"], "mention", theirs, "SECRET-TITLE", "x mentioned you", "editor1")
    notes = client.get("/api/notifications", headers=contributor_headers).json()["notifications"]
    assert all(n.get("item_id") != theirs for n in notes)
    assert all(n.get("item_name") != "SECRET-TITLE" for n in notes)


# ── B1 acceptance #7: admin/editor/viewer reads unchanged ────────────────────────
def test_admin_editor_viewer_reads_unchanged(client, team, admin_headers, editor_headers, viewer_headers):
    _mk(client, admin_headers, assignee="contrib1")
    _mk(client, admin_headers, assignee="other", dev="OtherPod")
    _mk(client, admin_headers, dev="")
    for hdr in (admin_headers, editor_headers, viewer_headers):
        got = client.get("/api/all", headers=hdr).json()
        assert len(got["projects"]) == 3
        assert client.get("/api/items", headers=hdr).json()["total"] == 3


# ── B1 acceptance #8: Phase A writes still enforced (sanity) ──────────────────────
def test_phase_a_write_scope_still_enforced(client, team, admin_headers, contributor_headers):
    theirs = _mk(client, admin_headers, assignee="other", dev="OtherPod")
    assert client.put(f"/api/projects/{theirs}", json={"description": "x"},
                      headers=contributor_headers).status_code == 403
