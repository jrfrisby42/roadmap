"""Phase C — sync an item's attachments to its PRIMARY linked Jira ticket.

One-way, add-only, idempotent push. The server reads bytes from S3 and forwards them to
Jira via multipart. These assert the server contract WITHOUT touching real S3 or Jira:
- jira_configured() is forced True (conftest blanks Jira creds);
- _s3_download is stubbed (CI has no AWS creds);
- _jira_req_multipart is stubbed to capture calls / simulate failures.
The multipart body-shape test exercises the REAL _jira_req_multipart with urlopen stubbed.
"""
import json
import pytest
import server


def _mk(client, headers, **extra):
    body = {"name": "Item", "status": "Planned"}
    body.update(extra)
    return client.post("/api/projects", json=body, headers=headers).json()["id"]


def _add_att(client, headers, pid, att_id, name="a.png", ct="image/png", size=1234):
    return client.post(f"/api/items/{pid}/attachments",
                       json={"attId": att_id, "key": f"items/{pid}/{att_id}/{name}",
                             "name": name, "contentType": ct, "size": size},
                       headers=headers)


@pytest.fixture
def jira_up(monkeypatch):
    """Force Jira 'configured' and stub the S3 read. Returns a `calls` list capturing each
    multipart upload as (path, filename, content_type); the stub returns one created att (J1).
    Set jira_up.fail = {filename: HTTPException} to simulate per-file Jira failures."""
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_s3_download", lambda key: b"BYTES:" + key.encode())
    calls = []
    fail = {}
    def fake_multipart(path, filename, content_type, data, timeout=60):
        calls.append((path, filename, content_type))
        if filename in fail:
            raise fail[filename]
        return [{"id": "J1", "filename": filename}]
    monkeypatch.setattr(server, "_jira_req_multipart", fake_multipart)
    fake_multipart.calls = calls
    fake_multipart.fail = fail
    return fake_multipart


def _audit_usernames(team, action="jira:attach-sync"):
    with server.db(team) as c:
        return [r["username"] for r in
                c.execute("SELECT username FROM audit_log WHERE action=?", (action,)).fetchall()]


# ── happy path + idempotency map ──────────────────────────────────────────────

def test_sync_records_jira_map(client, team, admin_headers, jira_up):
    pid = _mk(client, admin_headers, jiraTickets=["PROJ-1"])
    _add_att(client, admin_headers, pid, "att1")
    r = client.post(f"/api/jira/sync-attachments/{pid}", json={}, headers=admin_headers).json()
    assert r["ticket"] == "PROJ-1"
    assert r["synced"] == [{"attId": "att1", "name": "a.png", "jiraId": "J1"}]
    assert r["skipped"] == [] and r["failed"] == []
    # map persisted on the record (list endpoint spreads the record)
    lst = client.get(f"/api/items/{pid}/attachments", headers=admin_headers).json()["attachments"]
    assert lst[0]["jira"] == {"PROJ-1": "J1"}


def test_sync_is_idempotent(client, team, admin_headers, jira_up):
    pid = _mk(client, admin_headers, jiraTickets=["PROJ-1"])
    _add_att(client, admin_headers, pid, "att1")
    client.post(f"/api/jira/sync-attachments/{pid}", json={}, headers=admin_headers)
    r2 = client.post(f"/api/jira/sync-attachments/{pid}", json={}, headers=admin_headers).json()
    assert r2["skipped"] == ["a.png"] and r2["synced"] == []
    assert len(jira_up.calls) == 1   # Jira's endpoint does NOT dedupe — the map must gate it


def test_attid_targets_one_file(client, team, admin_headers, jira_up):
    pid = _mk(client, admin_headers, jiraTickets=["PROJ-1"])
    _add_att(client, admin_headers, pid, "att1", name="a.png")
    _add_att(client, admin_headers, pid, "att2", name="b.png")
    r = client.post(f"/api/jira/sync-attachments/{pid}", json={"attId": "att2"}, headers=admin_headers).json()
    assert [s["name"] for s in r["synced"]] == ["b.png"]
    assert len(jira_up.calls) == 1 and jira_up.calls[0][1] == "b.png"


# ── survives a wholesale blob PUT (the load-bearing regression) ────────────────

def test_jira_map_survives_full_blob_put(client, team, admin_headers, jira_up):
    pid = _mk(client, admin_headers, jiraTickets=["PROJ-1"])
    _add_att(client, admin_headers, pid, "att1")
    client.post(f"/api/jira/sync-attachments/{pid}", json={}, headers=admin_headers)
    # stale, attachment-less description save must NOT wipe the jira map
    client.put(f"/api/projects/{pid}",
               json={"name": "Item", "status": "Planned", "jiraTickets": ["PROJ-1"], "attachments": []},
               headers=admin_headers)
    lst = client.get(f"/api/items/{pid}/attachments", headers=admin_headers).json()["attachments"]
    assert lst[0]["jira"] == {"PROJ-1": "J1"}


# ── per-file isolation + failure reasons ───────────────────────────────────────

def test_per_file_isolation_and_reason(client, team, admin_headers, jira_up):
    pid = _mk(client, admin_headers, jiraTickets=["PROJ-1"])
    _add_att(client, admin_headers, pid, "att1", name="ok.png")
    _add_att(client, admin_headers, pid, "att2", name="toobig.png")
    jira_up.fail["toobig.png"] = server.HTTPException(413, "too big")
    r = client.post(f"/api/jira/sync-attachments/{pid}", json={}, headers=admin_headers).json()
    assert [s["name"] for s in r["synced"]] == ["ok.png"]
    assert r["failed"] == [{"name": "toobig.png", "reason": "exceeds Jira's attachment size limit"}]
    # only the success recorded a map entry
    lst = {a["name"]: a for a in client.get(f"/api/items/{pid}/attachments", headers=admin_headers).json()["attachments"]}
    assert lst["ok.png"].get("jira") == {"PROJ-1": "J1"} and "jira" not in lst["toobig.png"]


def test_failure_reasons(client, team, admin_headers, jira_up, monkeypatch):
    pid = _mk(client, admin_headers, jiraTickets=["PROJ-1"])
    _add_att(client, admin_headers, pid, "a403", name="perm.png")
    jira_up.fail["perm.png"] = server.HTTPException(403, "nope")
    r = client.post(f"/api/jira/sync-attachments/{pid}", json={"attId": "a403"}, headers=admin_headers).json()
    assert r["failed"][0]["reason"] == "Jira permission denied"
    # S3 read failure → its own reason, no map entry, no multipart call
    _add_att(client, admin_headers, pid, "as3", name="s3.png")
    monkeypatch.setattr(server, "_s3_download", lambda key: (_ for _ in ()).throw(RuntimeError("boom")))
    before = len(jira_up.calls)
    r2 = client.post(f"/api/jira/sync-attachments/{pid}", json={"attId": "as3"}, headers=admin_headers).json()
    assert r2["failed"] == [{"name": "s3.png", "reason": "S3 read failed"}]
    assert len(jira_up.calls) == before


def test_flow_size_guard_skips_upload(client, team, admin_headers, jira_up):
    pid = _mk(client, admin_headers, jiraTickets=["PROJ-1"])
    _add_att(client, admin_headers, pid, "big", name="big.zip", ct="application/zip",
             size=server.MAX_ATTACH_BYTES + 1)
    r = client.post(f"/api/jira/sync-attachments/{pid}", json={}, headers=admin_headers).json()
    assert r["failed"] == [{"name": "big.zip", "reason": "exceeds Flow size cap"}]
    assert jira_up.calls == []   # never attempted the upload


# ── audit attribution: auth username, never body ──────────────────────────────

def test_audit_uses_auth_username_not_body(client, team, admin_headers, jira_up):
    pid = _mk(client, admin_headers, jiraTickets=["PROJ-1"])
    _add_att(client, admin_headers, pid, "att1")
    client.post(f"/api/jira/sync-attachments/{pid}",
                json={"attId": "att1", "_username": "evil"}, headers=admin_headers)
    assert _audit_usernames(team) == ["admin"]   # spoofed body _username ignored


# ── gates ──────────────────────────────────────────────────────────────────────

def test_no_ticket_400(client, team, admin_headers, jira_up):
    pid = _mk(client, admin_headers)   # no jiraTickets
    _add_att(client, admin_headers, pid, "att1")
    assert client.post(f"/api/jira/sync-attachments/{pid}", json={}, headers=admin_headers).status_code == 400


def test_not_configured_503(client, team, admin_headers):
    pid = _mk(client, admin_headers, jiraTickets=["PROJ-1"])   # jira_configured() stays False (no jira_up)
    assert client.post(f"/api/jira/sync-attachments/{pid}", json={}, headers=admin_headers).status_code == 503


def test_viewer_forbidden_403(client, team, admin_headers, viewer_headers, jira_up):
    pid = _mk(client, admin_headers, jiraTickets=["PROJ-1"])
    assert client.post(f"/api/jira/sync-attachments/{pid}", json={}, headers=viewer_headers).status_code == 403


def test_unknown_item_404(client, team, admin_headers, jira_up):
    assert client.post("/api/jira/sync-attachments/999999", json={}, headers=admin_headers).status_code == 404


# ── multipart body shape (REAL _jira_req_multipart; urlopen stubbed) ───────────

def test_multipart_body_shape(monkeypatch):
    captured = {}
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'[{"id":"J1"}]'
    def fake_urlopen(req, timeout=60):
        captured["req"] = req
        return _Resp()
    monkeypatch.setattr(server, "urlopen", fake_urlopen)
    monkeypatch.setattr(server, "_jira_auth_header", lambda: "Basic xyz")

    out = server._jira_req_multipart("/rest/api/3/issue/PROJ-1/attachments",
                                     "my report.png", "image/png;charset=binary", b"PNGBYTES")
    assert out == [{"id": "J1"}]
    req = captured["req"]
    ctype = req.get_header("Content-type")
    assert ctype.startswith("multipart/form-data; boundary=----flow")
    assert req.get_header("X-atlassian-token") == "no-check"
    body = req.data
    assert b'\r\n' in body                                            # CRLF, not LF
    assert b'name="file"; filename="my_report.png"' in body          # header-safe (space -> _)
    assert b'Content-Type: image/png' in body                        # ;charset stripped
    assert b'PNGBYTES' in body
    boundary = ctype.split("boundary=")[1]
    assert body.rstrip(b'\r\n').endswith(b'--' + boundary.encode() + b'--')


def test_multipart_defaults_octet_stream(monkeypatch):
    captured = {}
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'[]'
    def fake_urlopen(req, timeout=60):
        captured["req"] = req
        return _Resp()
    monkeypatch.setattr(server, "urlopen", fake_urlopen)
    monkeypatch.setattr(server, "_jira_auth_header", lambda: "Basic xyz")
    server._jira_req_multipart("/x", "f.bin", "", b"DATA")
    assert b'Content-Type: application/octet-stream' in captured["req"].data
