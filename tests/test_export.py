"""Server-side complete backup export (GET /api/export).

Unlike the client-side "Quick Export" (in-memory only), this dumps every per-team
table so it's a faithful, restorable backup. Admin-only.
"""
import server


def test_export_requires_admin(client, team, admin_headers, editor_headers, viewer_headers):
    assert client.get("/api/export", headers=admin_headers).status_code == 200
    assert client.get("/api/export", headers=editor_headers).status_code == 403
    assert client.get("/api/export", headers=viewer_headers).status_code == 403


def test_export_shape_and_download_header(client, team, admin_headers):
    r = client.get("/api/export", headers=admin_headers)
    assert r.status_code == 200
    assert 'attachment; filename="' in r.headers.get("content-disposition", "")
    data = r.json()
    # every per-team table is represented
    for k in ("projects", "config", "comments", "activities", "audit_log",
              "capacity_overrides", "planning_sessions", "notifications",
              "watchers", "key_counters", "recent_views"):
        assert k in data, f"export missing {k}"
    assert data["_meta"]["team"] == team
    assert data["_meta"]["app_version"] == server.APP_VERSION


def test_export_includes_server_only_data(client, team, admin_headers):
    # A comment lives only server-side (the client Quick Export can't see it) — the
    # full backup must capture it.
    pid = client.post("/api/projects", json={"name": "Backup me", "status": "Planned"},
                      headers=admin_headers).json()["id"]
    client.post("/api/comments", json={"item_id": pid, "body": "keep this"}, headers=admin_headers)
    data = client.get("/api/export", headers=admin_headers).json()
    # project blob is parsed (not a raw JSON string)
    proj = next(p for p in data["projects"] if p["id"] == pid)
    assert proj["data"]["name"] == "Backup me"
    # the comment is present
    assert any(c.get("body") == "keep this" and c.get("item_id") == pid
               for c in data["comments"])
