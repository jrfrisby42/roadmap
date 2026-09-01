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
    # DEPT-DEFAULTS-1: a new team is seeded with the standard department set.
    assert _cfg_departments(client, admin_headers) == server._DEFAULT_DEPARTMENTS


def test_item_departments_are_normalized_on_save(client, team, admin_headers):
    it = _mk(client, admin_headers, departments=[" Sales ", "sales", "Ops"])
    assert it["departments"] == ["Sales", "Ops"]


def test_create_unions_new_departments_into_config(client, team, admin_headers):
    # Novel names (not in the seeded set) so the union is what is being tested, not a collision.
    _mk(client, admin_headers, departments=["QA Guild", " Field Techs "])
    cfg = _cfg_departments(client, admin_headers)
    assert "QA Guild" in cfg and "Field Techs" in cfg   # trimmed into the shared list


def test_case_insensitive_no_duplicate_in_config(client, team, admin_headers):
    # Novel name so first-seen casing is exercised (the seeded set is all-caps already).
    _mk(client, admin_headers, departments=["Zephyr"])
    _mk(client, admin_headers, departments=[" zephyr ", "ZEPHYR"])
    cfg = _cfg_departments(client, admin_headers)
    assert [d for d in cfg if d.lower() == "zephyr"] == ["Zephyr"]   # one entry, first-seen casing


def test_update_unions_departments(client, team, admin_headers):
    pid = _mk(client, admin_headers)["id"]
    client.put(f"/api/projects/{pid}",
               json={"name": "Item", "status": "Planned", "departments": ["Robotics Lab"]},
               headers=admin_headers)
    assert "Robotics Lab" in _cfg_departments(client, admin_headers)


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


def test_department_meta_seeds_colors_no_emails(client, team, admin_headers):
    # DEPT-DEFAULTS-1: a new team seeds pill colors for the standard departments, and NO notify
    # emails (routing is per-team). Was "defaults empty" before the standard set was seeded.
    meta = client.get("/api/all", headers=admin_headers).json()["departmentMeta"]
    assert meta.get("FINANCE", {}).get("color") == "#83d043"
    assert all("emails" not in v for v in meta.values())
