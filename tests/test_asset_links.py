"""FLOW-1: asset linking + context, offline.

assetLinks (authoritative) and assetCache (display) are server-owned blob fields; item_assets is
a reverse-lookup index DERIVED from assetLinks on every save. AssetHub calls are permitted only
on a picker search and an explicit refresh - never on link (cached from the search result),
unlink, item render, or list render. All AssetHub interaction is mocked; AssetHub is real and
the live path was proven at the FLOW-0 enablement gate.
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


def _enable(team, monkeypatch):
    monkeypatch.setenv("ASSETHUB_API_KEY_" + team.upper(), KEY)
    _set_cfg(team, "assethubConnection", {"providerEnvironment": "production", "assethubTeam": "IT"})


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


def _install_client(monkeypatch, handler=None, forbid=False):
    """Replace server.AssetHubClient. forbid=True asserts it is never even constructed (proves a
    render path made no call). Otherwise .get(path,params) delegates to handler and records calls."""
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


def _link(client, headers, pid, asset, role="related"):
    return client.post(f"/api/items/{pid}/asset-links", json={"asset": asset, "role": role}, headers=headers)


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
    _enable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid, _asset())
    client.put(f"/api/projects/{pid}", json={"name": "Renamed", "status": "Planned"}, headers=admin_headers)
    stored = _stored(team, pid)
    assert [l["publicId"] for l in stored["assetLinks"]] == [UUID1]
    assert UUID1 in stored["assetCache"]


def test_stripped_on_create(client, team, admin_headers):
    r = _create(client, admin_headers, assetLinks=[{"publicId": UUID1}], assetCache={UUID1: {}})
    stored = _stored(team, r.json()["id"])
    assert "assetLinks" not in stored and "assetCache" not in stored


def test_survive_two_save_reload_cycles(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid, _asset())
    for i in range(2):
        client.put(f"/api/projects/{pid}", json={"name": f"E{i}", "status": "Planned"}, headers=admin_headers)
    all_ = client.get("/api/all", headers=admin_headers).json()
    item = next(p for p in all_["projects"] if p["id"] == pid)
    assert [l["publicId"] for l in item["assetLinks"]] == [UUID1]
    assert item["assetCache"][UUID1]["state"] == "ok"


def test_recurrence_does_not_inherit(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    pid = _create(client, admin_headers, recurrence="weekly", start="2026-01-01", dueWeeks=2).json()["id"]
    _link(client, admin_headers, pid, _asset())
    child = client.post(f"/api/projects/{pid}/recur", json={}, headers=admin_headers).json()
    cs = _stored(team, child["id"])
    assert "assetLinks" not in cs and "assetCache" not in cs
    assert _rows(team, child["id"]) == []


def test_recurrence_invariant_holds_with_asset_fields():
    unhandled = [f for f in server.SERVER_OWNED_FIELDS
                 if f not in server.RECURRENCE_SKIP_KEYS and f not in server.RECURRENCE_INHERITED]
    assert not unhandled                                   # FLOW-0-era invariant still satisfied
    assert "assetLinks" in server.RECURRENCE_SKIP_KEYS and "assetCache" in server.RECURRENCE_SKIP_KEYS


# ── item_assets reverse-lookup index ─────────────────────────────────────────────

def test_item_assets_resyncs_on_link_unlink_relink(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid, _asset(UUID1, "IT-1"), role="primary")
    _link(client, admin_headers, pid, _asset(UUID2, "IT-2"))
    assert [r["asset_public_id"] for r in _rows(team, pid)] == sorted([UUID1, UUID2])
    client.request("DELETE", f"/api/items/{pid}/asset-links/{UUID1}", headers=admin_headers)
    assert [r["asset_public_id"] for r in _rows(team, pid)] == [UUID2]
    _link(client, admin_headers, pid, _asset(UUID1, "IT-1"))         # relink
    assert [r["asset_public_id"] for r in _rows(team, pid)] == sorted([UUID1, UUID2])
    # table matches the blob exactly
    blob_ids = sorted(l["publicId"] for l in _stored(team, pid)["assetLinks"])
    assert [r["asset_public_id"] for r in _rows(team, pid)] == blob_ids


def test_delete_item_removes_item_assets_rows(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid, _asset())
    assert _rows(team, pid)
    client.request("DELETE", f"/api/projects/{pid}", headers=admin_headers)
    assert _rows(team, pid) == []


def test_asset_public_id_stored_is_the_uuid(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid, _asset(UUID1, "SOME-TAG-not-id"))
    rows = _rows(team, pid)
    assert rows[0]["asset_public_id"] == UUID1                       # the UUID, never the tag


# ── link rules ───────────────────────────────────────────────────────────────────

def test_duplicate_link_rejected(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    assert _link(client, admin_headers, pid, _asset()).status_code == 200
    assert _link(client, admin_headers, pid, _asset()).status_code == 409


def test_cap_of_ten_enforced(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    for i in range(10):
        uid = f"{i:08d}-1111-4111-8111-111111111111"
        assert _link(client, admin_headers, pid, _asset(uid, f"IT-{i}")).status_code == 200
    over = _link(client, admin_headers, pid, _asset("99999999-1111-4111-8111-111111111111", "IT-x"))
    assert over.status_code == 422


def test_link_makes_zero_assethub_calls(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    calls = _install_client(monkeypatch, forbid=True)   # constructing the client at all would fail
    pid = _create(client, admin_headers).json()["id"]
    assert _link(client, admin_headers, pid, _asset()).status_code == 200
    assert calls == []                                  # cached from the search result, no re-fetch


# ── no AssetHub call on any render path ──────────────────────────────────────────

def test_render_paths_make_no_assethub_call(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid, _asset())
    _install_client(monkeypatch, forbid=True)           # any construction now raises
    assert client.get("/api/all", headers=admin_headers).status_code == 200
    assert client.get("/api/items", headers=admin_headers).status_code == 200
    assert client.post(f"/api/items/{pid}/view", json={}, headers=admin_headers).status_code == 200


# ── search ───────────────────────────────────────────────────────────────────────

def test_search_empty_query_makes_no_call(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    calls = _install_client(monkeypatch, forbid=True)
    r = client.get("/api/assethub/assets/search?q=%20%20", headers=admin_headers)
    assert r.status_code == 200 and r.json()["results"] == []
    assert calls == []


def test_search_calls_once_with_q(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    calls = _install_client(monkeypatch, handler=lambda path, params: {"data": [_asset()]})
    r = client.get("/api/assethub/assets/search?q=lap", headers=admin_headers)
    assert r.status_code == 200 and len(r.json()["results"]) == 1
    assert len(calls) == 1 and calls[0]["params"]["q"] == "lap"


# ── refresh ────────────────────────────────────────────────────────────────────

def test_refresh_one_call_per_linked_asset(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid, _asset(UUID1, "IT-1"))
    _link(client, admin_headers, pid, _asset(UUID2, "IT-2"))
    calls = _install_client(monkeypatch, handler=lambda path, params: {"data": _asset(path.rsplit("/", 1)[-1])})
    r = client.post(f"/api/items/{pid}/asset-links/refresh", json={}, headers=admin_headers)
    assert r.status_code == 200
    assert len(calls) == 2                              # exactly one per linked asset


def test_refresh_404_keeps_link_and_sets_not_found(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid, _asset())
    def h(path, params):
        raise server.AssetHubError("not_found", status=404, correlation_id="c1")
    _install_client(monkeypatch, handler=h)
    r = client.post(f"/api/items/{pid}/asset-links/refresh", json={}, headers=admin_headers).json()
    assert r["assetCache"][UUID1]["state"] == "not_found"
    assert [l["publicId"] for l in _stored(team, pid)["assetLinks"]] == [UUID1]   # link NOT dropped


def test_refresh_500_sets_error(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid, _asset())
    def h(path, params):
        raise server.AssetHubError("internal_error", status=500, correlation_id="c1")
    _install_client(monkeypatch, handler=h)
    r = client.post(f"/api/items/{pid}/asset-links/refresh", json={}, headers=admin_headers).json()
    assert r["assetCache"][UUID1]["state"] == "error"


def test_refresh_401_surfaces_connection_error_and_stops(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    _link(client, admin_headers, pid, _asset(UUID1, "IT-1"))
    _link(client, admin_headers, pid, _asset(UUID2, "IT-2"))
    def h(path, params):
        raise server.AssetHubError("invalid_credential", status=401, correlation_id="c1")
    calls = _install_client(monkeypatch, handler=h)
    r = client.post(f"/api/items/{pid}/asset-links/refresh", json={}, headers=admin_headers).json()
    assert r["connectionError"]["code"] == "invalid_credential"
    assert len(calls) == 1                              # stopped after the connection-wide failure


# ── gating ─────────────────────────────────────────────────────────────────────

def test_not_configured_refuses_link(client, team, admin_headers, monkeypatch):
    monkeypatch.delenv("ASSETHUB_API_KEY_" + team.upper(), raising=False)   # no credential
    _set_cfg(team, "assethubConnection", {})
    pid = _create(client, admin_headers).json()["id"]
    assert _link(client, admin_headers, pid, _asset()).status_code == 400


def test_hidden_item_refuses_link(client, team, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    pid = _create(client, admin_headers, hidden=True).json()["id"]
    assert _link(client, admin_headers, pid, _asset()).status_code == 400


def test_viewer_cannot_link(client, team, viewer_headers, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    assert _link(client, viewer_headers, pid, _asset()).status_code in (401, 403)


def test_contributor_cannot_link(client, team, contributor_headers, admin_headers, monkeypatch):
    _enable(team, monkeypatch)
    pid = _create(client, admin_headers).json()["id"]
    assert _link(client, contributor_headers, pid, _asset()).status_code in (401, 403)
