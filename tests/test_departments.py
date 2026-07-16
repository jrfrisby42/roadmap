"""Department field — per-item `departments` (array on the item blob) + a shared
`departments` config list that grows when new values are typed on an item.
Normalization: trim, drop empties, case-insensitive dedup, first-seen casing.
"""
import server


def _mk(client, headers, **fields):
    body = {"name": "Item", "status": "Planned", **fields}
    return client.post("/api/projects", json=body, headers=headers).json()


def _cfg_departments(client, headers):
    return client.get("/api/all", headers=headers).json().get("departments")


def test_departments_in_valid_keys():
    assert "departments" in server.VALID_KEYS


def test_normalize_trims_dedups_first_seen_casing():
    assert server._normalize_departments([" Sales ", "sales", "SALES", "", None, "Ops"]) == ["Sales", "Ops"]


def test_api_all_exposes_departments(client, team, admin_headers):
    assert _cfg_departments(client, admin_headers) == []   # seeded empty


def test_item_departments_are_normalized_on_save(client, team, admin_headers):
    it = _mk(client, admin_headers, departments=[" Sales ", "sales", "Ops"])
    assert it["departments"] == ["Sales", "Ops"]


def test_create_unions_new_departments_into_config(client, team, admin_headers):
    _mk(client, admin_headers, departments=["Sales", " Logistics "])
    cfg = _cfg_departments(client, admin_headers)
    assert "Sales" in cfg and "Logistics" in cfg   # trimmed into the shared list


def test_case_insensitive_no_duplicate_in_config(client, team, admin_headers):
    _mk(client, admin_headers, departments=["Logistics"])
    _mk(client, admin_headers, departments=[" logistics ", "LOGISTICS"])
    cfg = _cfg_departments(client, admin_headers)
    assert [d for d in cfg if d.lower() == "logistics"] == ["Logistics"]   # one entry, first-seen casing


def test_update_unions_departments(client, team, admin_headers):
    pid = _mk(client, admin_headers)["id"]
    client.put(f"/api/projects/{pid}",
               json={"name": "Item", "status": "Planned", "departments": ["Marketing"]},
               headers=admin_headers)
    assert "Marketing" in _cfg_departments(client, admin_headers)


def test_editor_can_create_a_department(client, team, admin_headers, editor_headers):
    _mk(client, editor_headers, departments=["FieldOps"])
    assert "FieldOps" in _cfg_departments(client, admin_headers)


# ── Phase 1: departmentMeta config (per-dept color + notify emails) ────────────
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


def test_department_meta_defaults_empty(client, team, admin_headers):
    assert client.get("/api/all", headers=admin_headers).json()["departmentMeta"] == {}
