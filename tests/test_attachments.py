"""Stage 2a — Attachments (S3). The browser uploads directly to S3 via a
presigned PUT; the backend only signs, records metadata on the item blob, and
deletes. These assert the server contract: size guard, filename/key shaping,
auth gating, and the record/list/delete round-trip.

The presign + S3 happy-path (signature generation) needs live AWS credentials,
so it is verified against the deployed environment, not here. The size guard,
sanitiser, key shape, auth, and metadata round-trip need no S3 and are covered.
"""
import server


def _mk(client, headers):
    return client.post("/api/projects", json={"name": "Item", "status": "Planned"},
                       headers=headers).json()["id"]


# ── pure helpers (single source of truth for key/filename shaping) ────────────

def test_sanitize_filename_strips_unsafe_and_path():
    assert server._sanitize_filename("my report.pdf") == "my_report.pdf"
    assert server._sanitize_filename("../../etc/passwd") == "passwd"   # basename only
    assert server._sanitize_filename("c:\\temp\\a*b.png") == "a_b.png"
    assert server._sanitize_filename("..hidden") == "hidden"           # no leading dots
    assert server._sanitize_filename("") == "file"
    assert server._sanitize_filename("résumé.doc") == "r_sum_.doc"


def test_attachment_key_shape():
    # items/{itemId}/{uuid}/{sanitized} — uuid is its own path segment
    assert server._attachment_key(7, "abc123", "my file.png") == "items/7/abc123/my_file.png"


# ── presign: server-side size guard + auth ────────────────────────────────────

def test_presign_rejects_oversized(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    r = client.post(f"/api/items/{pid}/attachments/presign",
                    json={"filename": "big.zip", "contentType": "application/zip",
                          "size": 51 * 1024 * 1024},
                    headers=admin_headers)
    assert r.status_code == 413


def test_presign_requires_editor(client, team, admin_headers, viewer_headers):
    pid = _mk(client, admin_headers)
    r = client.post(f"/api/items/{pid}/attachments/presign",
                    json={"filename": "a.png", "contentType": "image/png", "size": 10},
                    headers=viewer_headers)
    assert r.status_code == 403   # role gate runs before any S3 call


# ── record / list / delete round-trip (no S3 needed) ──────────────────────────

def test_add_list_delete_roundtrip(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    rec = client.post(f"/api/items/{pid}/attachments",
                      json={"attId": "att1", "key": f"items/{pid}/att1/a.png",
                            "name": "a.png", "contentType": "image/png", "size": 1234},
                      headers=admin_headers)
    assert rec.status_code == 200 and rec.json()["name"] == "a.png"

    lst = client.get(f"/api/items/{pid}/attachments", headers=admin_headers).json()["attachments"]
    assert len(lst) == 1 and lst[0]["id"] == "att1" and lst[0]["name"] == "a.png"
    # url may be None here (no AWS creds in CI) — the endpoint still returns 200.
    assert "url" in lst[0]

    d = client.delete(f"/api/items/{pid}/attachments/att1", headers=admin_headers)
    assert d.status_code == 200 and d.json()["deleted"] == "att1"
    assert client.get(f"/api/items/{pid}/attachments", headers=admin_headers).json()["attachments"] == []


def test_add_requires_attid_and_key(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    assert client.post(f"/api/items/{pid}/attachments", json={"name": "a.png"},
                       headers=admin_headers).status_code == 422


def test_add_unknown_item_404(client, team, admin_headers):
    assert client.post("/api/items/999999/attachments",
                       json={"attId": "a", "key": "k"}, headers=admin_headers).status_code == 404


def test_delete_unknown_attachment_404(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    assert client.delete(f"/api/items/{pid}/attachments/nope",
                         headers=admin_headers).status_code == 404


def test_viewer_can_list_not_mutate(client, team, admin_headers, viewer_headers):
    pid = _mk(client, admin_headers)
    assert client.get(f"/api/items/{pid}/attachments", headers=viewer_headers).status_code == 200
    assert client.post(f"/api/items/{pid}/attachments", json={"attId": "x", "key": "k"},
                       headers=viewer_headers).status_code == 403
    assert client.delete(f"/api/items/{pid}/attachments/x",
                         headers=viewer_headers).status_code == 403
