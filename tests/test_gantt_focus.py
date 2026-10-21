"""GANTT-FOCUS-1: the per-Org projectTypes config key (the Gantt's "Focus: Projects only" types)."""
import server

DEFAULT = ["Roadmap Item", "Epic", "Feature", "Project"]


def test_default_is_served_on_api_all(client, admin_headers):
    assert client.get("/api/all", headers=admin_headers).json()["projectTypes"] == DEFAULT


def test_admin_can_set_it_and_it_round_trips(client, admin_headers):
    r = client.put("/api/config/projectTypes", json=["Project"], headers=admin_headers)
    assert r.status_code == 200, r.text
    assert client.get("/api/all", headers=admin_headers).json()["projectTypes"] == ["Project"]


def test_editor_cannot_set_it(client, team):
    h = {"Authorization": f"Bearer {server.create_token(team, 'ed', 'editor')}", "X-Team": team}
    assert client.put("/api/config/projectTypes", json=["Epic"], headers=h).status_code == 403


def test_empty_list_falls_back_to_the_default(client, admin_headers):
    client.put("/api/config/projectTypes", json=[], headers=admin_headers)
    assert client.get("/api/all", headers=admin_headers).json()["projectTypes"] == DEFAULT
