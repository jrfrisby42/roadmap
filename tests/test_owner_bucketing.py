"""Owner bucketing (4.38.0): derive an item's/assignment's owner pool from its
assignee's ownerFilter.

Rules under test:
  - Items + assignments, create & update: a BLANK owner is filled from the
    assignee's ownerFilter pod.
  - Items, on an assignee CHANGE (update): the owner FOLLOWS the new assignee's
    pod - unless the client explicitly set the owner in the same save.
  - Never overrides an owner when the assignee is unchanged; never blanks an
    owner; no-op when the assignee has no pod.

Fixtures come from conftest.py. Tokens are minted directly (not from the users
config), so replacing the users config here does not affect auth.
"""

USERS = [
    {"username": "admin", "role": "admin", "builtin": True},
    {"username": "sam",   "role": "editor", "ownerFilter": "Everest"},
    {"username": "amir",  "role": "admin",  "ownerFilter": "Kilimanjaro"},
    {"username": "nopod", "role": "editor", "ownerFilter": ""},
]


def _seed_users(client, admin_headers):
    r = client.put("/api/config/users", json=USERS, headers=admin_headers)
    assert r.status_code == 200


def _mk_item(client, headers, **fields):
    return client.post("/api/projects", json=fields, headers=headers)


# ── Items: create ─────────────────────────────────────────────────────────────
def test_item_create_fills_blank_owner_from_assignee(client, admin_headers):
    _seed_users(client, admin_headers)
    r = _mk_item(client, admin_headers, name="Alpha", assignee="sam")
    assert r.status_code == 200
    assert r.json()["dev"] == "Everest"


def test_item_create_no_pod_leaves_owner_blank(client, admin_headers):
    _seed_users(client, admin_headers)
    r = _mk_item(client, admin_headers, name="Beta", assignee="nopod")
    assert r.status_code == 200
    assert (r.json().get("dev") or "") == ""


def test_item_create_explicit_owner_preserved(client, admin_headers):
    _seed_users(client, admin_headers)
    r = _mk_item(client, admin_headers, name="Gamma", assignee="sam", dev="Denali")
    assert r.status_code == 200
    assert r.json()["dev"] == "Denali"          # explicit owner wins over the assignee's pod


# ── Items: update ─────────────────────────────────────────────────────────────
def test_item_update_fills_blank_owner(client, admin_headers):
    _seed_users(client, admin_headers)
    pid = _mk_item(client, admin_headers, name="Delta").json()["id"]   # no assignee, no owner
    u = client.put(f"/api/projects/{pid}", json={"assignee": "sam"}, headers=admin_headers)
    assert u.status_code == 200
    assert u.json()["dev"] == "Everest"


def test_item_update_follows_assignee_change(client, admin_headers):
    _seed_users(client, admin_headers)
    pid = _mk_item(client, admin_headers, name="Eps", assignee="sam").json()["id"]   # -> Everest
    u = client.put(f"/api/projects/{pid}", json={"assignee": "amir"}, headers=admin_headers)
    assert u.status_code == 200
    assert u.json()["dev"] == "Kilimanjaro"     # owner followed the new assignee's pod


def test_item_update_explicit_owner_wins_over_follow(client, admin_headers):
    _seed_users(client, admin_headers)
    pid = _mk_item(client, admin_headers, name="Zeta", assignee="sam").json()["id"]  # -> Everest
    u = client.put(f"/api/projects/{pid}",
                   json={"assignee": "amir", "dev": "Rainier"}, headers=admin_headers)
    assert u.status_code == 200
    assert u.json()["dev"] == "Rainier"         # explicit owner in the same save wins


def test_item_update_unchanged_assignee_does_not_override_owner(client, admin_headers):
    _seed_users(client, admin_headers)
    # explicit mismatched owner at create (assignee sam's pod is Everest)
    created = _mk_item(client, admin_headers, name="Eta", assignee="sam", dev="Rainier").json()
    assert created["dev"] == "Rainier"          # explicit owner preserved at create
    pid = created["id"]
    u = client.put(f"/api/projects/{pid}",
                   json={"name": "Eta2", "assignee": "sam", "dev": "Rainier"}, headers=admin_headers)
    assert u.status_code == 200
    assert u.json()["dev"] == "Rainier"         # assignee unchanged -> owner left alone


# ── Assignments: create + update ───────────────────────────────────────────────
def test_assignment_create_fills_blank_owner(client, admin_headers):
    _seed_users(client, admin_headers)
    r = client.post("/api/assignments",
                    json={"type_id": "pto", "username": "sam",
                          "start_date": "2026-07-20", "end_date": "2026-07-22"},
                    headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["owner"] == "Everest"


def test_assignment_create_explicit_owner_preserved(client, admin_headers):
    _seed_users(client, admin_headers)
    r = client.post("/api/assignments",
                    json={"type_id": "pto", "username": "sam", "owner": "Denali",
                          "start_date": "2026-07-20", "end_date": "2026-07-22"},
                    headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["owner"] == "Denali"


def test_assignment_update_fills_blank_owner(client, admin_headers):
    _seed_users(client, admin_headers)
    aid = client.post("/api/assignments",
                      json={"type_id": "pto", "username": "sam", "owner": "Denali",
                            "start_date": "2026-07-20", "end_date": "2026-07-22"},
                      headers=admin_headers).json()["id"]
    u = client.put(f"/api/assignments/{aid}",
                   json={"type_id": "pto", "username": "sam", "owner": "",
                         "start_date": "2026-07-20", "end_date": "2026-07-22"},
                   headers=admin_headers)
    assert u.status_code == 200
    assert u.json()["owner"] == "Everest"       # blank owner refilled from the assignee's pod


def test_assignment_update_follows_reassignment(client, admin_headers):
    _seed_users(client, admin_headers)
    aid = client.post("/api/assignments",
                      json={"type_id": "pto", "username": "sam",   # -> owner Everest (auto)
                            "start_date": "2026-07-20", "end_date": "2026-07-22"},
                      headers=admin_headers).json()["id"]
    # reassign to amir; owner still carries the old (auto-derived) Everest pod -> follows
    u = client.put(f"/api/assignments/{aid}",
                   json={"type_id": "pto", "username": "amir", "owner": "Everest",
                         "start_date": "2026-07-20", "end_date": "2026-07-22"},
                   headers=admin_headers)
    assert u.status_code == 200
    assert u.json()["owner"] == "Kilimanjaro"   # followed the new assignee's pod


def test_assignment_update_reassignment_keeps_manual_owner(client, admin_headers):
    _seed_users(client, admin_headers)
    aid = client.post("/api/assignments",
                      json={"type_id": "pto", "username": "sam", "owner": "Denali",  # manual, != sam's pod
                            "start_date": "2026-07-20", "end_date": "2026-07-22"},
                      headers=admin_headers).json()["id"]
    # reassign to amir; owner was NOT tracking sam's pod (it's a hand-picked Denali) -> left alone
    u = client.put(f"/api/assignments/{aid}",
                   json={"type_id": "pto", "username": "amir", "owner": "Denali",
                         "start_date": "2026-07-20", "end_date": "2026-07-22"},
                   headers=admin_headers)
    assert u.status_code == 200
    assert u.json()["owner"] == "Denali"        # deliberate owner respected, no follow
