"""Feature C — Release entity. Additive /api/releases endpoints (shared). These
assert the server contract + state validation."""


def _rel(**kw):
    r = {"id": "rel1", "name": "v2.4", "targetDate": "2026-07-01", "state": "Unreleased"}
    r.update(kw)
    return r


def test_releases_default_empty(client, team, admin_headers):
    r = client.get("/api/releases", headers=admin_headers)
    assert r.status_code == 200 and r.json()["releases"] == []


def test_releases_round_trip(client, team, admin_headers):
    payload = {"releases": [_rel(), _rel(id="rel2", name="v2.5", state="Released", releasedDate="2026-06-20")]}
    assert client.put("/api/releases", json=payload, headers=admin_headers).status_code == 200
    got = client.get("/api/releases", headers=admin_headers).json()["releases"]
    assert [x["name"] for x in got] == ["v2.4", "v2.5"]
    assert got[1]["state"] == "Released" and got[1]["releasedDate"] == "2026-06-20"


def test_release_state_validated(client, team, admin_headers):
    assert client.put("/api/releases", json={"releases": [_rel(state="Shipping")]},
                      headers=admin_headers).status_code == 422


def test_release_requires_id_and_name(client, team, admin_headers):
    assert client.put("/api/releases", json={"releases": [_rel(name="")]},
                      headers=admin_headers).status_code == 422


def test_releases_editable_by_any_authed_user(client, team, editor_headers, viewer_headers):
    assert client.put("/api/releases", json={"releases": [_rel()]},
                      headers=editor_headers).status_code == 200
    assert client.get("/api/releases", headers=viewer_headers).status_code == 200
