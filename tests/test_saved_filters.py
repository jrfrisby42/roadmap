"""Per-user server-backed saved filters (Flow shell "SAVED FILTERS" rail).

Saved filters were localStorage-only (per-device); they are now persisted per team+user in
the ``saved_filters`` table so they follow a user across browsers. Endpoints:
``GET/PUT /api/saved-filters`` (any authed user, incl. viewers; a user only ever sees/writes
their OWN row).
"""
import server


_F1 = {"id": "s1", "name": "At Risk", "view": "list", "qs": "?status=Blocked"}
_F2 = {"id": "s2", "name": "My Gantt", "view": "gantt", "qs": "?owner=jr"}


def test_get_default_empty(client, admin_headers):
    r = client.get("/api/saved-filters", headers=admin_headers)
    assert r.status_code == 200
    assert r.json() == {"filters": []}


def test_put_then_get_roundtrip(client, admin_headers):
    r = client.put("/api/saved-filters", json={"filters": [_F1, _F2]}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["filters"] == [_F1, _F2]
    got = client.get("/api/saved-filters", headers=admin_headers).json()["filters"]
    assert got == [_F1, _F2]


def test_put_replaces_wholesale(client, admin_headers):
    client.put("/api/saved-filters", json={"filters": [_F1, _F2]}, headers=admin_headers)
    client.put("/api/saved-filters", json={"filters": [_F1]}, headers=admin_headers)
    assert client.get("/api/saved-filters", headers=admin_headers).json()["filters"] == [_F1]


def test_per_user_isolation(client, admin_headers, viewer_headers):
    client.put("/api/saved-filters", json={"filters": [_F1]}, headers=admin_headers)
    # A different user in the same team sees their OWN (empty) set, never the admin's.
    assert client.get("/api/saved-filters", headers=viewer_headers).json()["filters"] == []
    client.put("/api/saved-filters", json={"filters": [_F2]}, headers=viewer_headers)
    assert client.get("/api/saved-filters", headers=admin_headers).json()["filters"] == [_F1]
    assert client.get("/api/saved-filters", headers=viewer_headers).json()["filters"] == [_F2]


def test_viewer_may_save(client, viewer_headers):
    # Saved filters are personal UI state, not privileged config: a viewer can read/write.
    r = client.put("/api/saved-filters", json={"filters": [_F1]}, headers=viewer_headers)
    assert r.status_code == 200
    assert client.get("/api/saved-filters", headers=viewer_headers).json()["filters"] == [_F1]


def test_bare_list_body_accepted(client, admin_headers):
    r = client.put("/api/saved-filters", json=[_F1], headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["filters"] == [_F1]


def test_non_list_rejected(client, admin_headers):
    assert client.put("/api/saved-filters", json={"filters": "nope"}, headers=admin_headers).status_code == 422


def test_entries_without_name_dropped(client, admin_headers):
    bad = [{"id": "x", "view": "list"}, {"name": "  ", "view": "list"}, _F1]
    r = client.put("/api/saved-filters", json={"filters": bad}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["filters"] == [_F1]   # only the named entry survives


def test_too_many_rejected(client, admin_headers):
    many = [{"id": f"s{i}", "name": f"F{i}", "view": "list", "qs": ""} for i in range(server._SAVED_FILTERS_MAX + 1)]
    assert client.put("/api/saved-filters", json={"filters": many}, headers=admin_headers).status_code == 422


def test_requires_auth(client, team):
    assert client.get("/api/saved-filters", headers={"X-Team": team}).status_code == 401
    assert client.put("/api/saved-filters", json={"filters": []}, headers={"X-Team": team}).status_code == 401
