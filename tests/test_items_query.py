"""Phase 1c (JIRA-REPLACEMENT.md §3.2): the /api/items query/search/paginate
endpoint over the indexed columns."""
import server


def _mk(client, headers, **fields):
    body = {"status": "Planned", **fields}
    return client.post("/api/projects", json=body, headers=headers).json()["id"]


def _seed(client, headers):
    return {
        "feat":  _mk(client, headers, name="Login revamp", type="Feature",
                     status="In Progress", dev="PodA", product="Fraznet"),
        "enh":   _mk(client, headers, name="SSO support", type="Enhancement",
                     status="Planned", dev="PodA", product="Fraznet"),
        "task":  _mk(client, headers, name="Wire OIDC", type="Task",
                     status="Planned", dev="PodB", product="HubSpot"),
    }


def test_list_all_returns_blobs_with_ids(client, team, admin_headers):
    ids = _seed(client, admin_headers)
    r = client.get("/api/items", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert {i["id"] for i in body["items"]} == set(ids.values())
    assert all("name" in i for i in body["items"])   # full blob, not just columns


def test_filter_by_status_type_owner_product(client, team, admin_headers):
    _seed(client, admin_headers)
    assert client.get("/api/items?status=Planned", headers=admin_headers).json()["total"] == 2
    assert client.get("/api/items?type=Feature", headers=admin_headers).json()["total"] == 1
    assert client.get("/api/items?owner=PodA", headers=admin_headers).json()["total"] == 2
    assert client.get("/api/items?product=HubSpot", headers=admin_headers).json()["total"] == 1
    # combined filters AND together
    assert client.get("/api/items?owner=PodA&type=Enhancement",
                      headers=admin_headers).json()["total"] == 1


def test_filter_multi_value_is_or(client, team, admin_headers):
    # Comma-separated values expand to SQL IN(...) — the List view's top-bar
    # multi-select filters rely on this (any-of within a filter).
    _seed(client, admin_headers)
    # two statuses → matches items in either
    assert client.get("/api/items?status=Planned,In Progress",
                      headers=admin_headers).json()["total"] == 3
    # two owners, one of which has no items → still just PodA's two
    assert client.get("/api/items?owner=PodA,Nobody",
                      headers=admin_headers).json()["total"] == 2
    # multi-value across two filters still ANDs the filters together
    assert client.get("/api/items?product=Fraznet,HubSpot&type=Task",
                      headers=admin_headers).json()["total"] == 1


def test_search_q(client, team, admin_headers):
    _seed(client, admin_headers)
    r = client.get("/api/items?q=OIDC", headers=admin_headers).json()
    assert r["total"] == 1
    assert r["items"][0]["name"] == "Wire OIDC"


def test_search_multi_term_is_and(client, team, admin_headers):
    _seed(client, admin_headers)
    # both terms in "Login revamp" -> match; only one term -> still that one item
    assert client.get("/api/items?q=login+revamp", headers=admin_headers).json()["total"] == 1
    # a term that matches nothing in name/key/description -> no results
    assert client.get("/api/items?q=login+nonexistentword",
                      headers=admin_headers).json()["total"] == 0


def test_search_scoped_to_text_fields(client, team, admin_headers):
    _seed(client, admin_headers)
    # owner (dev) is NOT a searchable text field — q must not match it (precision).
    assert client.get("/api/items?q=PodB", headers=admin_headers).json()["total"] == 0
    # but the dedicated owner filter still works
    assert client.get("/api/items?owner=PodB", headers=admin_headers).json()["total"] == 1


def test_search_matches_description(client, team, admin_headers):
    pid = client.post("/api/projects", json={"name": "Item X", "status": "Planned",
                                             "description": "needs the widget pipeline"},
                      headers=admin_headers).json()["id"]
    r = client.get("/api/items?q=widget", headers=admin_headers).json()
    assert pid in {i["id"] for i in r["items"]}


def test_pagination(client, team, admin_headers):
    _seed(client, admin_headers)
    p1 = client.get("/api/items?page=1&page_size=2&sort=id:asc", headers=admin_headers).json()
    assert len(p1["items"]) == 2 and p1["total"] == 3 and p1["pages"] == 2
    p2 = client.get("/api/items?page=2&page_size=2&sort=id:asc", headers=admin_headers).json()
    assert len(p2["items"]) == 1
    # no overlap between pages
    assert not ({i["id"] for i in p1["items"]} & {i["id"] for i in p2["items"]})


def test_parent_filter(client, team, admin_headers):
    parent = _mk(client, admin_headers, name="Parent", type="Feature")
    child = _mk(client, admin_headers, name="Child", type="Task", parent=parent)
    by_parent = client.get(f"/api/items?parent_id={parent}", headers=admin_headers).json()
    assert {i["id"] for i in by_parent["items"]} == {child}
    top = client.get("/api/items?parent_id=none", headers=admin_headers).json()
    assert parent in {i["id"] for i in top["items"]}
    assert child not in {i["id"] for i in top["items"]}


def test_archived_filtering(client, team, admin_headers):
    keep = _mk(client, admin_headers, name="Keep")
    gone = _mk(client, admin_headers, name="Archive me")
    client.put(f"/api/projects/{gone}", json={"name": "Archive me", "status": "Planned",
                                              "archived": True}, headers=admin_headers)
    default = client.get("/api/items", headers=admin_headers).json()
    assert gone not in {i["id"] for i in default["items"]}   # archived hidden by default
    assert keep in {i["id"] for i in default["items"]}
    only = client.get("/api/items?archived=1", headers=admin_headers).json()
    assert {i["id"] for i in only["items"]} == {gone}
    both = client.get("/api/items?archived=all", headers=admin_headers).json()
    assert both["total"] == 2


def test_bad_parent_id_rejected(client, team, admin_headers):
    assert client.get("/api/items?parent_id=abc", headers=admin_headers).status_code == 400


def test_viewer_can_read(client, team, viewer_headers, admin_headers):
    _mk(client, admin_headers, name="Visible")
    assert client.get("/api/items", headers=viewer_headers).status_code == 200


def test_child_counts(client, team, admin_headers):
    parent = _mk(client, admin_headers, name="Feature", type="Feature")
    _mk(client, admin_headers, name="Task 1", parent=parent)
    _mk(client, admin_headers, name="Task 2", parent=parent)
    leaf = _mk(client, admin_headers, name="Lonely")
    # counts only present when requested
    plain = client.get("/api/items", headers=admin_headers).json()["items"][0]
    assert "_childCount" not in plain
    # top-level with counts → parent shows 2 children, leaf shows 0
    top = client.get("/api/items?parent_id=none&counts=1", headers=admin_headers).json()
    by_id = {i["id"]: i for i in top["items"]}
    assert by_id[parent]["_childCount"] == 2
    assert by_id[leaf]["_childCount"] == 0


def test_sort_by_name(client, team, admin_headers):
    # `name` lives in the JSON blob — sortable via json_extract, case-insensitive.
    _mk(client, admin_headers, name="banana")
    _mk(client, admin_headers, name="Apple")
    _mk(client, admin_headers, name="cherry")
    asc = [i["name"] for i in
           client.get("/api/items?sort=name:asc", headers=admin_headers).json()["items"]]
    assert asc == ["Apple", "banana", "cherry"]   # NOCASE: Apple before banana
    desc = [i["name"] for i in
            client.get("/api/items?sort=name:desc", headers=admin_headers).json()["items"]]
    assert desc == ["cherry", "banana", "Apple"]


def test_sort_by_indexed_column(client, team, admin_headers):
    ids = _seed(client, admin_headers)
    r = client.get("/api/items?sort=type:asc", headers=admin_headers).json()
    # sorting by an indexed column returns all rows without error
    assert {i["id"] for i in r["items"]} == set(ids.values())


def test_sort_whitelist_ignores_garbage(client, team, admin_headers):
    _seed(client, admin_headers)
    # A non-whitelisted sort column must not error or inject — falls back to default.
    r = client.get("/api/items?sort=data;DROP", headers=admin_headers)
    assert r.status_code == 200 and r.json()["total"] == 3
