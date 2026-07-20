"""Team Calendar Phase 1: assignment types (config-backed) + assignments (table).

Backend data layer only - capacity integration + UI land in later chunks.
Fixtures (client, team, admin_headers, editor_headers, viewer_headers) come from
conftest.py; the *_headers ones mint tokens directly (bypass the login limiter).
"""
import server


# ── assignment types (config-backed collection) ──────────────────────────────
def test_assignment_types_seeded(client, admin_headers):
    r = client.get("/api/assignment-types", headers=admin_headers)
    assert r.status_code == 200
    types = r.json()["assignmentTypes"]
    assert len(types) == 12
    by_name = {t["name"]: t for t in types}
    # spec defaults
    assert by_name["PTO"]["system"] and by_name["PTO"]["blocks_work"] and by_name["PTO"]["exclusive"]
    assert by_name["PTO"]["capacity_impact"] == 1.0 and not by_name["PTO"]["allow_tickets"]
    assert by_name["Training"]["capacity_impact"] == 0.5 and by_name["Training"]["allow_tickets"]
    assert by_name["Office"]["is_default"] is True and by_name["Office"]["system"]
    assert sum(t["is_default"] for t in types) == 1               # exactly one default
    assert sum(t["system"] for t in types) == 5                   # PTO/Sick/Holiday/Office/Remote


def test_assignment_types_read_any_role(client, viewer_headers):
    assert client.get("/api/assignment-types", headers=viewer_headers).status_code == 200


def test_assignment_types_put_admin_only(client, admin_headers, editor_headers):
    body = {"assignmentTypes": [{"id": "x", "name": "Custom X", "capacity_impact": 0.5}]}
    assert client.put("/api/assignment-types", json=body, headers=editor_headers).status_code == 403
    r = client.put("/api/assignment-types", json=body, headers=admin_headers)
    # a lone custom type is allowed only if no system type is being dropped -> this
    # array drops the 5 system defaults, so it must 422
    assert r.status_code == 422


def test_assignment_types_edit_preserves_system_and_one_default(client, admin_headers):
    types = client.get("/api/assignment-types", headers=admin_headers).json()["assignmentTypes"]
    # rename Training, try to sneak system=True onto it, and add a second default
    for t in types:
        if t["id"] == "training":
            t["name"] = "Training/Cert"; t["system"] = True; t["is_default"] = True
    r = client.put("/api/assignment-types", json={"assignmentTypes": types}, headers=admin_headers)
    assert r.status_code == 422        # two defaults (Office + Training) rejected
    # fix: only one default
    for t in types:
        if t["id"] == "office":
            t["is_default"] = False
    r = client.put("/api/assignment-types", json={"assignmentTypes": types}, headers=admin_headers)
    assert r.status_code == 200
    saved = {t["id"]: t for t in r.json()["assignmentTypes"]}
    assert saved["training"]["name"] == "Training/Cert"
    assert saved["training"]["system"] is False    # system is server-owned, not client-settable
    assert saved["training"]["is_default"] is True


def test_assignment_types_cannot_delete_system(client, admin_headers):
    types = client.get("/api/assignment-types", headers=admin_headers).json()["assignmentTypes"]
    keep = [t for t in types if t["id"] != "pto"]     # PTO is system
    assert client.put("/api/assignment-types", json={"assignmentTypes": keep}, headers=admin_headers).status_code == 422
    # a non-system (custom) type CAN be dropped
    types.append({"id": "board-support", "name": "Board Support", "capacity_impact": 0.3})
    assert client.put("/api/assignment-types", json={"assignmentTypes": types}, headers=admin_headers).status_code == 200
    types2 = client.get("/api/assignment-types", headers=admin_headers).json()["assignmentTypes"]
    keep2 = [t for t in types2 if t["id"] != "board-support"]
    assert client.put("/api/assignment-types", json={"assignmentTypes": keep2}, headers=admin_headers).status_code == 200


# ── assignments (relational table) ────────────────────────────────────────────
def _mk(client, headers, **over):
    body = {"type_id": "pto", "owner": "Everest", "username": "sam",
            "start_date": "2026-07-20", "end_date": "2026-07-22"}
    body.update(over)
    return client.post("/api/assignments", json=body, headers=headers)


def test_assignment_crud(client, admin_headers):
    r = _mk(client, admin_headers, description="Vacation")
    assert r.status_code == 200
    a = r.json()
    assert a["type_id"] == "pto" and a["owner"] == "Everest" and a["id"]
    # list
    lst = client.get("/api/assignments", headers=admin_headers).json()["assignments"]
    assert any(x["id"] == a["id"] for x in lst)
    # update
    u = client.put(f"/api/assignments/{a['id']}",
                   json={"type_id": "training", "owner": "Everest", "username": "sam",
                         "start_date": "2026-07-20", "end_date": "2026-07-21"}, headers=admin_headers)
    assert u.status_code == 200 and u.json()["type_id"] == "training"
    # delete
    assert client.delete(f"/api/assignments/{a['id']}", headers=admin_headers).status_code == 200
    assert client.delete(f"/api/assignments/{a['id']}", headers=admin_headers).status_code == 404


def test_assignment_role_gating(client, admin_headers, editor_headers, viewer_headers):
    assert _mk(client, viewer_headers).status_code == 403         # viewer cannot create
    assert _mk(client, editor_headers).status_code == 200         # editor can
    assert client.get("/api/assignments", headers=viewer_headers).status_code == 200  # viewer can read


def test_assignment_validation(client, admin_headers):
    assert _mk(client, admin_headers, type_id="does-not-exist").status_code == 422
    assert _mk(client, admin_headers, start_date="2026-07-22", end_date="2026-07-20").status_code == 422
    assert _mk(client, admin_headers, start_date="not-a-date").status_code == 422


def test_assignment_range_overlap_query(client, admin_headers):
    _mk(client, admin_headers, start_date="2026-07-20", end_date="2026-07-25")
    # a window that overlaps the middle returns it; a disjoint window does not
    hit = client.get("/api/assignments?owner=Everest&date_from=2026-07-23&date_to=2026-07-24", headers=admin_headers)
    assert len(hit.json()["assignments"]) >= 1
    miss = client.get("/api/assignments?owner=Everest&date_from=2026-08-01&date_to=2026-08-05", headers=admin_headers)
    assert all(x["end_date"] >= "2026-08-01" for x in miss.json()["assignments"])


# ── capacity integration (Phase 1b): impacts subtract from effective capacity ──
def _eff(client, headers, owner, date):
    return client.get(f"/api/capacity-overrides/effective?owner={owner}&date={date}", headers=headers).json()


def test_assignment_impact_reduces_effective_capacity(client, admin_headers):
    client.put("/api/config/ownerCapacity", json={"Everest": 3.0}, headers=admin_headers)
    # PTO (impact 1.0) on 07-20 drops effective 3 -> 2; an unaffected day stays 3.
    _mk(client, admin_headers, type_id="pto", owner="Everest", start_date="2026-07-20", end_date="2026-07-20")
    assert _eff(client, admin_headers, "Everest", "2026-07-20")["capacity"] == 2.0
    assert _eff(client, admin_headers, "Everest", "2026-07-20")["default"] == 3.0
    assert _eff(client, admin_headers, "Everest", "2026-07-25")["capacity"] == 3.0
    # Stacking: add Training (0.5) same day -> 3 - 1.0 - 0.5 = 1.5
    _mk(client, admin_headers, type_id="training", owner="Everest", start_date="2026-07-20", end_date="2026-07-20")
    assert _eff(client, admin_headers, "Everest", "2026-07-20")["capacity"] == 1.5
    # Above-base override (+contractor => 4) then impacts: 4 - 1.5 = 2.5
    client.post("/api/capacity-overrides", json={"owner": "Everest", "date": "2026-07-20", "capacity": 4.0}, headers=admin_headers)
    assert _eff(client, admin_headers, "Everest", "2026-07-20")["capacity"] == 2.5


def test_inactive_type_impact_ignored(client, admin_headers):
    client.put("/api/config/ownerCapacity", json={"Everest": 3.0}, headers=admin_headers)
    _mk(client, admin_headers, type_id="training", owner="Everest", start_date="2026-07-20", end_date="2026-07-20")
    assert _eff(client, admin_headers, "Everest", "2026-07-20")["capacity"] == 2.5
    # deactivate Training -> its impact no longer counts
    types = client.get("/api/assignment-types", headers=admin_headers).json()["assignmentTypes"]
    for t in types:
        if t["id"] == "training":
            t["active"] = False
    client.put("/api/assignment-types", json={"assignmentTypes": types}, headers=admin_headers)
    assert _eff(client, admin_headers, "Everest", "2026-07-20")["capacity"] == 3.0
