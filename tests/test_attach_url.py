"""ATTACH-URL-1: attachment view/download no longer uses a render-time presigned S3 URL (which expired
300s after render - the FRZ-311 bug). list_attachments returns the authenticated streaming-proxy PATH,
and the bytes are served by GET .../attachments/{id}/raw behind require_item_read.

THE REGRESSION GUARD is test_list_returns_proxy_path_not_presign: if presigns come back to being minted
at render, the returned url stops being the proxy path (it becomes a signed amazonaws URL or None), and
the assertion fails. Demonstrated falsifiable in the deliverable's revert demo. The actual S3 byte
stream needs live AWS creds, so it is verified against the deployed environment, not here (same boundary
as test_attachments.py); the auth/scope/404 gates need no S3 and are covered.
"""
import server


def _mk(client, headers, **fields):
    return client.post("/api/projects", json={"name": "Item", "status": "Planned", **fields},
                       headers=headers).json()["id"]


def _add_att(client, headers, pid, att_id="att1", name="pic.png", ctype="image/png"):
    key = f"items/{pid}/{att_id}/{name}"
    r = client.post(f"/api/items/{pid}/attachments",
                    json={"attId": att_id, "key": key, "name": name, "contentType": ctype, "size": 10},
                    headers=headers)
    assert r.status_code == 200, r.text
    return att_id


# ── THE GUARD ─────────────────────────────────────────────────────────────────────────────────────
def test_list_returns_proxy_path_not_presign(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    _add_att(client, admin_headers, pid, "att1")
    atts = client.get(f"/api/items/{pid}/attachments", headers=admin_headers).json()["attachments"]
    url = atts[0]["url"]
    # It must be the app proxy path, NOT a render-time presigned S3 URL.
    assert url == f"/api/items/{pid}/attachments/att1/raw"
    assert "X-Amz-Signature" not in (url or "")
    assert "amazonaws" not in (url or "")


def test_presign_expiry_is_one_named_constant():
    # The 300s literal is centralized; it governs the UPLOAD PUT only now.
    assert server.PRESIGN_EXPIRY == 300


def test_raw_requires_auth(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    _add_att(client, admin_headers, pid, "att1")
    assert client.get(f"/api/items/{pid}/attachments/att1/raw").status_code == 401


def test_raw_unknown_attachment_404(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    # No attachment recorded -> 404 before any S3 call (proves the lookup + gate run first).
    assert client.get(f"/api/items/{pid}/attachments/nope/raw", headers=admin_headers).status_code == 404


def test_raw_out_of_scope_is_not_readable(client, team, admin_headers, contributor_headers):
    # A Contributor with no read scope on the item cannot reach its attachment bytes (require_item_read).
    pid = _mk(client, admin_headers, assignee="other", dev="OtherPod")
    _add_att(client, admin_headers, pid, "att1")
    assert client.get(f"/api/items/{pid}/attachments/att1/raw", headers=contributor_headers).status_code in (403, 404)


def test_raw_streams_bytes_with_mocked_s3(client, team, admin_headers, monkeypatch):
    # The real S3 byte path needs live creds (verified on the deployed env); here we mock the boto3
    # client to prove the endpoint STREAMS (content-type, cache-control, bytes, HTTP 200 - never XML).
    pid = _mk(client, admin_headers)
    _add_att(client, admin_headers, pid, "att1", name="pic.png", ctype="image/png")

    class _FakeBody:
        def iter_chunks(self, chunk_size=65536):
            yield b"\x89PNG\r\n\x1a\n"
            yield b"rest-of-bytes"

    class _FakeS3:
        def get_object(self, Bucket, Key):
            assert Key == f"items/{pid}/att1/pic.png"
            return {"Body": _FakeBody(), "ContentType": "image/png", "ContentLength": 21}

    monkeypatch.setattr(server, "_s3_client", lambda: _FakeS3())
    r = client.get(f"/api/items/{pid}/attachments/att1/raw", headers=admin_headers)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.headers.get("cache-control")
    assert b"PNG" in r.content and b"rest-of-bytes" in r.content
    assert "<Error>" not in r.text          # never S3 XML
