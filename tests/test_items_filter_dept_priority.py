"""/api/items filtering by priority (indexed column) and department (item_departments side
index). Priority is scalar/in-set; department is multi-valued/contains-any. Both support a
'__none__' member meaning "unset", and both combine (AND) with the other filters. The department
index is derived from the blob and re-synced on every write (via _reindex_project)."""
import server


def _mk(client, headers, **fields):
    body = {"name": "Item", "status": "Planned", **fields}
    return client.post("/api/projects", json=body, headers=headers).json()["id"]


def _total(client, headers, qs):
    return client.get("/api/items?" + qs, headers=headers).json()["total"]


def _ids(client, headers, qs):
    return {i["id"] for i in client.get("/api/items?" + qs, headers=headers).json()["items"]}


# ── priority (scalar, indexed column) ─────────────────────────────────────────

def test_filter_by_single_priority(client, team, admin_headers):
    _mk(client, admin_headers, priority="1")
    _mk(client, admin_headers, priority="2")
    _mk(client, admin_headers, priority="2")
    assert _total(client, admin_headers, "priority=2") == 2
    assert _total(client, admin_headers, "priority=1") == 1


def test_filter_by_multiple_priorities_is_or(client, team, admin_headers):
    _mk(client, admin_headers, priority="1")
    _mk(client, admin_headers, priority="2")
    _mk(client, admin_headers, priority="4")
    assert _total(client, admin_headers, "priority=1,4") == 2


def test_filter_no_priority(client, team, admin_headers):
    _mk(client, admin_headers, priority="2")
    _mk(client, admin_headers)                 # no priority
    _mk(client, admin_headers, priority="")    # empty -> unset
    assert _total(client, admin_headers, "priority=__none__") == 2
    # a real code OR none
    assert _total(client, admin_headers, "priority=2,__none__") == 3


def test_priority_ands_with_other_filters(client, team, admin_headers):
    _mk(client, admin_headers, priority="2", type="Feature")
    _mk(client, admin_headers, priority="2", type="Enhancement")
    assert _total(client, admin_headers, "priority=2&type=Feature") == 1


# ── department (multi-valued, side index, contains-any) ───────────────────────

def test_filter_by_single_department(client, team, admin_headers):
    _mk(client, admin_headers, departments=["SALES"])
    _mk(client, admin_headers, departments=["SERVICE"])
    _mk(client, admin_headers, departments=["SALES", "WAREHOUSE"])
    assert _total(client, admin_headers, "department=SALES") == 2   # contains-any
    assert _total(client, admin_headers, "department=WAREHOUSE") == 1


def test_filter_by_multiple_departments_is_or(client, team, admin_headers):
    _mk(client, admin_headers, departments=["SALES"])
    _mk(client, admin_headers, departments=["SERVICE"])
    _mk(client, admin_headers, departments=["WAREHOUSE"])
    assert _total(client, admin_headers, "department=SALES,SERVICE") == 2


def test_multi_department_item_matches_any(client, team, admin_headers):
    pid = _mk(client, admin_headers, departments=["SALES", "SERVICE", "WAREHOUSE"])
    assert pid in _ids(client, admin_headers, "department=SERVICE")
    assert pid in _ids(client, admin_headers, "department=SALES")


def test_filter_no_department(client, team, admin_headers):
    _mk(client, admin_headers, departments=["SALES"])
    _mk(client, admin_headers)                    # no departments key
    _mk(client, admin_headers, departments=[])    # empty list
    assert _total(client, admin_headers, "department=__none__") == 2
    assert _total(client, admin_headers, "department=SALES,__none__") == 3


def test_department_ands_with_other_filters(client, team, admin_headers):
    _mk(client, admin_headers, departments=["SALES"], type="Feature")
    _mk(client, admin_headers, departments=["SALES"], type="Enhancement")
    assert _total(client, admin_headers, "department=SALES&type=Feature") == 1


# ── index stays in sync with the blob ─────────────────────────────────────────

def test_department_index_resyncs_on_update(client, team, admin_headers):
    pid = _mk(client, admin_headers, departments=["SALES"])
    assert _total(client, admin_headers, "department=SALES") == 1
    # move it to SERVICE
    client.put(f"/api/projects/{pid}", json={"departments": ["SERVICE"]}, headers=admin_headers)
    assert _total(client, admin_headers, "department=SALES") == 0
    assert _total(client, admin_headers, "department=SERVICE") == 1
    # clear it -> now "(No department)"
    client.put(f"/api/projects/{pid}", json={"departments": []}, headers=admin_headers)
    assert _total(client, admin_headers, "department=SERVICE") == 0
    assert _total(client, admin_headers, "department=__none__") == 1


def test_backfill_rebuilds_index_for_preexisting_items(client, team, admin_headers):
    # Regression for the boot-time backfill bug: the index must be (re)buildable for items that
    # predate it. Simulate that by wiping the index + the fresh marker, then run the deferred pass.
    _mk(client, admin_headers, departments=["SALES"])
    _mk(client, admin_headers, departments=["SERVICE", "WAREHOUSE"])
    with server.db(team) as c:
        c.execute("DELETE FROM item_departments")
        c.execute("DELETE FROM config WHERE key='__deptIndexBuilt2'")
    assert _total(client, admin_headers, "department=SALES") == 0   # index empty -> nothing matches
    n = server._backfill_item_departments(team)
    assert n >= 2
    assert _total(client, admin_headers, "department=SALES") == 1
    assert _total(client, admin_headers, "department=WAREHOUSE") == 1
    assert server._backfill_item_departments(team) == -1               # marker set -> no-op second run


def test_department_index_dropped_on_delete(client, team, admin_headers):
    pid = _mk(client, admin_headers, departments=["SALES"])
    assert _total(client, admin_headers, "department=SALES") == 1
    client.delete(f"/api/projects/{pid}", headers=admin_headers)
    assert _total(client, admin_headers, "department=SALES") == 0
    with server.db(team) as c:
        assert c.execute("SELECT count(*) FROM item_departments WHERE item_id=?", (pid,)).fetchone()[0] == 0
