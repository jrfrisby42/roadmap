"""Department field - per-item `departments` (array on the item blob) over a CLOSED `departments`
config vocabulary (DEPT-PICKER-1). The vocabulary no longer grows when a value is typed on an item:
create and update REJECT (422) an unknown department, bulk import DROPS unknowns per item, and a
pre-existing out-of-vocabulary value on an item is grandfathered (saveable + removable, never re-offered).
Normalization is trim + case-insensitive dedup only - no case-fold, no auto-correct (exact match).
"""
import server


def _mk(client, headers, **fields):
    body = {"name": "Item", "status": "Planned", **fields}
    return client.post("/api/projects", json=body, headers=headers)


def _cfg_departments(client, headers):
    return client.get("/api/all", headers=headers).json().get("departments")


def _seed_oov_item(team, depts):
    """Insert an item carrying an out-of-vocabulary department directly (bypassing the API guard),
    modelling a value that predates the closed vocabulary or arrived from another Organization."""
    with server.db(team) as c:
        return server._insert_project(c, {"name": "Legacy", "status": "Planned", "departments": depts})


def test_departments_in_valid_keys():
    assert "departments" in server.VALID_KEYS


def test_normalize_trims_dedups_first_seen_casing():
    assert server._normalize_departments([" Sales ", "sales", "SALES", "", None, "Ops"]) == ["Sales", "Ops"]


def test_api_all_exposes_departments(client, team, admin_headers):
    # DEPT-DEFAULTS-1: a new team is seeded with the standard department set.
    assert _cfg_departments(client, admin_headers) == server._DEFAULT_DEPARTMENTS


def test_item_departments_normalized_on_save_within_vocabulary(client, team, admin_headers):
    # Trim + case-insensitive dedup still apply; values must be EXACT members of the seeded (all-caps) vocab.
    it = _mk(client, admin_headers, departments=[" SALES ", "SALES", "OPERATIONS"]).json()
    assert it["departments"] == ["SALES", "OPERATIONS"]


# ── The closed vocabulary: create/update reject unknowns (the load-bearing guard) ───────────────────────
def test_create_rejects_unknown_department(client, team, admin_headers):
    r = _mk(client, admin_headers, departments=["QA Guild"])
    assert r.status_code == 422


def test_create_unknown_does_NOT_grow_config(client, team, admin_headers):
    """THE bug this stage fixes: posting an unknown department to the API must not grow config.departments.
    This is the load-bearing guard - a client-only picker would pass with this hole wide open."""
    before = _cfg_departments(client, admin_headers)
    r = _mk(client, admin_headers, departments=["QA Guild", "Field Techs"])
    assert r.status_code == 422
    after = _cfg_departments(client, admin_headers)
    assert after == before   # the vocabulary did not grow


def test_exact_match_required_no_casefold(client, team, admin_headers):
    # Vocab has "SALES". A lowercase "sales" is NOT auto-corrected to it - exact match is the point (Part 2.3).
    assert _mk(client, admin_headers, departments=["sales"]).status_code == 422
    assert _mk(client, admin_headers, departments=["SALES"]).json()["departments"] == ["SALES"]


def test_update_rejects_new_unknown_and_does_not_grow_config(client, team, admin_headers):
    pid = _mk(client, admin_headers).json()["id"]
    before = _cfg_departments(client, admin_headers)
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "Planned", "departments": ["Robotics Lab"]},
                   headers=admin_headers)
    assert r.status_code == 422
    assert _cfg_departments(client, admin_headers) == before


def test_editor_cannot_create_department_but_can_set_configured(client, team, admin_headers, editor_headers):
    assert _mk(client, editor_headers, departments=["FieldOps"]).status_code == 422           # cannot invent
    assert _mk(client, editor_headers, departments=["SERVICE"]).json()["departments"] == ["SERVICE"]  # can set configured


# ── Part 2.4: a pre-existing out-of-vocabulary value is grandfathered ────────────────────────────────────
def test_update_grandfathers_existing_oov_and_allows_removal(client, team, admin_headers):
    pid = _seed_oov_item(team, ["GHOSTDEPT"])
    # keeping the stored OOV value succeeds (grandfathered)
    keep = client.put(f"/api/projects/{pid}",
                      json={"name": "Legacy", "status": "Planned", "departments": ["GHOSTDEPT"]},
                      headers=admin_headers)
    assert keep.status_code == 200 and keep.json()["departments"] == ["GHOSTDEPT"]
    # it can also be combined with a configured value
    both = client.put(f"/api/projects/{pid}",
                      json={"name": "Legacy", "status": "Planned", "departments": ["GHOSTDEPT", "SALES"]},
                      headers=admin_headers)
    assert both.status_code == 200 and both.json()["departments"] == ["GHOSTDEPT", "SALES"]
    # and it can be removed
    gone = client.put(f"/api/projects/{pid}",
                      json={"name": "Legacy", "status": "Planned", "departments": []},
                      headers=admin_headers)
    assert gone.status_code == 200 and gone.json()["departments"] == []


def test_grandfather_reads_stored_blob_not_payload(client, team, admin_headers):
    """Addition 1: grandfathering compares against the STORED blob, not the incoming payload - so a client
    cannot grandfather an arbitrary value by sending it. An item stored with only SALES cannot introduce a
    new unknown alongside it."""
    pid = _mk(client, admin_headers, departments=["SALES"]).json()["id"]
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "Planned", "departments": ["SALES", "GHOST"]},
                   headers=admin_headers)
    assert r.status_code == 422   # GHOST is neither configured nor on the stored item


# ── Decision 1: bulk import drops unknowns per item (attachmentsDropped precedent) ───────────────────────
def test_bulk_import_drops_unknown_departments_per_item(client, team, admin_headers):
    payload = {
        "departments": ["SALES", "SERVICE"],   # the import's own vocabulary
        "projects": [
            {"name": "A", "status": "Planned", "departments": ["SALES", "STALEDEPT"]},
            {"name": "B", "status": "Planned", "departments": ["SERVICE"]},
        ],
    }
    r = client.post("/api/import", json=payload, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["itemsWithDeptDropped"] == 1   # only item A dropped something
    items = {p["name"]: p for p in client.get("/api/all", headers=admin_headers).json()["projects"]}
    assert items["A"]["departments"] == ["SALES"]          # unknown dropped
    assert items["A"]["departmentsDropped"] == ["STALEDEPT"]   # recorded PER ITEM
    assert items["B"]["departments"] == ["SERVICE"]
    assert "departmentsDropped" not in items["B"]           # nothing dropped -> no marker
    # config is the import's vocabulary, not grown by the dropped value
    assert client.get("/api/all", headers=admin_headers).json()["departments"] == ["SALES", "SERVICE"]


# ── Phase 1: departmentMeta config (per-dept color + notify emails) - unchanged invariants ──────────────
def test_department_meta_in_valid_keys():
    assert "departmentMeta" in server.VALID_KEYS


def test_department_meta_round_trips(client, team, admin_headers):
    meta = {"IT": {"color": "#0059A9", "emails": "it@x.com, ops@x.com"},
            "FINANCE": {"color": "#22b96e", "emails": "fin@x.com"}}
    assert client.put("/api/config/departmentMeta", json=meta, headers=admin_headers).status_code == 200
    got = client.get("/api/all", headers=admin_headers).json()["departmentMeta"]
    assert got == meta


def test_department_meta_admin_only(client, team, editor_headers):
    assert client.put("/api/config/departmentMeta", json={"IT": {"color": "#000"}},
                      headers=editor_headers).status_code == 403


def test_department_meta_seeds_colors_no_emails(client, team, admin_headers):
    # DEPT-DEFAULTS-1: a new team seeds pill colors for the standard departments, and NO notify
    # emails (routing is per-team).
    meta = client.get("/api/all", headers=admin_headers).json()["departmentMeta"]
    assert meta.get("FINANCE", {}).get("color") == "#83d043"
    assert all("emails" not in v for v in meta.values())
