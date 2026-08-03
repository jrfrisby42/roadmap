"""FLOW-1: asset linking + context, offline.

assetLinks (authoritative) and assetCache (display) are server-owned blob fields; item_assets is
a reverse-lookup index DERIVED from assetLinks on every save. AssetHub calls are permitted only
on a picker search, an explicit refresh, and a LINK (5.6.1: the server fetches the asset by
public id and caches THAT - the client no longer supplies asset content into a server-owned
field). Never on item render or list render. All AssetHub interaction is mocked.
"""
import json

import pytest

import server

KEY = "ahk_aaaaaaaaaaaaaaaa_" + "b" * 52
UUID1 = "11111111-1111-4111-8111-111111111111"
UUID2 = "22222222-2222-4222-8222-222222222222"


def _set_cfg(team, key, val):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(val)))


def _create(client, headers, **fields):
    return client.post("/api/projects", json={"name": "Item", "status": "Planned", **fields}, headers=headers)


def _stored(team, pid):
    with server.db(team) as c:
        row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
    return json.loads(row["data"]) if row else None


def _rows(team, pid):
    with server.db(team) as c:
        return [dict(r) for r in c.execute(
            "SELECT asset_public_id, role FROM item_assets WHERE item_id=? ORDER BY asset_public_id", (pid,)).fetchall()]


def _asset(uid=UUID1, tag="IT-1"):
    return {"id": uid, "asset_tag": tag, "name": "Dev Laptop", "serial_number": "S1",
            "category": {"id": "cat-1", "code": "IT", "name": "IT/Tech"},
            "status": {"value": "in_stock", "label": "In Stock"},
            "operational_condition": {"value": None, "label": "Not tracked", "tracking": "not_tracked"},
            "location": None, "warranty_until": None, "replacement_due": None,
            "retirement": {"retired": False}, "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z"}


def _enable(team, monkeypatch):
    monkeypatch.setenv("ASSETHUB_API_KEY_" + team.upper(), KEY)
    _set_cfg(team, "assethubConnection", {"providerEnvironment": "production", "assethubTeam": "IT"})


def _install(monkeypatch, handler=None, forbid=False):
    """Replace server.AssetHubClient. forbid=True asserts it is never even constructed. Otherwise
    .get(path,params) delegates to handler and records calls."""
    calls = []
    class Fake:
        def __init__(self, team, role, **kw):
            if forbid:
                raise AssertionError("AssetHubClient constructed on a path that must make no call")
        def get(self, path, params=None):
            calls.append({"path": path, "params": params})
            return handler(path, params)
    monkeypatch.setattr(server, "AssetHubClient", Fake)
    return calls


def _detail_ok(path, params):
    """Default detail handler: synthesize an AssetResponse from the requested public id."""
    pubid = path.rsplit("/", 1)[-1]
    return {"data": _asset(pubid, "TAG-" + pubid[:4]), "correlation_id": "c"}


def _enable_linkable(team, monkeypatch, handler=_detail_ok):
    """Enable the team AND install a client whose detail fetch succeeds, so link() works."""
    _enable(team, monkeypatch)
    return _install(monkeypatch, handler=handler)


def _link(client, headers, pid, pubid=UUID1, role="related", extra=None):
    body = {"publicId": pubid, "role": role}
    if extra:
        body.update(extra)
    return client.post(f"/api/items/{pid}/asset-links", json=body, headers=headers)


# ── server-owned blob fields ─────────────────────────────────────────────────────

def test_client_put_cannot_forge_asset_fields(client, team, admin_headers):
    pid = _create(client, admin_headers).json()["id"]
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "Planned",
                         "assetLinks": [{"publicId": UUID1, "role": "primary"}],
                         "assetCache": {UUID1: {"asset": {"id": UUID1}}}},
                   headers=admin_headers)
    assert r.status_code == 200
    stored = _stored(team, pid)
    assert "assetLinks" not in stored and "assetCache" not in stored


def test_put_omitting_does_not_wipe(client, team, admin_headers, monkeypatch):
    _enable_linkable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid)
    client.put(f"/api/projects/{pid}", json={"name": "Renamed", "status": "Planned"}, headers=admin_headers)
    stored = _stored(team, pid)
    assert [l["publicId"] for l in stored["assetLinks"]] == [UUID1]
    assert UUID1 in stored["assetCache"]


def test_stripped_on_create(client, team, admin_headers):
    r = _create(client, admin_headers, assetLinks=[{"publicId": UUID1}], assetCache={UUID1: {}})
    stored = _stored(team, r.json()["id"])
    assert "assetLinks" not in stored and "assetCache" not in stored


def test_survive_two_save_reload_cycles(client, team, admin_headers, monkeypatch):
    _enable_linkable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid)
    for i in range(2):
        client.put(f"/api/projects/{pid}", json={"name": f"E{i}", "status": "Planned"}, headers=admin_headers)
    all_ = client.get("/api/all", headers=admin_headers).json()
    item = next(p for p in all_["projects"] if p["id"] == pid)
    assert [l["publicId"] for l in item["assetLinks"]] == [UUID1]
    assert item["assetCache"][UUID1]["state"] == "ok"


def test_recurrence_does_not_inherit(client, team, admin_headers, monkeypatch):
    _enable_linkable(team, monkeypatch)
    pid = _create(client, admin_headers, recurrence="weekly", start="2026-01-01", dueWeeks=2).json()["id"]
    _link(client, admin_headers, pid)
    child = client.post(f"/api/projects/{pid}/recur", json={}, headers=admin_headers).json()
    cs = _stored(team, child["id"])
    assert "assetLinks" not in cs and "assetCache" not in cs
    assert _rows(team, child["id"]) == []


def test_recurrence_invariant_holds_with_asset_fields():
    unhandled = [f for f in server.SERVER_OWNED_FIELDS
                 if f not in server.RECURRENCE_SKIP_KEYS and f not in server.RECURRENCE_INHERITED]
    assert not unhandled
    assert "assetLinks" in server.RECURRENCE_SKIP_KEYS and "assetCache" in server.RECURRENCE_SKIP_KEYS


# ── item_assets reverse-lookup index ─────────────────────────────────────────────

def test_item_assets_resyncs_on_link_unlink_relink(client, team, admin_headers, monkeypatch):
    _enable_linkable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid, UUID1, role="primary")
    _link(client, admin_headers, pid, UUID2)
    assert [r["asset_public_id"] for r in _rows(team, pid)] == sorted([UUID1, UUID2])
    client.delete(f"/api/items/{pid}/asset-links/{UUID1}", headers=admin_headers)
    assert [r["asset_public_id"] for r in _rows(team, pid)] == [UUID2]
    _link(client, admin_headers, pid, UUID1)                        # relink
    blob_ids = sorted(l["publicId"] for l in _stored(team, pid)["assetLinks"])
    assert [r["asset_public_id"] for r in _rows(team, pid)] == blob_ids


def test_delete_item_removes_item_assets_rows(client, team, admin_headers, monkeypatch):
    _enable_linkable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid)
    assert _rows(team, pid)
    client.delete(f"/api/projects/{pid}", headers=admin_headers)
    assert _rows(team, pid) == []


def test_asset_public_id_stored_is_the_uuid(client, team, admin_headers, monkeypatch):
    _enable_linkable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid, UUID1)
    assert _rows(team, pid)[0]["asset_public_id"] == UUID1          # the UUID, never the tag


# ── link rules ───────────────────────────────────────────────────────────────────

def test_duplicate_link_rejected(client, team, admin_headers, monkeypatch):
    _enable_linkable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    assert _link(client, admin_headers, pid).status_code == 200
    assert _link(client, admin_headers, pid).status_code == 409


def test_cap_of_ten_enforced(client, team, admin_headers, monkeypatch):
    _enable_linkable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    for i in range(10):
        assert _link(client, admin_headers, pid, f"{i:08d}-1111-4111-8111-111111111111").status_code == 200
    assert _link(client, admin_headers, pid, "99999999-1111-4111-8111-111111111111").status_code == 422


# ── Item 1 (5.6.1): the SERVER fetches; the client cannot inject cache content ───

def test_link_makes_exactly_one_assethub_call(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    calls = _install(monkeypatch, handler=_detail_ok)
    pid = _create(client, admin_headers).json()["id"]
    assert _link(client, admin_headers, pid).status_code == 200
    assert len(calls) == 1                                          # one detail fetch, not zero


def test_link_ignores_client_asset_and_caches_server_fetch(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    # server fetch returns the REAL asset; the request also carries a FABRICATED asset payload.
    _install(monkeypatch, handler=lambda path, params: {"data": _asset(UUID1, "REAL-TAG")})
    pid = _create(client, admin_headers).json()["id"]
    fabricated = _asset(UUID1, "FAKE-TAG"); fabricated["name"] = "Fabricated Name"
    r = _link(client, admin_headers, pid, UUID1, extra={"asset": fabricated})
    assert r.status_code == 200
    cached = _stored(team, pid)["assetCache"][UUID1]["asset"]
    assert cached["asset_tag"] == "REAL-TAG"                        # from the server fetch...
    assert cached["name"] == "Dev Laptop"                          # ...not the fabricated payload


def test_link_forged_uuid_404_creates_no_link(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    _install(monkeypatch, handler=_raise("not_found", 404))
    pid = _create(client, admin_headers).json()["id"]
    r = _link(client, admin_headers, pid, "12345678-1111-4111-8111-111111111111")
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["error"]["code"] == "not_found"
    assert not (_stored(team, pid).get("assetLinks"))              # no link created
    assert _rows(team, pid) == []


def test_link_malformed_uuid_rejected_before_any_call(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    _install(monkeypatch, forbid=True)                             # constructing the client would fail
    pid = _create(client, admin_headers).json()["id"]
    assert _link(client, admin_headers, pid, "not-a-uuid").status_code == 422


def _raise(code, status):
    def h(path, params):
        raise server.AssetHubError(code, status=status, correlation_id="c1")
    return h


@pytest.mark.parametrize("code,status", [("internal_error", 500), ("invalid_credential", 401),
                                         ("connection_inactive", 403), ("unreachable", None)])
def test_link_error_cases_create_no_link(client, team, admin_headers, monkeypatch, code, status):
    _enable(team, monkeypatch)
    _install(monkeypatch, handler=_raise(code, status))
    pid = _create(client, admin_headers).json()["id"]
    r = _link(client, admin_headers, pid).json()
    assert r["ok"] is False and r["error"]["code"] == code
    assert not (_stored(team, pid).get("assetLinks"))


# ── no AssetHub call on any render path ──────────────────────────────────────────

def test_render_paths_make_no_assethub_call(client, team, admin_headers, monkeypatch):
    _enable_linkable(team, monkeypatch)                            # ok stub for the setup link
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid)
    _install(monkeypatch, forbid=True)                            # now any construction raises
    assert client.get("/api/all", headers=admin_headers).status_code == 200
    assert client.get("/api/items", headers=admin_headers).status_code == 200
    assert client.post(f"/api/items/{pid}/view", json={}, headers=admin_headers).status_code == 200


# ── search ───────────────────────────────────────────────────────────────────────

def test_search_empty_query_makes_no_call(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    _install(monkeypatch, forbid=True)
    r = client.get("/api/assethub/assets/search?q=%20%20", headers=admin_headers)
    assert r.status_code == 200 and r.json()["results"] == []


def test_search_calls_once_with_q(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    calls = _install(monkeypatch, handler=lambda path, params: {"data": [_asset()]})
    r = client.get("/api/assethub/assets/search?q=lap", headers=admin_headers)
    assert r.status_code == 200 and len(r.json()["results"]) == 1
    assert len(calls) == 1 and calls[0]["params"]["q"] == "lap"


# ── refresh ────────────────────────────────────────────────────────────────────

def test_refresh_one_call_per_linked_asset(client, team, admin_headers, monkeypatch):
    _enable_linkable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid, UUID1)
    _link(client, admin_headers, pid, UUID2)
    calls = _install(monkeypatch, handler=_detail_ok)             # fresh counter for refresh only
    r = client.post(f"/api/items/{pid}/asset-links/refresh", json={}, headers=admin_headers)
    assert r.status_code == 200 and len(calls) == 2               # exactly one per linked asset


def test_refresh_404_keeps_link_and_sets_not_found(client, team, admin_headers, monkeypatch):
    _enable_linkable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid)
    _install(monkeypatch, handler=_raise("not_found", 404))
    r = client.post(f"/api/items/{pid}/asset-links/refresh", json={}, headers=admin_headers).json()
    assert r["assetCache"][UUID1]["state"] == "not_found"
    assert [l["publicId"] for l in _stored(team, pid)["assetLinks"]] == [UUID1]   # link NOT dropped


def test_refresh_500_sets_error(client, team, admin_headers, monkeypatch):
    _enable_linkable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid)
    _install(monkeypatch, handler=_raise("internal_error", 500))
    r = client.post(f"/api/items/{pid}/asset-links/refresh", json={}, headers=admin_headers).json()
    assert r["assetCache"][UUID1]["state"] == "error"


def test_refresh_401_surfaces_connection_error_and_stops(client, team, admin_headers, monkeypatch):
    _enable_linkable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid, UUID1)
    _link(client, admin_headers, pid, UUID2)
    calls = _install(monkeypatch, handler=_raise("invalid_credential", 401))
    r = client.post(f"/api/items/{pid}/asset-links/refresh", json={}, headers=admin_headers).json()
    assert r["connectionError"]["code"] == "invalid_credential"
    assert len(calls) == 1                                        # stopped after the connection-wide failure


# ── gating ─────────────────────────────────────────────────────────────────────

def test_not_configured_refuses_link(client, team, admin_headers, monkeypatch):
    monkeypatch.delenv("ASSETHUB_API_KEY_" + team.upper(), raising=False)
    _set_cfg(team, "assethubConnection", {})
    pid = _create(client, admin_headers).json()["id"]
    assert _link(client, admin_headers, pid).status_code == 400


def test_hidden_item_refuses_link(client, team, admin_headers, monkeypatch):
    _enable_linkable(team, monkeypatch)
    pid = _create(client, admin_headers, hidden=True).json()["id"]
    assert _link(client, admin_headers, pid).status_code == 400


def test_viewer_cannot_link(client, team, viewer_headers, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    assert _link(client, viewer_headers, pid).status_code in (401, 403)


def test_contributor_cannot_link(client, team, contributor_headers, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    assert _link(client, contributor_headers, pid).status_code in (401, 403)


# ── reverse lookup: which tickets touched asset X (5.12.x) ───────────────────────
# GET /api/assethub/assets/{public_id}/items reads the DERIVED item_assets index (no blob scan,
# NO AssetHub call). Returns a lightweight row per linked item + the asset's cached display.

def _revlookup(client, headers, pubid=UUID1):
    return client.get(f"/api/assethub/assets/{pubid}/items", headers=headers)


def test_reverse_lookup_lists_linked_items(client, team, admin_headers, monkeypatch):
    _enable_linkable(team, monkeypatch)
    a = _create(client, admin_headers, name="A").json()["id"]
    b = _create(client, admin_headers, name="B").json()["id"]
    _link(client, admin_headers, a)                 # both -> UUID1
    _link(client, admin_headers, b)
    other = _create(client, admin_headers, name="C").json()["id"]
    _link(client, admin_headers, other, pubid=UUID2)   # a different asset, must not appear
    r = _revlookup(client, admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    ids = {it["id"] for it in body["items"]}
    assert ids == {a, b}
    # asset display comes from the cached AssetResponse (TAG-1111), no AssetHub call needed here
    assert body["asset"]["tag"] == "TAG-" + UUID1[:4]
    assert body["asset"]["publicId"] == UUID1


def test_reverse_lookup_orders_open_first_then_newest(client, team, admin_headers, monkeypatch):
    _set_cfg(team, "statusIsTerminal", {"Done": True})
    _enable_linkable(team, monkeypatch)
    old_open = _create(client, admin_headers, name="old open", status="Planned").json()["id"]
    new_open = _create(client, admin_headers, name="new open", status="Planned").json()["id"]
    done = _create(client, admin_headers, name="done", status="Done").json()["id"]
    for pid in (old_open, new_open, done):
        _link(client, admin_headers, pid)
    items = _revlookup(client, admin_headers).json()["items"]
    order = [it["id"] for it in items]
    # open group first (newest-created first within it), terminal last
    assert order == [new_open, old_open, done]
    assert [it["open"] for it in items] == [True, True, False]


def test_reverse_lookup_excludes_hidden_and_archived(client, team, admin_headers, monkeypatch):
    _enable_linkable(team, monkeypatch)
    visible = _create(client, admin_headers, name="visible").json()["id"]
    _link(client, admin_headers, visible)
    # A hidden item cannot be linked via the endpoint (guarded), so seed the link + index directly.
    hidden = _create(client, admin_headers, name="hidden").json()["id"]
    with server.db(team) as c:
        row = c.execute("SELECT data FROM projects WHERE id=?", (hidden,)).fetchone()
        p = json.loads(row["data"]); p["hidden"] = True
        p["assetLinks"] = [{"publicId": UUID1, "role": "related", "linkedAt": "", "linkedBy": ""}]
        server._save_project(c, hidden, p)          # re-syncs item_assets even for a hidden item
    body = _revlookup(client, admin_headers).json()
    assert body["count"] == 1
    assert body["items"][0]["id"] == visible


def test_reverse_lookup_bad_uuid_422(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    assert client.get("/api/assethub/assets/not-a-uuid/items", headers=admin_headers).status_code == 422


def test_reverse_lookup_requires_assethub_config(client, team, admin_headers):
    # Team not enabled -> 400 (mirrors the other asset endpoints' gate).
    assert _revlookup(client, admin_headers).status_code == 400


def test_reverse_lookup_role_gated(client, team, viewer_headers, monkeypatch):
    _enable(team, monkeypatch)
    assert _revlookup(client, viewer_headers).status_code in (401, 403)


def test_reverse_lookup_makes_no_assethub_call(client, team, admin_headers, monkeypatch):
    _enable_linkable(team, monkeypatch)
    pid = _create(client, admin_headers, name="A").json()["id"]
    _link(client, admin_headers, pid)
    # Re-install a client that BLOWS UP if constructed: the reverse lookup must not touch AssetHub.
    _install(monkeypatch, forbid=True)
    r = _revlookup(client, admin_headers)
    assert r.status_code == 200
    assert r.json()["count"] == 1
