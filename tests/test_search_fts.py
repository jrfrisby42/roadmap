"""FTS5 full-text search maintenance: the index stays in sync as items are
created, edited, and deleted (JIRA-REPLACEMENT.md §3.2). Falls back to LIKE if
fts5 is unavailable — these assert the search *behavior*, which holds either way."""
import server


def _mk(client, headers, **fields):
    return client.post("/api/projects", json={"status": "Planned", **fields}, headers=headers).json()["id"]


def _ids(client, headers, q):
    return {i["id"] for i in client.get(f"/api/items?q={q}", headers=headers).json()["items"]}


def test_fts_is_active_in_test_env():
    # Sanity: the environments we ship to have fts5 (see deploy checks).
    assert server._FTS_ENABLED is True


def test_search_finds_created_item(client, team, admin_headers):
    pid = _mk(client, admin_headers, name="Quantum uploader")
    assert pid in _ids(client, admin_headers, "quantum")


def test_search_reflects_rename(client, team, admin_headers):
    pid = _mk(client, admin_headers, name="Alpha widget")
    assert pid in _ids(client, admin_headers, "alpha")
    client.put(f"/api/projects/{pid}", json={"name": "Beta gadget", "status": "Planned"},
               headers=admin_headers)
    assert pid not in _ids(client, admin_headers, "alpha")   # old term no longer matches
    assert pid in _ids(client, admin_headers, "beta")        # new term matches (FTS synced on save)


def test_search_drops_deleted_item(client, team, admin_headers):
    pid = _mk(client, admin_headers, name="Ephemeral thing")
    assert pid in _ids(client, admin_headers, "ephemeral")
    client.delete(f"/api/projects/{pid}", headers=admin_headers)
    assert pid not in _ids(client, admin_headers, "ephemeral")   # removed from the index


def test_search_prefix_and_multiterm(client, team, admin_headers):
    pid = _mk(client, admin_headers, name="Payment reconciliation")
    # prefix matching
    assert pid in _ids(client, admin_headers, "recon")
    # multi-term AND
    assert pid in _ids(client, admin_headers, "payment+recon")
    assert pid not in _ids(client, admin_headers, "payment+nope")


def test_search_relevance_orders_name_match_first(client, team, admin_headers):
    # term in the name should rank above term only in description
    desc_only = _mk(client, admin_headers, name="Unrelated", description="mentions sparkle here")
    name_hit  = _mk(client, admin_headers, name="Sparkle engine")
    items = client.get("/api/items?q=sparkle", headers=admin_headers).json()["items"]
    ids = [i["id"] for i in items]
    assert set(ids) == {desc_only, name_hit}
    assert ids[0] == name_hit   # bm25: name match ranks first by default
