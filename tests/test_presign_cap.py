"""PRESIGN-CAP-1 regression guard: the public intake size cap is a real S3 policy condition.

The old path signed a PUT (generate_presigned_url), which cannot carry a size condition, so the
15 MB cap was only a declared-size refusal while the actual body was unbounded - an unauthenticated
write primitive into the bucket. The fix converts the presign to generate_presigned_post with a
content-length-range condition, so S3 itself rejects an oversized body (403 EntityTooLarge). The
portal's upload code switches PUT -> multipart POST in the same stage (the portal JS lives inside
server.py).

Two kinds of guard here:
- BEHAVIORAL (a capturing fake _s3_client): the exact Conditions/Fields/Key handed to S3 - the size
  bound, the Content-Type pin, the SSE-KMS mirror-guard, and that the key stays server-generated even
  when the client tries to supply one. The credential-less test env would 502 on a real presign, so
  the fake is how the policy is asserted without live creds. The live S3 rejection (case 3) is J.R.'s
  post-deploy step - it needs a real KMS bucket.
- SOURCE-SHAPE: the server conversion, the retained pre-check, the corrected item-page comment (which
  must NOT claim a policy cap it doesn't have), and the portal POST/FormData/file-last shape.
"""
import re
import pathlib

import server

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"


def _py() -> str:
    return SERVER.read_text(encoding="utf-8", errors="replace")


def _expose(client, admin_headers, types=None):
    client.put("/api/config/statuses", json=["New", "In Progress", "Released"], headers=admin_headers)
    client.put("/api/config/statusIsDefault", json={"New": True}, headers=admin_headers)
    client.put("/api/config/intakeTypes", json=(types if types is not None else ["Bug"]), headers=admin_headers)
    client.put("/api/config/intakeEnabled", json=True, headers=admin_headers)


class _FakeS3:
    """Captures the exact generate_presigned_post call and echoes a plausible POST response."""
    def __init__(self, sink):
        self.sink = sink

    def generate_presigned_post(self, Bucket, Key, Fields=None, Conditions=None, ExpiresIn=None):
        self.sink.update(dict(Bucket=Bucket, Key=Key, Fields=Fields or {},
                              Conditions=Conditions or [], ExpiresIn=ExpiresIn))
        # A real POST response carries the pre-filled fields plus the signed policy.
        fields = dict(Fields or {})
        fields.update({"key": Key, "policy": "b64policy", "x-amz-signature": "sig"})
        return {"url": f"https://{Bucket}.s3.us-west-2.amazonaws.com/", "fields": fields}


def _capture(monkeypatch):
    sink = {}
    monkeypatch.setattr(server, "_s3_client", lambda: _FakeS3(sink))
    return sink


# ── behavioral: the policy the endpoint hands to S3 ───────────────────────────
def test_presign_uses_post_with_content_length_range(client, team, admin_headers, monkeypatch):
    _expose(client, admin_headers)
    sink = _capture(monkeypatch)
    server._rate.clear()
    r = client.post(f"/api/intake/{team}/attach",
                    json={"filename": "shot.png", "contentType": "image/png", "size": 100})
    assert r.status_code == 200, r.text
    body = r.json()
    # response is the POST shape (url + fields), NOT the old signed-PUT shape (url + headers)
    assert "fields" in body and "headers" not in body, "response must expose POST fields, not PUT headers"
    assert body["url"] and body["attId"] and body["key"]
    # the size cap is a real policy condition bounded by the public max
    clr = [c for c in sink["Conditions"] if isinstance(c, list) and c and c[0] == "content-length-range"]
    assert clr == [["content-length-range", 0, server._INTAKE_MAX_ATTACH_BYTES]], sink["Conditions"]
    # content-type is pinned as both a field and a condition (allow-list becomes an upload constraint)
    assert sink["Fields"].get("Content-Type") == "image/png"
    assert {"Content-Type": "image/png"} in sink["Conditions"]
    # presign expiry unchanged
    assert sink["ExpiresIn"] == server.PRESIGN_EXPIRY


def test_key_is_server_generated_even_if_client_supplies_one(client, team, admin_headers, monkeypatch):
    # Case 10: a client cannot choose the key or prefix - the endpoint ignores any key/prefix in the
    # body and pins its own intake/{team}/{uuid}/ shape into the (fixed-Key) POST policy.
    _expose(client, admin_headers)
    sink = _capture(monkeypatch)
    server._rate.clear()
    r = client.post(f"/api/intake/{team}/attach",
                    json={"filename": "a.png", "contentType": "image/png", "size": 10,
                          "key": "items/9/evil/x.png", "prefix": "items/9/"})
    assert r.status_code == 200
    assert re.fullmatch(rf"intake/{team}/[0-9a-f]{{32}}/a\.png", sink["Key"]), sink["Key"]
    assert r.json()["key"] == sink["Key"]


def test_sse_kms_is_mirrored_under_the_guard_not_hardcoded(client, team, admin_headers, monkeypatch):
    _expose(client, admin_headers)
    # KMS SET: the SSE-KMS values travel as POST fields AND matching policy conditions.
    monkeypatch.setattr(server, "ATTACH_KMS_KEY_ID", "arn:aws:kms:us-west-2:1:key/abc")
    sink = _capture(monkeypatch)
    server._rate.clear()
    assert client.post(f"/api/intake/{team}/attach",
                       json={"filename": "a.png", "contentType": "image/png", "size": 10}).status_code == 200
    assert sink["Fields"].get("x-amz-server-side-encryption") == "aws:kms"
    assert sink["Fields"].get("x-amz-server-side-encryption-aws-kms-key-id") == "arn:aws:kms:us-west-2:1:key/abc"
    assert {"x-amz-server-side-encryption": "aws:kms"} in sink["Conditions"]
    assert {"x-amz-server-side-encryption-aws-kms-key-id": "arn:aws:kms:us-west-2:1:key/abc"} in sink["Conditions"]

    # KMS UNSET: the bucket default applies, so the endpoint omits the SSE fields entirely (not hardcoded).
    monkeypatch.setattr(server, "ATTACH_KMS_KEY_ID", None)
    sink2 = _capture(monkeypatch)
    server._rate.clear()
    assert client.post(f"/api/intake/{team}/attach",
                       json={"filename": "a.png", "contentType": "image/png", "size": 10}).status_code == 200
    assert not any(k.startswith("x-amz-server-side-encryption") for k in sink2["Fields"]), sink2["Fields"]
    assert not any(isinstance(c, dict) and any(k.startswith("x-amz-server-side-encryption") for k in c)
                   for c in sink2["Conditions"]), sink2["Conditions"]


def test_declared_size_precheck_and_type_gate_survive(client, team, admin_headers, monkeypatch):
    # 2.1.2 belt-and-suspenders: the fast declared-size refusal is kept (before any S3 call), and the
    # type allow-list still refuses a disallowed type. Neither reaches the fake.
    _expose(client, admin_headers)
    _capture(monkeypatch)
    server._rate.clear()
    assert client.post(f"/api/intake/{team}/attach",
                       json={"filename": "big.png", "contentType": "image/png",
                             "size": server._INTAKE_MAX_ATTACH_BYTES + 1}).status_code == 413
    server._rate.clear()
    assert client.post(f"/api/intake/{team}/attach",
                       json={"filename": "a.exe", "contentType": "application/x-msdownload",
                             "size": 10}).status_code == 415


def test_closed_team_gate_survives(client, team, monkeypatch):
    # _intake_open(team) still gates: a team that never enabled intake refuses before any S3 call.
    _capture(monkeypatch)
    server._rate.clear()
    assert client.post(f"/api/intake/{team}/attach",
                       json={"filename": "a.png", "contentType": "image/png", "size": 10}).status_code == 404


# ── source-shape: the conversion, the retained pieces, the corrected comment ──
def test_server_uses_presigned_post_not_put():
    src = _py()
    m = re.search(r"def intake_presign\(.*?\ndef ", src, re.DOTALL)
    assert m, "intake_presign not found"
    block = m.group(0)
    assert "generate_presigned_post(" in block, "intake presign must use generate_presigned_post"
    assert 'generate_presigned_url("put_object"' not in block, "intake presign must no longer sign a PUT"
    assert '["content-length-range", 0, _INTAKE_MAX_ATTACH_BYTES]' in block, "the size cap must be a content-length-range condition"
    assert 'if ATTACH_KMS_KEY_ID:' in block, "SSE-KMS must stay behind the ATTACH_KMS_KEY_ID guard, not hardcoded"
    assert 'if size > _INTAKE_MAX_ATTACH_BYTES:' in block, "the declared-size pre-check must be retained"
    assert '"fields": post["fields"]' in block and '"headers"' not in block, "response returns POST fields, not PUT headers"


def test_submit_key_validation_untouched():
    # 2.2: the submit-time key validation is unchanged - only intake/{team}/ keys with a valid attId land.
    src = _py()
    assert 'if not key.startswith(f"intake/{team}/") or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", aid):' in src


def test_item_page_comment_corrected_and_endpoint_unchanged():
    src = _py()
    # 0.3: the overstated comment must be gone and replaced with an honest one.
    assert "enforced server-side (refuse to sign) + client-side" not in src, "the overstated item-page comment must be corrected"
    assert "declared-size REFUSAL, not an S3 policy cap" in src, "the item-page comment must state the true (advisory) nature"
    # 3.2: the item-page endpoint itself is NOT converted in this stage - it still signs a PUT.
    m = re.search(r"def presign_attachment\(.*?\ndef ", src, re.DOTALL)
    assert m and 'generate_presigned_url(' in m.group(0) and '"put_object"' in m.group(0), \
        "the item-page presign must remain a signed PUT (out of scope this stage)"


def test_portal_upload_is_post_with_file_last():
    src = _py()
    # the portal JS lives inside server.py; the upload must be a multipart POST with the file appended
    # AFTER every policy field (S3 ignores fields that follow the file).
    assert "var fd=new FormData(); var flds=pd.fields||{};" in src, "portal must build a FormData from pd.fields"
    forEach_at = src.find("Object.keys(flds).forEach(function(k){ fd.append(k, flds[k]); });")
    file_at = src.find("fd.append('file', f);")
    post_at = src.find("var put=await fetch(pd.url,{method:'POST',body:fd});")
    assert forEach_at != -1 and file_at != -1 and post_at != -1, "portal POST/FormData lines missing"
    assert forEach_at < file_at < post_at, "the file must be appended AFTER the policy fields, before the POST"
    assert "if(/EntityTooLarge/i.test(xt)) msg='exceeds the 15 MB limit'" in src, "portal must surface the oversized (EntityTooLarge) rejection honestly"
    assert "method:'PUT'" not in src[src.find("async function addFiles"):src.find("async function submitForm")], \
        "the portal upload must no longer PUT"
