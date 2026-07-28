"""Recurrence spawn inheritance (5.4.4).

`spawn_recurrence` builds its child as a BLOCKLIST COPY of the parent blob, so its default
is to INHERIT any field not in the skip set. Ship C's measurement fields (createdAt,
completedAt, firstResponseAt) were absent from that set, so every occurrence inherited the
parent's timestamps - completedAt being the serious one, since `_stamp_measurement_ts` only
ever sets and never clears, making the child's real completion time unrecordable.

The invariant test is the part that prevents this class of bug from recurring: the
force-restore allowlist (SERVER_OWNED_FIELDS) and the recurrence blocklist
(RECURRENCE_SKIP_KEYS) have opposite defaults and used to live far apart, so a new
server-owned field was safe from client forgery the moment it joined the allowlist yet
silently inherited by every recurrence child until someone remembered the blocklist. The
invariant forces that decision.
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


def _patch(team, pid, **kv):
    """Set fields on a stored blob directly (stand in for server-side state)."""
    with server.db(team) as c:
        p = json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])
        p.update(kv)
        server._save_project(c, pid, p)


# ── The invariant (Part 4) ──────────────────────────────────────────────────────

def test_every_server_owned_field_is_dropped_or_explicitly_inherited():
    """Every SERVER_OWNED_FIELDS member must be stripped on recurrence spawn
    (RECURRENCE_SKIP_KEYS) or on the explicit RECURRENCE_INHERITED exception list."""
    unhandled = [f for f in server.SERVER_OWNED_FIELDS
                 if f not in server.RECURRENCE_SKIP_KEYS
                 and f not in server.RECURRENCE_INHERITED]
    assert not unhandled, (
        "Server-owned field(s) neither stripped on recurrence spawn nor on the "
        "RECURRENCE_INHERITED exception list: " + ", ".join(unhandled) +
        ". Add each to RECURRENCE_SKIP_KEYS (drop it - the usual choice for a new "
        "occurrence) or to RECURRENCE_INHERITED with a justifying comment.")


def test_invariant_catches_an_unhandled_server_owned_field():
    """Demonstrates the invariant fails when a server-owned field is added to the
    force-restore allowlist but forgotten in both the skip set and the exception list."""
    fields = tuple(server.SERVER_OWNED_FIELDS) + ("aNewServerOwnedField",)
    unhandled = [f for f in fields
                 if f not in server.RECURRENCE_SKIP_KEYS
                 and f not in server.RECURRENCE_INHERITED]
    assert unhandled == ["aNewServerOwnedField"]


def test_inherited_set_holds_only_server_owned_fields():
    """RECURRENCE_INHERITED is an exception list for server-owned fields; it must not
    accumulate unrelated business fields."""
    assert set(server.RECURRENCE_INHERITED).issubset(set(server.SERVER_OWNED_FIELDS))


# ── Behavior: the three measurement fields are not inherited (Part 7) ─────────────

def test_spawn_does_not_inherit_measurement_timestamps(client, team, admin_headers):
    pid = _create(client, admin_headers, recurrence="weekly", start="2026-01-01", dueWeeks=2).json()["id"]
    _patch(team, pid,
           createdAt="2020-01-01T00:00:00+00:00",
           completedAt="2020-02-01T00:00:00+00:00",
           firstResponseAt="2020-01-05T00:00:00+00:00")
    child = client.post(f"/api/projects/{pid}/recur", json={}, headers=admin_headers).json()
    stored = _stored(team, child["id"])
    assert "completedAt" not in stored          # the serious one - not carried over
    assert "firstResponseAt" not in stored
    # createdAt is present but FRESH (its own), not the parent's inherited value.
    assert stored.get("createdAt")
    assert stored["createdAt"] != "2020-01-01T00:00:00+00:00"


def test_spawned_child_records_its_own_completion(client, team, admin_headers):
    """The serious case, proven fixed: a child spawned from a COMPLETED parent begins with
    completedAt absent, and records its OWN completion when it later reaches terminal."""
    pid = _create(client, admin_headers, recurrence="weekly", start="2026-01-01", dueWeeks=2).json()["id"]
    _patch(team, pid, status="Released", completedAt="2020-02-01T00:00:00+00:00")
    child = client.post(f"/api/projects/{pid}/recur", json={}, headers=admin_headers).json()
    cid = child["id"]
    assert "completedAt" not in _stored(team, cid)     # begins un-stamped despite completed parent
    # Move the child itself to a terminal status - _stamp_measurement_ts records ITS value.
    r = client.put(f"/api/projects/{cid}",
                   json={"name": child.get("name", "Item"), "status": "Released"},
                   headers=admin_headers)
    assert r.status_code == 200
    stamped = _stored(team, cid)["completedAt"]
    assert stamped and stamped != "2020-02-01T00:00:00+00:00"


def test_spawn_still_strips_itemkey_attachments_externalrefs(client, team, admin_headers):
    """Existing strip behavior unchanged by the refactor to the module constant."""
    pid = _create(client, admin_headers, recurrence="weekly", start="2026-01-01", dueWeeks=2).json()["id"]
    _patch(team, pid,
           attachments=[{"id": "a1", "key": f"items/{pid}/a1", "name": "f"}],
           externalRefs=[{"system": "assethub", "kind": "request", "status": "linked", "at": "x"}])
    parent_key = _stored(team, pid)["itemKey"]
    child = client.post(f"/api/projects/{pid}/recur", json={}, headers=admin_headers).json()
    stored = _stored(team, child["id"])
    assert not stored.get("attachments")
    assert "externalRefs" not in stored
    assert stored["itemKey"] and stored["itemKey"] != parent_key   # fresh key, not inherited


def test_spawn_sets_recurrence_parent_to_the_spawning_item(client, team, admin_headers):
    """recurrence_parent is on RECURRENCE_INHERITED because spawn overwrites it with the
    spawning parent id, not because the parent's value is carried over."""
    pid = _create(client, admin_headers, recurrence="weekly", start="2026-01-01", dueWeeks=2).json()["id"]
    _patch(team, pid, recurrence_parent=999999)   # a stale value that must NOT survive
    child = client.post(f"/api/projects/{pid}/recur", json={}, headers=admin_headers).json()
    assert _stored(team, child["id"])["recurrence_parent"] == pid
