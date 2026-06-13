"""Feature B — Sprint entity. Additive /api/sprints endpoints (shared, single
Active). These assert the server contract + the one-Active constraint."""


def _sprint(**kw):
    s = {"id": "sp1", "name": "Sprint 1", "goal": "", "startDate": "2026-06-01",
         "endDate": "2026-06-15", "state": "Planned", "scope": "global", "carryOver": 0}
    s.update(kw)
    return s


def test_sprints_default_empty(client, team, admin_headers):
    r = client.get("/api/sprints", headers=admin_headers)
    assert r.status_code == 200 and r.json()["sprints"] == []


def test_sprints_round_trip(client, team, admin_headers):
    payload = {"sprints": [_sprint(), _sprint(id="sp2", name="Sprint 2", state="Active")]}
    assert client.put("/api/sprints", json=payload, headers=admin_headers).status_code == 200
    got = client.get("/api/sprints", headers=admin_headers).json()["sprints"]
    assert [s["name"] for s in got] == ["Sprint 1", "Sprint 2"]
    assert got[1]["state"] == "Active"


def test_only_one_active_sprint(client, team, admin_headers):
    bad = {"sprints": [_sprint(id="a", state="Active"), _sprint(id="b", state="Active")]}
    r = client.put("/api/sprints", json=bad, headers=admin_headers)
    assert r.status_code == 422 and "one sprint" in r.json()["detail"]


def test_sprint_state_validated(client, team, admin_headers):
    assert client.put("/api/sprints", json={"sprints": [_sprint(state="Bogus")]},
                      headers=admin_headers).status_code == 422


def test_sprint_requires_id_and_name(client, team, admin_headers):
    assert client.put("/api/sprints", json={"sprints": [_sprint(name="")]},
                      headers=admin_headers).status_code == 422


def test_sprints_editable_by_any_authed_user(client, team, editor_headers, viewer_headers):
    assert client.put("/api/sprints", json={"sprints": [_sprint()]},
                      headers=editor_headers).status_code == 200
    assert client.get("/api/sprints", headers=viewer_headers).status_code == 200
