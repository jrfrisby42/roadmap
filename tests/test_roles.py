"""Server-side role gating. Never trust the frontend — every mutating route
must enforce its own role, so we assert the 403s here."""


def _make_item(client, headers, **fields):
    body = {"name": "Test Item", "status": "Planned", **fields}
    return client.post("/api/projects", json=body, headers=headers)


def test_create_project_role_gating(client, viewer_headers, editor_headers, admin_headers):
    assert _make_item(client, viewer_headers).status_code == 403
    assert _make_item(client, editor_headers).status_code == 200
    assert _make_item(client, admin_headers).status_code == 200


def test_delete_is_admin_only(client, team, editor_headers, admin_headers):
    created = _make_item(client, admin_headers)
    pid = created.json()["id"]
    # Editor cannot delete (admin-only route).
    assert client.delete(f"/api/projects/{pid}", headers=editor_headers).status_code == 403
    # Admin can.
    assert client.delete(f"/api/projects/{pid}", headers=admin_headers).status_code == 200


def test_config_is_admin_only(client, editor_headers, admin_headers):
    r_editor = client.put("/api/config/statusIsActive", json={"In Progress": True},
                          headers=editor_headers)
    assert r_editor.status_code == 403
    r_admin = client.put("/api/config/statusIsActive", json={"In Progress": True},
                         headers=admin_headers)
    assert r_admin.status_code == 200
