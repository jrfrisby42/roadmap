"""INTAKE-PROMOTE-1 Part B.5 guard: a dropped attachment is discoverable to staff via the audit.

Part B (A3 option 3) drops a file whose copy fails and tells the reporter on-screen, but the only
server-side signal was a log.warning - reactive, greppable, not discoverable. B.5 folds the dropped
FILENAMES into the intake:create audit row's `changes`, and ONLY when something was dropped (a
populated field always means a copy failed). The promotion runs before the audit write, so
dropped_atts is in scope - a same-entry fold, no second audit path, no notification.

Guard kinds:
- FAIL-ON-REVERT (new behavior): guard 2 (a copy failure records the dropped filenames in the audit)
  and the source-shape guard. These fail when B.5 is reverted.
- INVARIANT / no-op assertions (pass on both HEAD and revert, proving nothing broke and the field is
  conditional): success writes NO dropped field, no-attachments unchanged, both log lines retained,
  the reporter still sees the drop, the 38 live intake/ still stream, a foreign key is still rejected.

Failure is INDUCED by a MOCKED CopyObject that raises - stated plainly per 2.2. The credential-less
test env makes _s3_client() raise anyway (the opposite of prod), so the mock proves the HANDLING, not
the trigger; the trigger is not reproducible without inducing a real prod failure, which is barred.
"""
import json
import re
import logging
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
    def __init__(self, fail_copy=False):
        self.fail_copy = fail_copy

    def copy_object(self, **kw):
        if self.fail_copy:
            raise RuntimeError("simulated copy failure")
        return {}

    def get_object(self, Bucket, Key):
        class _Body:
            def iter_chunks(self, chunk_size=65536):
                yield b"\x89PNG\r\n\x1a\n"
        return {"Body": _Body(), "ContentType": "image/png", "ContentLength": 8}


def _submit(client, team, atts):
    return client.post(f"/api/intake/{team}", json={
        "title": "Att ticket", "email": "reporter@example.com", "name": "Pat", "attachments": atts})


def _item_id(client, admin_headers, name="Att ticket"):
    return next(p["id"] for p in client.get("/api/all", headers=admin_headers).json()["projects"]
               if p["name"] == name)


def _create_audit_changes(team, pid):
    """The intake:create audit row's changes dict (or None) for an item."""
    with server.db(team) as c:
        row = c.execute("SELECT changes FROM audit_log WHERE project_id=? AND action='intake:create'"
                        " ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    if not row or not row["changes"]:
        return None
    return json.loads(row["changes"])


# ── case 1 (no-op assertion): success writes NO dropped field ─────────────────
def test_success_audit_has_email_and_no_dropped_field(client, team, admin_headers, monkeypatch):
    _expose(client, admin_headers)
    monkeypatch.setattr(server, "_s3_client", lambda: _FakeS3(fail_copy=False))
    server._rate.clear()
    assert _submit(client, team, [{"attId": "a1", "key": f"intake/{team}/u/shot.png",
                                   "name": "shot.png", "size": 100}]).status_code == 200
    ch = _create_audit_changes(team, _item_id(client, admin_headers))
    assert ch is not None and ch.get("email") == "reporter@example.com"
    assert "attachmentsDropped" not in ch, "no drop -> the field must be ABSENT (populated == a real failure)"


# ── case 2 (FAIL-ON-REVERT): a copy failure records the dropped filenames ──────
def test_copy_failure_records_dropped_filenames_in_audit(client, team, admin_headers, monkeypatch):
    _expose(client, admin_headers)
    monkeypatch.setattr(server, "_s3_client", lambda: _FakeS3(fail_copy=True))  # MOCKED failure (see module doc)
    server._rate.clear()
    r = _submit(client, team, [{"attId": "a1", "key": f"intake/{team}/u/shot.png",
                                "name": "shot.png", "size": 100}])
    assert r.status_code == 200, r.text            # ticket still created (A3 option 3)
    ch = _create_audit_changes(team, _item_id(client, admin_headers))
    assert ch.get("email") == "reporter@example.com", "existing changes content must be preserved"
    assert ch.get("attachmentsDropped") == ["shot.png"], "dropped FILENAMES must be recorded in the audit"
    # names, not keys - no S3 prefix leaks into the audit
    assert not any("intake/" in str(x) or "items/" in str(x) for x in ch["attachmentsDropped"])


# ── case 3 (no-op assertion): both log lines retained, and one fires on failure ─
def test_failure_still_logs_warning(client, team, admin_headers, monkeypatch, caplog):
    _expose(client, admin_headers)
    monkeypatch.setattr(server, "_s3_client", lambda: _FakeS3(fail_copy=True))
    server._rate.clear()
    with caplog.at_level(logging.WARNING):
        assert _submit(client, team, [{"attId": "a1", "key": f"intake/{team}/u/shot.png",
                                       "name": "shot.png", "size": 100}]).status_code == 200
    assert any("promote copy failed" in r.getMessage() for r in caplog.records), \
        "the per-file promote-failure log.warning must still fire (the audit does not replace it)"
    src = _py()
    assert '"[Intake] promote copy failed for item %s att %s: %s"' in src, "per-file log line must be retained"
    assert "attachment promotion failed for item" in src, "outer promotion log line must be retained"


# ── case 4 (invariant): the reporter still sees the drop ──────────────────────
def test_reporter_still_sees_drop(client, team, admin_headers, monkeypatch):
    _expose(client, admin_headers)
    monkeypatch.setattr(server, "_s3_client", lambda: _FakeS3(fail_copy=True))
    server._rate.clear()
    r = _submit(client, team, [{"attId": "a1", "key": f"intake/{team}/u/shot.png",
                                "name": "shot.png", "size": 100}])
    assert r.json().get("attachmentsDropped") == ["shot.png"], "the reporter-facing signal is unchanged"


# ── case 5 (no-op assertion): no attachments behaves exactly as today ─────────
def test_no_attachments_audit_unchanged(client, team, admin_headers, monkeypatch):
    _expose(client, admin_headers)
    monkeypatch.setattr(server, "_s3_client", lambda: _FakeS3())
    server._rate.clear()
    assert client.post(f"/api/intake/{team}",
                       json={"title": "Att ticket", "email": "reporter@example.com"}).status_code == 200
    ch = _create_audit_changes(team, _item_id(client, admin_headers))
    assert ch == {"email": "reporter@example.com"}, "no-attachment create audit must be exactly today's content"


# ── case 6 (INVARIANT, load-bearing): a pre-existing intake/ attachment still streams ──
def test_preexisting_intake_key_still_streams(client, team, admin_headers, monkeypatch):
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
    assert seen["key"] == f"intake/{team}/legacyuuid/old.png", "the intake/ key is served verbatim (prefix-agnostic)"


# ── case 7 (invariant): a client-supplied foreign key is still rejected ───────
def test_foreign_key_rejected_and_no_dropped_field(client, team, admin_headers, monkeypatch):
    _expose(client, admin_headers)
    monkeypatch.setattr(server, "_s3_client", lambda: _FakeS3())
    server._rate.clear()
    assert _submit(client, team, [{"attId": "x", "key": "items/9/x/secret.png",
                                   "name": "secret.png", "size": 1}]).status_code == 200
    pid = _item_id(client, admin_headers)
    it = next(p for p in client.get("/api/all", headers=admin_headers).json()["projects"] if p["id"] == pid)
    assert (it.get("attachments") or []) == [], "a foreign key must never be recorded"
    # a rejected-at-record-time key was never an attachment, so it is not a 'drop' either
    ch = _create_audit_changes(team, pid)
    assert "attachmentsDropped" not in ch, "a rejected foreign key is not a promotion drop"


# ── source-shape (FAIL-ON-REVERT): the conditional fold ───────────────────────
def test_audit_fold_is_conditional():
    src = _py()
    assert '_create_changes = {"email": email}' in src, "the base create-audit changes must keep the email"
    assert "if dropped_atts:" in src and '_create_changes["attachmentsDropped"] = dropped_atts' in src, \
        "the dropped names must be folded in ONLY when non-empty"
    assert "changes=_create_changes" in src, "write_audit must use the folded changes dict (no second audit path)"
    assert 'action, "intake:create"' not in src, "must remain a single intake:create audit, not a new action"
