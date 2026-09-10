"""INTAKE-PROMOTE-1 Part B guard: copy-on-submit promotes intake/ attachments to items/.

A portal submission's attachments upload to intake/{team}/ (PRESIGN-CAP-1). Part B copies each one to
items/{pid}/ AFTER the item is inserted (the id does not exist until then - A2.2), re-stores the new
key with a surgical json_set on $.attachments, and does NOT delete the source (the lifecycle rule in
Part C does, preserving a reversal window). A3 option 3: a copy that fails DROPS that file (never left
as a live intake/ reference - the trap) and is surfaced to the reporter, and NEVER blocks the ticket.

Guard kinds:
- FAIL-ON-REVERT (new behavior): guard 1 (promoted key is items/ + streams), guard 3 (copy failure
  drops + surfaces + does not block), and the source-shape guards. These fail when Part B is reverted.
- INVARIANT (must-not-change): guard 5 (a pre-existing intake/-keyed attachment still streams - the
  read path is prefix-agnostic, and 38 such attachments are live in prod right now). Part B does not
  touch the read path, so guard 5 passes on BOTH HEAD and revert by design - its job is to prove the
  promotion work did not break the 38, not to flip on revert. Called out as load-bearing.

The real S3 copy/stream needs live creds; here _s3_client is a capturing fake (same boundary as
test_attachments.py / test_attach_url.py).
"""
import json
import re
import pathlib

import server

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"


def _py() -> str:
    return SERVER.read_text(encoding="utf-8", errors="replace")


def _expose(client, admin_headers):
    client.put("/api/config/statuses", json=["New", "In Progress", "Released"], headers=admin_headers)
    client.put("/api/config/statusIsDefault", json={"New": True}, headers=admin_headers)
    client.put("/api/config/intakeTypes", json=["Bug"], headers=admin_headers)
    client.put("/api/config/intakeEnabled", json=True, headers=admin_headers)


class _FakeS3:
    """Records copy_object / delete_object / get_object and can be told to fail copies."""
    def __init__(self, fail_copy=False):
        self.copies = []
        self.deletes = []
        self.fail_copy = fail_copy

    def copy_object(self, **kw):
        self.copies.append(kw)
        if self.fail_copy:
            raise RuntimeError("simulated copy failure")
        return {"CopyObjectResult": {"ETag": "x"}}

    def delete_object(self, **kw):
        self.deletes.append(kw)
        return {}

    def get_object(self, Bucket, Key):
        class _Body:
            def iter_chunks(self, chunk_size=65536):
                yield b"\x89PNG\r\n\x1a\n"
        return {"Body": _Body(), "ContentType": "image/png", "ContentLength": 8, "_key": Key}


def _submit_with_att(client, team, att_key, att_id="a1", name="shot.png"):
    return client.post(f"/api/intake/{team}", json={
        "title": "With shot", "email": "reporter@example.com", "name": "Pat",
        "attachments": [{"attId": att_id, "key": att_key, "name": name,
                         "contentType": "image/png", "size": 100}]})


def _item_by_name(client, admin_headers, name):
    return next(p for p in client.get("/api/all", headers=admin_headers).json()["projects"]
               if p["name"] == name)


# ── guard 1 (fail-on-revert): promoted to items/ AND streams ──────────────────
def test_submit_promotes_intake_to_items_and_streams(client, team, admin_headers, monkeypatch):
    _expose(client, admin_headers)
    fake = _FakeS3()
    monkeypatch.setattr(server, "_s3_client", lambda: fake)
    server._rate.clear()
    r = _submit_with_att(client, team, f"intake/{team}/uuid1/shot.png")
    assert r.status_code == 200, r.text
    assert r.json().get("attachmentsDropped") == []
    it = _item_by_name(client, admin_headers, "With shot")
    atts = it.get("attachments") or []
    assert len(atts) == 1
    # the RECORDED key is now items/{pid}/... (promoted), not intake/
    assert atts[0]["key"] == f"items/{it['id']}/a1/shot.png", atts[0]["key"]
    # a copy actually happened, intake/ -> items/
    assert len(fake.copies) == 1
    assert fake.copies[0]["CopySource"] == {"Bucket": server.ATTACH_BUCKET, "Key": f"intake/{team}/uuid1/shot.png"}
    assert fake.copies[0]["Key"] == f"items/{it['id']}/a1/shot.png"
    # and it streams from the new key
    rr = client.get(f"/api/items/{it['id']}/attachments/a1/raw", headers=admin_headers)
    assert rr.status_code == 200 and rr.headers["content-type"].startswith("image/png")


# ── guard 2 (fail-on-revert): source NOT deleted by the app ───────────────────
def test_source_object_not_deleted_on_submit(client, team, admin_headers, monkeypatch):
    _expose(client, admin_headers)
    fake = _FakeS3()
    monkeypatch.setattr(server, "_s3_client", lambda: fake)
    server._rate.clear()
    assert _submit_with_att(client, team, f"intake/{team}/uuid2/shot.png").status_code == 200
    assert fake.deletes == [], "the app must NOT delete the intake/ source (the lifecycle rule does)"


# ── guard 3 (fail-on-revert, EMPHASIS): copy failure drops + surfaces, never blocks ──
def test_copy_failure_drops_file_and_does_not_block_ticket(client, team, admin_headers, monkeypatch):
    _expose(client, admin_headers)
    fake = _FakeS3(fail_copy=True)
    monkeypatch.setattr(server, "_s3_client", lambda: fake)
    server._rate.clear()
    r = _submit_with_att(client, team, f"intake/{team}/uuid3/shot.png", name="shot.png")
    # the ticket is STILL created (never blocked by a copy failure - A3 option 3)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("id") and body.get("itemKey")
    # the failed file is DROPPED (not left as an intake/ reference) and SURFACED to the reporter
    assert body.get("attachmentsDropped") == ["shot.png"]
    it = _item_by_name(client, admin_headers, "With shot")
    assert (it.get("attachments") or []) == [], "a file that could not be copied must not remain recorded"


# ── guard 4 (invariant): a client-supplied foreign key is still rejected ──────
def test_client_supplied_foreign_key_still_rejected(client, team, admin_headers, monkeypatch):
    _expose(client, admin_headers)
    monkeypatch.setattr(server, "_s3_client", lambda: _FakeS3())
    server._rate.clear()
    r = client.post(f"/api/intake/{team}", json={
        "title": "Foreign key", "email": "reporter@example.com",
        "attachments": [{"attId": "x", "key": "items/9/x/secret.png", "name": "secret.png", "size": 1}]})
    assert r.status_code == 200
    it = _item_by_name(client, admin_headers, "Foreign key")
    assert (it.get("attachments") or []) == [], "a non-intake/ key must never be recorded (submit-time validation)"


# ── guard 5 (INVARIANT, load-bearing): a pre-existing intake/-keyed attachment still streams ──
def test_preexisting_intake_key_still_streams(client, team, admin_headers, monkeypatch):
    # 38 items in prod carry intake/-keyed attachments. The read path is prefix-agnostic and MUST
    # stay so: promotion changes only NEW submissions, never how an already-stored key is served.
    pid = client.post("/api/projects", json={"name": "Legacy att", "status": "New"},
                      headers=admin_headers).json()["id"]
    with server.db(team) as c:
        row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
        p = json.loads(row["data"])
        p["attachments"] = [{"id": "leg1", "key": f"intake/{team}/legacyuuid/old.png",
                             "name": "old.png", "contentType": "image/png", "size": 5}]
        c.execute("UPDATE projects SET data=? WHERE id=?", (json.dumps(p), pid))
    seen = {}

    class _F(_FakeS3):
        def get_object(self, Bucket, Key):
            seen["key"] = Key
            return super().get_object(Bucket, Key)
    monkeypatch.setattr(server, "_s3_client", lambda: _F())
    r = client.get(f"/api/items/{pid}/attachments/leg1/raw", headers=admin_headers)
    assert r.status_code == 200 and b"PNG" in r.content
    assert seen["key"] == f"intake/{team}/legacyuuid/old.png", "the intake/ key must be served verbatim"


# ── guard 6 (invariant): _intake_open still gates submit ──────────────────────
def test_closed_team_still_gates_submit(client, team, monkeypatch):
    monkeypatch.setattr(server, "_s3_client", lambda: _FakeS3())
    server._rate.clear()
    assert _submit_with_att(client, team, f"intake/{team}/u/shot.png").status_code == 404


# ── source-shape (fail-on-revert): the helper + integration + portal ──────────
def test_promote_helper_shape():
    src = _py()
    m = re.search(r"def _promote_intake_attachments\(.*?\n@app\.", src, re.DOTALL)
    assert m, "the promote helper was not found"
    h = m.group(0)
    assert "s3.copy_object(**params)" in h, "must copy via copy_object"
    assert '"CopySource": {"Bucket": ATTACH_BUCKET, "Key": key}' in h, "must copy from the intake/ source key"
    assert "_attachment_key(pid," in h, "dest must be the items/ convention via _attachment_key"
    assert "dropped.append" in h, "a failed copy must be dropped (A3 option 3)"
    assert "delete_object" not in h, "the helper must NOT delete the source object"
    assert "if ATTACH_KMS_KEY_ID:" in h, "SSE-KMS guard must be mirrored (belt-and-suspenders)"


def test_submit_integration_and_return_shape():
    src = _py()
    # promotion runs AFTER the insert, re-storing via a surgical json_set on $.attachments
    assert 'item["id"] = _insert_project(c, item)' in src
    assert "_promote_intake_attachments(team, item[\"id\"], item[\"attachments\"])" in src
    assert "json_set(data, '$.attachments', json(?))" in src, "re-store must be a surgical json_set, not a wholesale rewrite"
    assert '"attachmentsDropped": dropped_atts' in src, "submit must surface dropped names in the response"


def test_portal_surfaces_dropped_to_reporter():
    src = _py()
    assert 'id="doneAttWarn"' in src, "the success screen needs a dropped-attachment warning element"
    assert "d.attachmentsDropped" in src, "the portal must read attachmentsDropped"
    assert "could not be saved" in src, "the reporter must see an explicit dropped-file message"
