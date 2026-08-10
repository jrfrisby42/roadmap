"""WRITE-2 Stage 3 read-back: POST /api/items/{pid}/requests/{operationRef}/refresh.

Pulls the linked AssetHub request's lifecycle status (GET /api/v1/requests/{public_id},
scope request.read) and records it on the SAME externalRefs entry (requestStatus +
statusCheckedAt) via the dedicated server-owned path. Covers the role gate, the
linked-only precondition, unknown operationRef, the not-configured guard, persistence,
and that a failed AssetHub call changes nothing on the entry.

These are the first endpoint-level tests for the item request routes; the AssetHub call
is faked by monkeypatching server.AssetHubClient (the endpoint's single outbound path).
"""
import json

import pytest

import server


OP_REF = "abc123op"
PUBLIC_ID = "11111111-2222-4333-8444-555555555555"


def _mk_item(client, headers, **over):
    body = {"name": "Readback item", "start": "2026-08-03", "dueWeeks": 2}
    body.update(over)
    r = client.post("/api/projects", headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _seed_request_ref(team, pid, **over):
    """Write a request externalRefs entry the way the create path does (dedicated path,
    direct DB - externalRefs is SERVER_OWNED and can't be seeded through the item PUT)."""
    entry = {"system": "assethub", "kind": "request", "operationRef": OP_REF,
             "status": "linked", "id": PUBLIC_ID, "attempts": 1,
             "at": "2026-08-01T00:00:00+00:00"}
    entry.update(over)
    with server.db(team) as c:
        row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
        p = json.loads(row["data"])
        server._append_external_ref(p, entry)
        server._save_project(c, pid, p)
    return entry


def _read_ref(team, pid):
    with server.db(team) as c:
        row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
    p = json.loads(row["data"])
    return server._find_request_ref(p, OP_REF)


class _FakeClient:
    """Stands in for AssetHubClient: records the GET path, returns a canned envelope."""
    status = "PENDING_BUDGET"
    calls: list = []
    fail_code = None

    def __init__(self, team, role, **kw):
        self.team, self.role = team, role

    def get(self, path, params=None):
        _FakeClient.calls.append(path)
        if _FakeClient.fail_code:
            raise server.AssetHubError(_FakeClient.fail_code, correlation_id="corr-err")
        return {"data": {"public_id": PUBLIC_ID, "status": _FakeClient.status,
                         "title": "Readback item", "quantity": 1,
                         "source_system": "flow", "source_reference": OP_REF,
                         "created_at": "2026-08-01T00:00:00Z",
                         "updated_at": "2026-08-09T00:00:00Z"},
                "correlation_id": "corr-ok"}


@pytest.fixture
def assethub(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.fail_code = None
    _FakeClient.status = "PENDING_BUDGET"
    monkeypatch.setattr(server, "AssetHubClient", _FakeClient)
    monkeypatch.setattr(server, "_assethub_configured", lambda team: True)
    return _FakeClient


def _url(pid):
    return f"/api/items/{pid}/requests/{OP_REF}/refresh"


def test_refresh_records_status_and_persists(client, team, admin_headers, assethub):
    pid = _mk_item(client, admin_headers)
    _seed_request_ref(team, pid)
    r = client.post(_url(pid), headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["requestStatus"] == "PENDING_BUDGET"
    assert body["correlationId"] == "corr-ok"
    # the GET hit the contract route for the stored public_id
    assert assethub.calls == [f"/api/v1/requests/{PUBLIC_ID}"]
    # persisted on the entry, entry still linked
    ref = _read_ref(team, pid)
    assert ref["requestStatus"] == "PENDING_BUDGET" and ref["status"] == "linked"
    assert ref["statusCheckedAt"] and ref["lastCorrelationId"] == "corr-ok"
    # the response's externalRefs mirror the stored entry (the client re-renders from it)
    got = [x for x in body["externalRefs"] if x.get("operationRef") == OP_REF][0]
    assert got["requestStatus"] == "PENDING_BUDGET"


def test_refresh_updates_on_change(client, team, admin_headers, assethub):
    pid = _mk_item(client, admin_headers)
    _seed_request_ref(team, pid)
    client.post(_url(pid), headers=admin_headers)
    assethub.status = "APPROVED"
    r = client.post(_url(pid), headers=admin_headers)
    assert r.json()["requestStatus"] == "APPROVED"
    assert _read_ref(team, pid)["requestStatus"] == "APPROVED"


def test_refresh_failure_changes_nothing(client, team, admin_headers, assethub):
    pid = _mk_item(client, admin_headers)
    _seed_request_ref(team, pid)
    assethub.fail_code = "not_found"
    r = client.post(_url(pid), headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["error"]["code"] == "not_found"
    ref = _read_ref(team, pid)
    assert "requestStatus" not in ref and "statusCheckedAt" not in ref
    assert ref["status"] == "linked"


def test_refresh_requires_linked_entry(client, team, admin_headers, assethub):
    pid = _mk_item(client, admin_headers)
    _seed_request_ref(team, pid, status="failed", id=None)
    assert client.post(_url(pid), headers=admin_headers).status_code == 409
    assert assethub.calls == []


def test_refresh_unknown_operation_ref_404(client, team, admin_headers, assethub):
    pid = _mk_item(client, admin_headers)
    assert client.post(_url(pid), headers=admin_headers).status_code == 404


def test_refresh_role_gate(client, team, admin_headers, editor_headers,
                           viewer_headers, contributor_headers, assethub):
    pid = _mk_item(client, admin_headers)
    _seed_request_ref(team, pid)
    assert client.post(_url(pid), headers=viewer_headers).status_code == 403
    assert client.post(_url(pid), headers=contributor_headers).status_code == 403
    assert client.post(_url(pid), headers=editor_headers).status_code == 200


def test_refresh_requires_assethub_configured(client, team, admin_headers, monkeypatch):
    monkeypatch.setattr(server, "AssetHubClient", _FakeClient)
    pid = _mk_item(client, admin_headers)
    _seed_request_ref(team, pid)
    # no assethubConnection config and no key in this env -> the shared 400 guard
    assert client.post(_url(pid), headers=admin_headers).status_code == 400
