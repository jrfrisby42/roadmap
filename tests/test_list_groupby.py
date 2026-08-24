"""LIST-GROUPBY-1: /api/items group-count aggregate + the __none__ (unset) filter.

The grouped List renders headers from the aggregate (`?group_by=<field>` -> {groups,total}) and loads
each group's rows by re-querying with `<field>=<value>` (or `__none__` for the unset bucket). The
aggregate must reuse the SAME where as the row query, so counts can never disagree with the rows a
group shows. These cover the server half; the grouped render is frontend JS (no pytest harness).
"""
import server


def _mk(client, headers, **fields):
    r = client.post("/api/projects", json={"name": "Item", "status": "New", **fields}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _groups(client, headers, field, extra=""):
    r = client.get(f"/api/items?group_by={field}{extra}", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    return {(g["value"]): g["count"] for g in body["groups"]}, body["total"]


# ── the aggregate ────────────────────────────────────────────────────────────
def test_group_by_status_counts(client, team, admin_headers):
    _mk(client, admin_headers, status="New")
    _mk(client, admin_headers, status="New")
    _mk(client, admin_headers, status="In Progress")
    g, total = _groups(client, admin_headers, "status")
    assert g.get("New") == 2 and g.get("In Progress") == 1
    assert total == sum(g.values()) == 3


def test_group_by_priority_and_owner(client, team, admin_headers):
    _mk(client, admin_headers, priority=1, dev="Pod A")
    _mk(client, admin_headers, priority=1, dev="Pod B")
    _mk(client, admin_headers, priority=3, dev="Pod A")
    gp, _ = _groups(client, admin_headers, "priority")
    assert gp.get("1") == 2 and gp.get("3") == 1
    go, _ = _groups(client, admin_headers, "owner")     # owner column = the `dev` blob field
    assert go.get("Pod A") == 2 and go.get("Pod B") == 1


def test_group_by_respects_active_filter(client, team, admin_headers):
    _mk(client, admin_headers, status="New", product="Alpha")
    _mk(client, admin_headers, status="New", product="Beta")
    _mk(client, admin_headers, status="Done", product="Alpha")
    # unfiltered: New=2, Done=1
    g_all, total_all = _groups(client, admin_headers, "status")
    assert g_all.get("New") == 2 and total_all == 3
    # filtered to product=Alpha: New=1, Done=1 (the Beta 'New' drops out of the counts)
    g_a, total_a = _groups(client, admin_headers, "status", extra="&product=Alpha")
    assert g_a.get("New") == 1 and g_a.get("Done") == 1 and total_a == 2


def test_group_by_unset_bucket_is_a_null_group(client, team, admin_headers):
    _mk(client, admin_headers, assignee="alice")
    _mk(client, admin_headers)                          # no assignee -> unset
    _mk(client, admin_headers)                          # no assignee -> unset
    g, total = _groups(client, admin_headers, "assignee")
    assert g.get("alice") == 1
    assert g.get(None) == 2                             # the unset bucket, value=null
    assert total == 3


def test_group_by_invalid_field_400(client, team, admin_headers):
    assert client.get("/api/items?group_by=notacolumn", headers=admin_headers).status_code == 400
    assert client.get("/api/items?group_by=name", headers=admin_headers).status_code == 400   # not a groupable col


# ── the __none__ (unset) row filter ───────────────────────────────────────────
def test_none_filter_returns_only_unset_rows(client, team, admin_headers):
    a = _mk(client, admin_headers, assignee="alice")
    u1 = _mk(client, admin_headers)
    u2 = _mk(client, admin_headers)
    r = client.get("/api/items?assignee=__none__", headers=admin_headers)
    assert r.status_code == 200
    ids = {it["id"] for it in r.json()["items"]}
    assert ids == {u1, u2} and a not in ids


def test_none_filter_combines_with_real_values_via_or(client, team, admin_headers):
    # assignee (unlike status) can genuinely be unset - create_project defaults an empty status.
    a = _mk(client, admin_headers, assignee="alice")
    b = _mk(client, admin_headers, assignee="bob")
    u = _mk(client, admin_headers)                      # unset assignee
    r = client.get("/api/items?assignee=alice,__none__", headers=admin_headers)
    ids = {it["id"] for it in r.json()["items"]}
    assert ids == {a, u} and b not in ids               # alice OR unset, not bob


def test_group_counts_match_the_rows_they_load(client, team, admin_headers):
    # The contract: a group's count equals the number of rows its __none__/value query returns.
    _mk(client, admin_headers)                          # unset assignee
    _mk(client, admin_headers)                          # unset assignee
    _mk(client, admin_headers, assignee="bob")
    g, _ = _groups(client, admin_headers, "assignee")
    rows = client.get("/api/items?assignee=__none__&page_size=500", headers=admin_headers).json()["items"]
    assert g.get(None) == len(rows) == 2
