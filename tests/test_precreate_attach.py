"""PRECREATE-ATTACH-1 Stage A: create-mode attachments via the intake mechanism.

A file is uploaded to intake/{team}/ before the item exists (the new /api/attachments/presign-draft
endpoint, admin/editor only), carrying an HMAC claim token. On create, create_project validates each
pending entry (token + intake-prefix + size), drops any that fail, and promotes the rest to
items/{pid}/ via the SAME _promote_intake_attachments the portal uses. A forged `attachments` array is
stripped; a copy failure drops that file but never blocks the create.

Real S3 needs live creds; here _s3_client is a capturing fake (same boundary as test_intake_promote).

Guard kinds:
- FAIL-ON-REVERT: token/prefix rejection (cases 5-6), attachments strip (case 7), copy-failure drop
  (case 8), and the source-shape guards. Each has a revert demonstration in the report.
- INVARIANT: the intake portal promote path is untouched (test_intake_promote.py still passes).
"""
import json
import re
import pathlib

import server

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"
HTML = ROOT / "roadmap.html"


def _py():
    return SERVER.read_text(encoding="utf-8", errors="replace")


def _html():
    return HTML.read_text(encoding="utf-8", errors="replace")


class _FakeS3:
    def __init__(self, fail_copy=False):
        self.copies = []
        self.fail_copy = fail_copy

    def generate_presigned_post(self, bucket, key, Fields=None, Conditions=None, ExpiresIn=None):
        return {"url": "https://s3.example/upload", "fields": dict(Fields or {})}

    def copy_object(self, **kw):
        self.copies.append(kw)
        if self.fail_copy:
            raise RuntimeError("simulated copy failure")
        return {"CopyObjectResult": {"ETag": "x"}}

    def get_object(self, Bucket, Key):
        class _Body:
            def iter_chunks(self, chunk_size=65536):
                yield b"\x89PNG\r\n\x1a\n"
        return {"Body": _Body(), "ContentType": "image/png", "ContentLength": 8}


def _item(client, admin_headers, name):
    return next(p for p in client.get("/api/all", headers=admin_headers).json()["projects"]
               if p["name"] == name)


def _pending(team, user, uuid="u1", name="shot.png", size=100, token=None, key=None):
    key = key if key is not None else f"intake/{team}/{uuid}/{name}"
    token = token if token is not None else server._draft_attach_token(team, key, user)
    return {"key": key, "token": token, "attId": uuid, "name": name, "size": size, "contentType": "image/png"}


# ── the draft presign endpoint ────────────────────────────────────────────────
def test_presign_draft_admin_gets_key_and_verifying_token(client, team, admin_headers, monkeypatch):
    monkeypatch.setattr(server, "_s3_client", lambda: _FakeS3())
    r = client.post("/api/attachments/presign-draft",
                    json={"filename": "a.png", "contentType": "image/png", "size": 100},
                    headers=admin_headers)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["key"].startswith(f"intake/{team}/"), d["key"]           # server-built intake prefix (no staging/)
    assert d["token"] == server._draft_attach_token(team, d["key"], "admin")   # token verifies for this user+key
    assert "url" in d and "fields" in d                               # presigned POST shape


def test_presign_draft_editor_allowed_contributor_and_viewer_refused(client, team, admin_headers,
                                                                     editor_headers, contributor_headers,
                                                                     viewer_headers, monkeypatch):
    monkeypatch.setattr(server, "_s3_client", lambda: _FakeS3())
    body = {"filename": "a.png", "contentType": "image/png", "size": 10}
    assert client.post("/api/attachments/presign-draft", json=body, headers=editor_headers).status_code == 200
    assert client.post("/api/attachments/presign-draft", json=body, headers=contributor_headers).status_code == 403
    assert client.post("/api/attachments/presign-draft", json=body, headers=viewer_headers).status_code == 403


def test_presign_draft_oversize_declared_refused(client, team, admin_headers, monkeypatch):
    monkeypatch.setattr(server, "_s3_client", lambda: _FakeS3())
    r = client.post("/api/attachments/presign-draft",
                    json={"filename": "big.bin", "size": server.MAX_ATTACH_BYTES + 1},
                    headers=admin_headers)
    assert r.status_code == 413


# ── case 1 / 2: valid pending promotes and lands on the saved item ─────────────
def test_create_with_valid_pending_promotes_admin(client, team, admin_headers, monkeypatch):
    fake = _FakeS3()
    monkeypatch.setattr(server, "_s3_client", lambda: fake)
    key = f"intake/{team}/u1/shot.png"
    r = client.post("/api/projects", json={"name": "HasAtt", "status": "New",
                    "pendingAttachments": [_pending(team, "admin", uuid="u1", key=key)]},
                    headers=admin_headers)
    assert r.status_code in (200, 201), r.text
    assert r.json().get("attachmentsDropped") in (None, [])
    it = _item(client, admin_headers, "HasAtt")            # fresh fetch, not the copy call's return
    atts = it.get("attachments") or []
    assert len(atts) == 1 and atts[0]["key"] == f"items/{it['id']}/u1/shot.png", atts
    assert fake.copies and fake.copies[0]["CopySource"]["Key"] == key


def test_create_with_valid_pending_promotes_editor(client, team, admin_headers, editor_headers, monkeypatch):
    fake = _FakeS3()
    monkeypatch.setattr(server, "_s3_client", lambda: fake)
    key = f"intake/{team}/e1/shot.png"
    r = client.post("/api/projects", json={"name": "EdAtt", "status": "New",
                    "pendingAttachments": [_pending(team, "editor1", uuid="e1", key=key)]},
                    headers=editor_headers)
    assert r.status_code in (200, 201), r.text
    it = _item(client, admin_headers, "EdAtt")
    assert (it.get("attachments") or [])[0]["key"] == f"items/{it['id']}/e1/shot.png"


# ── case 5 (REQUIRED GUARD): a tampered token is dropped, item still created ────
def test_tampered_token_dropped_no_copy(client, team, admin_headers, monkeypatch):
    fake = _FakeS3()
    monkeypatch.setattr(server, "_s3_client", lambda: fake)
    bad = _pending(team, "admin", uuid="u1", name="evil.png")
    bad["token"] = "deadbeef" * 4                                    # tampered
    r = client.post("/api/projects", json={"name": "Tampered", "status": "New",
                    "pendingAttachments": [bad]}, headers=admin_headers)
    assert r.status_code in (200, 201), r.text
    assert r.json().get("attachmentsDropped") == ["evil.png"]       # surfaced to the user
    it = _item(client, admin_headers, "Tampered")
    assert (it.get("attachments") or []) == []                      # not persisted
    assert fake.copies == [], "a failed-validation entry must be dropped BEFORE promote (no copy attempted)"


# ── case 6 (REQUIRED GUARD): a key outside the team's intake prefix is dropped ──
def test_foreign_prefix_key_dropped(client, team, admin_headers, monkeypatch):
    fake = _FakeS3()
    monkeypatch.setattr(server, "_s3_client", lambda: fake)
    foreign = f"items/9/x/secret.png"                                # not intake/{team}/
    ent = _pending(team, "admin", key=foreign, name="secret.png")   # token is minted over the foreign key
    r = client.post("/api/projects", json={"name": "Foreign", "status": "New",
                    "pendingAttachments": [ent]}, headers=admin_headers)
    assert r.status_code in (200, 201), r.text
    assert r.json().get("attachmentsDropped") == ["secret.png"]
    it = _item(client, admin_headers, "Foreign")
    assert (it.get("attachments") or []) == []
    assert fake.copies == [], "an out-of-prefix key must be dropped before promote (never passed through)"


# ── case 7 (REQUIRED GUARD): a forged attachments array is not persisted ────────
def test_forged_attachments_array_stripped(client, team, admin_headers, monkeypatch):
    monkeypatch.setattr(server, "_s3_client", lambda: _FakeS3())
    r = client.post("/api/projects", json={"name": "Forged", "status": "New",
                    "attachments": [{"id": "z", "key": "items/9/z/secret.png", "name": "secret.png"}]},
                    headers=admin_headers)
    assert r.status_code in (200, 201), r.text
    it = _item(client, admin_headers, "Forged")
    assert (it.get("attachments") or []) == [], "a client-forged attachments array must not persist on create"


# ── case 8 (REQUIRED GUARD): a copy failure drops that one file, item still lands, other file lands ──
def test_copy_failure_drops_one_not_the_other(client, team, admin_headers, monkeypatch):
    # First file's copy fails, second succeeds. Fake fails only the first copy call.
    class _FailFirst(_FakeS3):
        def copy_object(self, **kw):
            self.copies.append(kw)
            if len(self.copies) == 1:
                raise RuntimeError("simulated copy failure")
            return {}
    fake = _FailFirst()
    monkeypatch.setattr(server, "_s3_client", lambda: fake)
    p1 = _pending(team, "admin", uuid="f1", name="fails.png")
    p2 = _pending(team, "admin", uuid="f2", name="lands.png")
    r = client.post("/api/projects", json={"name": "MixedCopy", "status": "New",
                    "pendingAttachments": [p1, p2]}, headers=admin_headers)
    assert r.status_code in (200, 201), r.text                       # never blocked by a copy failure
    assert r.json().get("attachmentsDropped") == ["fails.png"]
    it = _item(client, admin_headers, "MixedCopy")
    atts = it.get("attachments") or []
    assert [a["name"] for a in atts] == ["lands.png"]               # the good one still landed
    assert atts[0]["key"] == f"items/{it['id']}/f2/lands.png"


# ── cap at 10 (matches the intake submit path) ─────────────────────────────────
def test_pending_capped_at_ten(client, team, admin_headers, monkeypatch):
    monkeypatch.setattr(server, "_s3_client", lambda: _FakeS3())
    pend = [_pending(team, "admin", uuid=f"u{i}", name=f"f{i}.png") for i in range(12)]
    r = client.post("/api/projects", json={"name": "Capped", "status": "New",
                    "pendingAttachments": pend}, headers=admin_headers)
    assert r.status_code in (200, 201), r.text
    it = _item(client, admin_headers, "Capped")
    assert len(it.get("attachments") or []) <= 10


# ── source-shape guards (fail on revert) ───────────────────────────────────────
def test_source_shape_server():
    src = _py()
    assert '@app.post("/api/attachments/presign-draft")' in src, "the draft presign route must exist"
    assert "def presign_draft_attachment(" in src
    assert 'require_role("admin", "editor")' in re.search(r"def presign_draft_attachment\(.*?\):", src, re.DOTALL).group(0)
    assert "def _draft_attach_token(" in src and 'return _sign(f"draft-att:' in src, "HMAC claim token via _sign"
    assert "generate_presigned_post(" in re.search(r"def presign_draft_attachment\(.*?\n@app\.", src, re.DOTALL).group(0), \
        "draft presign must use generate_presigned_post (real content-length-range), not a PUT"
    # create closes the forge hole with an explicit pop and consumes pendingAttachments
    cp = re.search(r"def create_project\(.*?\n@app\.put", src, re.DOTALL).group(0)
    assert 'body.pop("attachments", None)' in cp, "create must strip a client attachments array"
    assert 'body.pop("pendingAttachments", None)' in cp, "create must consume pendingAttachments (not persist it)"
    assert "hmac.compare_digest(tok, _draft_attach_token(team, key, username))" in cp, "token verified in create"
    assert "_promote_intake_attachments(team, body[\"id\"]" in cp, "create reuses the shared promote fn"
    # A1.2: attachments was NOT added to SERVER_OWNED_FIELDS (one mechanism per invariant)
    sof = re.search(r"SERVER_OWNED_FIELDS = \((.*?)\)", src, re.DOTALL).group(1)
    assert '"attachments"' not in sof, "A1.2: attachments must NOT be in SERVER_OWNED_FIELDS (update guard already covers it)"


# ── Item 5: the validate-then-promote block must never RAISE on a malformed pendingAttachments ─────────
# Type confusion is the accidental case. For every shape below the create must still succeed (item made),
# never a 500. A bad ENTRY drops (surfaced via attachmentsDropped) or is ignored; a bad CONTAINER is
# ignored wholesale. None of these may raise.
def test_pending_attachments_type_confusion_never_raises(client, team, admin_headers, monkeypatch):
    monkeypatch.setattr(server, "_s3_client", lambda: _FakeS3())
    goodkey = f"intake/{team}/uZ/ok.png"
    goodtok = server._draft_attach_token(team, goodkey, "admin")
    cases = {
        "absent": _MISSING,
        "string_not_list": "nope",
        "dict_not_list": {"key": goodkey},
        "number_not_list": 5,
        "list_with_None": [None],
        "list_with_string": ["nope"],
        "dict_missing_key": [{"token": goodtok, "size": 1}],
        "dict_missing_token": [{"key": goodkey, "size": 1}],
        "dict_missing_size": [{"key": goodkey, "token": goodtok}],
        "size_not_number_str": [{"key": goodkey, "token": goodtok, "size": "abc"}],
        "size_not_number_list": [{"key": goodkey, "token": goodtok, "size": [1, 2]}],
        "name_not_string": [{"key": goodkey, "token": goodtok, "size": 1, "attId": "uZ", "name": 123}],
        "over_cap_of_10": [{"key": f"intake/{team}/u{i}/f{i}.png",
                            "token": server._draft_attach_token(team, f"intake/{team}/u{i}/f{i}.png", "admin"),
                            "attId": f"u{i}", "size": 1, "name": f"f{i}.png"} for i in range(15)],
    }
    for label, pend in cases.items():
        body = {"name": f"tc-{label}", "status": "New"}
        if pend is not _MISSING:
            body["pendingAttachments"] = pend
        r = client.post("/api/projects", json=body, headers=admin_headers)
        assert r.status_code in (200, 201), f"{label} must not 500: {r.status_code} {r.text[:200]}"
        it = _item(client, admin_headers, f"tc-{label}")            # the item was created either way
        assert (len(it.get("attachments") or []) <= 10), f"{label}: never more than the cap"


_MISSING = object()


def test_source_shape_client():
    src = _html()
    assert "/api/attachments/presign-draft" in src, "client uploads via the draft presign"
    assert "_frzUploadDraftAtt" in src and "_frzPendingAtts" in src
    assert "pendingAttachments:" in src, "the create save must send pendingAttachments"
    assert "composer-attach-zone" in src, "the create-mode drop/select zone"
    assert "Linked assets can be added once the item is saved." in src, "the asset half of the note must survive"
    assert "Files and linked assets can be added once the item is saved." not in src, "the old combined note is replaced"
