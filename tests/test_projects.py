"""Business invariants on items: the test-period rule, parallelResources rounding,
and the active-status lock on parallelResources."""
import pytest
import server


# ── round_up_to_quarter (pure helper) ─────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    (1.0, 1.0),
    (1.01, 1.25),
    (1.26, 1.50),
    (2.62, 2.75),
    (3.00, 3.00),
    (0, 1.0),       # min floor is 1.0
    (-5, 1.0),      # negatives floor to 1.0
    ("not-a-number", 1.0),
])
def test_round_up_to_quarter(raw, expected):
    assert server.round_up_to_quarter(raw) == expected


def _create(client, headers, **fields):
    body = {"name": "Item", "status": "Planned", **fields}
    return client.post("/api/projects", json=body, headers=headers)


# ── testWeeks must be strictly less than dueWeeks (HTTP 422) ───────────────────
def test_test_period_equal_to_estimate_rejected(client, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "Planned", "dueWeeks": 3, "testWeeks": 3},
                   headers=admin_headers)
    assert r.status_code == 422


def test_test_period_exceeds_estimate_rejected(client, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "Planned", "dueWeeks": 3, "testWeeks": 5},
                   headers=admin_headers)
    assert r.status_code == 422


def test_test_period_less_than_estimate_ok(client, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "Planned", "dueWeeks": 3, "testWeeks": 2},
                   headers=admin_headers)
    assert r.status_code == 200


# ── parallelResources is rounded up to the nearest 0.25 on create ─────────────
def test_create_rounds_parallel_resources_in_response(client, admin_headers):
    r = _create(client, admin_headers, parallelResources=1.1)
    assert r.status_code == 200
    assert r.json()["parallelResources"] == 1.25


def test_create_persists_rounded_parallel_resources(client, admin_headers):
    """Regression: the value must be rounded in the DB, not only in the response."""
    pid = _create(client, admin_headers, parallelResources=2.62).json()["id"]
    allr = client.get("/api/all", headers=admin_headers).json()
    stored = next(p for p in allr["projects"] if p["id"] == pid)
    assert stored["parallelResources"] == 2.75


# ── parallelResources is rounded up to the nearest 0.25 on update ──────────────
def test_update_rounds_parallel_resources(client, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "Planned", "parallelResources": 1.1},
                   headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["parallelResources"] == 1.25


# ── parallelResources cannot change while the item is in an active status ──────
def test_parallel_resources_locked_when_active(client, admin_headers):
    # Mark "In Progress" as an active status for this team.
    client.put("/api/config/statusIsActive", json={"In Progress": True}, headers=admin_headers)

    created = _create(client, admin_headers, status="In Progress", parallelResources=2.0)
    pid = created.json()["id"]

    # Attempting to change parallelResources while active -> 422.
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "In Progress", "parallelResources": 3.0},
                   headers=admin_headers)
    assert r.status_code == 422

    # Same value (no change) is allowed even while active.
    r_same = client.put(f"/api/projects/{pid}",
                        json={"name": "Item", "status": "In Progress", "parallelResources": 2.0},
                        headers=admin_headers)
    assert r_same.status_code == 200


# ── T1 (4.11.0): update MERGES the patch — omitted fields are NOT wiped ─────────
def test_update_preserves_omitted_fields(client, team, admin_headers):
    # The classic edit modal sends a fixed field list; anything it omits used to be
    # wiped by the wholesale-blob replace. update_project now merges, so server-owned
    # / other fields the client didn't send survive the edit.
    created = _create(client, admin_headers,
                      assignee="Alice", sprintId="SPR-1", storyPoints=5,
                      reporter="Bob", release="REL-9").json()
    pid = created["id"]
    # A partial edit that omits assignee/sprintId/storyPoints/reporter/release.
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Renamed", "status": "Planned"}, headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Renamed"           # the change applied
    assert body["assignee"] == "Alice"         # omitted fields preserved (was wiped pre-4.11.0)
    assert body["sprintId"] == "SPR-1"
    assert body["storyPoints"] == 5
    assert body["reporter"] == "Bob"
    assert body["release"] == "REL-9"
    # And it's durable in the stored blob, not just the response.
    with server.db(team) as c:
        import json as _json
        stored = _json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])
    assert stored["assignee"] == "Alice" and stored["sprintId"] == "SPR-1"


def test_update_can_still_clear_a_field_explicitly(client, team, admin_headers):
    # Merge preserves OMITTED fields but must still let a client CLEAR a field it
    # explicitly sends as empty (how the modal clears e.g. owner).
    pid = _create(client, admin_headers, dev="Everest").json()["id"]
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "Planned", "dev": ""}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["dev"] == ""               # explicit empty applied, not reverted to "Everest"


# ── T3 (4.12.0): optimistic concurrency on item PUT (updated_ts precondition) ───
def test_update_optimistic_lock_conflict(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    item = next(p for p in client.get("/api/all", headers=admin_headers).json()["projects"]
                if p["id"] == pid)
    ts = item["updated_ts"]
    assert ts                                   # /api/all now exposes the token
    # Correct base token → 200, and the response carries a NEW token.
    r_ok = client.put(f"/api/projects/{pid}",
                      json={"name": "E1", "status": "Planned", "_baseUpdatedTs": ts},
                      headers=admin_headers)
    assert r_ok.status_code == 200
    new_ts = r_ok.json()["updated_ts"]
    assert new_ts and new_ts != ts
    # Reusing the now-STALE token → 409 with the current token echoed back.
    r_conf = client.put(f"/api/projects/{pid}",
                        json={"name": "E2", "status": "Planned", "_baseUpdatedTs": ts},
                        headers=admin_headers)
    assert r_conf.status_code == 409
    assert r_conf.json()["detail"]["currentUpdatedTs"] == new_ts


def test_update_without_token_bypasses_lock(client, team, admin_headers):
    # System / batch ops (and old clients) omit the token → never 409, even across
    # back-to-back edits. The precondition is strictly opt-in.
    pid = _create(client, admin_headers).json()["id"]
    r1 = client.put(f"/api/projects/{pid}", json={"name": "A", "status": "Planned"},
                    headers=admin_headers)
    r2 = client.put(f"/api/projects/{pid}", json={"name": "B", "status": "Planned"},
                    headers=admin_headers)
    assert r1.status_code == 200 and r2.status_code == 200


# ── 4.13.0: audit actor is not spoofable via _username ──────────────────────────
def test_update_audit_actor_not_spoofable(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    client.put(f"/api/projects/{pid}",
               json={"name": "X", "status": "Planned", "_username": "impostor"},
               headers=admin_headers)
    with server.db(team) as c:
        row = c.execute("SELECT username FROM audit_log WHERE action='update' AND project_id=? "
                        "ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    assert row["username"] == "admin"        # spoofed _username ignored; real user recorded


def test_update_audit_actor_allows_system_sentinel(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    client.put(f"/api/projects/{pid}",
               json={"name": "Y", "status": "Planned", "_username": "System"},
               headers=admin_headers)
    with server.db(team) as c:
        row = c.execute("SELECT username FROM audit_log WHERE action='update' AND project_id=? "
                        "ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    assert row["username"] == "System"       # the automated-ops sentinel is preserved
