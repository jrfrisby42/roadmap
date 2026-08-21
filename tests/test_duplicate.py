"""DUPLICATE-1: mark an item as a duplicate of another (same team). Nothing moves - the loser is
closed (status Duplicate) and cross-linked via externalRefs; reversible."""
import server


def _hdr(team, user, role):
    return {"Authorization": "Bearer " + server.create_token(team, user, role), "X-Team": team}


def _mk(client, headers, **f):
    return client.post("/api/projects", json={"name": "X", "status": "Planned", **f}, headers=headers).json()["id"]


def _blob(team, pid):
    with server.db(team) as c:
        return server._get_item_blob(c, pid)


# ── reserved status ──────────────────────────────────────────────────────────
def test_duplicate_status_terminal_reserved_and_not_config(team):
    assert server._is_terminal(server.DUPLICATE_STATUS, team) is True
    assert server.DUPLICATE_STATUS in server.RESERVED_TERMINAL_STATUSES
    assert server.TRANSFERRED_STATUS in server.RESERVED_TERMINAL_STATUSES        # transfer unchanged
    assert server.DUPLICATE_STATUS not in (server._cfg_val(team, "statuses", []) or [])


# ── mark + unmark round-trip ─────────────────────────────────────────────────
def test_mark_and_unmark_roundtrip(client, team, admin_headers):
    lid = _mk(client, admin_headers, name="Dup")
    sid = _mk(client, admin_headers, name="Real")
    prev = _blob(team, lid)["status"]                                            # whatever it created as
    skey = _blob(team, sid)["itemKey"]

    r = client.post(f"/api/items/{lid}/mark-duplicate", json={"survivorId": sid}, headers=admin_headers)
    assert r.status_code == 200, r.text
    lb, sb = _blob(team, lid), _blob(team, sid)
    assert lb["status"] == server.DUPLICATE_STATUS
    assert "completedAt" not in lb                                               # a duplicate is not completed work
    dref = server._find_dup_ref(lb, "duplicate-of")
    assert dref and dref["targetId"] == sid and dref["prevStatus"] == prev and dref["number"] == skey
    bref = server._find_dup_ref(sb, "duplicated-by")
    assert bref and bref["sourceId"] == lid
    # findable by the status filter
    got = client.get("/api/items?status=Duplicate", headers=admin_headers).json()
    assert any(i["id"] == lid for i in got["items"])

    r2 = client.post(f"/api/items/{lid}/unmark-duplicate", json={}, headers=admin_headers)
    assert r2.status_code == 200
    lb2, sb2 = _blob(team, lid), _blob(team, sid)
    assert lb2["status"] == prev                                                 # prior status restored
    assert server._find_dup_ref(lb2, "duplicate-of") is None                     # both directions removed
    assert server._find_dup_ref(sb2, "duplicated-by") is None


# ── guards ───────────────────────────────────────────────────────────────────
def test_cannot_duplicate_self(client, team, admin_headers):
    a = _mk(client, admin_headers)
    assert client.post(f"/api/items/{a}/mark-duplicate", json={"survivorId": a}, headers=admin_headers).status_code == 422


def test_survivor_must_exist(client, team, admin_headers):
    a = _mk(client, admin_headers)
    assert client.post(f"/api/items/{a}/mark-duplicate", json={"survivorId": 999999}, headers=admin_headers).status_code == 404


def test_cannot_mark_an_already_duplicate(client, team, admin_headers):
    a, b, c = _mk(client, admin_headers), _mk(client, admin_headers), _mk(client, admin_headers)
    assert client.post(f"/api/items/{a}/mark-duplicate", json={"survivorId": b}, headers=admin_headers).status_code == 200
    r = client.post(f"/api/items/{a}/mark-duplicate", json={"survivorId": c}, headers=admin_headers)
    assert r.status_code == 409 and "already" in r.json()["detail"]["message"].lower()


def test_chain_is_refused_and_names_the_real_survivor(client, team, admin_headers):
    a, b, c = _mk(client, admin_headers), _mk(client, admin_headers), _mk(client, admin_headers)
    bkey = _blob(team, b)["itemKey"]
    client.post(f"/api/items/{a}/mark-duplicate", json={"survivorId": b}, headers=admin_headers)   # a -> b
    r = client.post(f"/api/items/{c}/mark-duplicate", json={"survivorId": a}, headers=admin_headers)  # c -> a (a is a dup)
    assert r.status_code == 409
    assert bkey in r.json()["detail"]["message"]                                 # names b, the real survivor


# ── role gate ────────────────────────────────────────────────────────────────
def test_mark_is_admin_or_editor_not_viewer(client, team, admin_headers):
    a, b = _mk(client, admin_headers), _mk(client, admin_headers)
    body = {"survivorId": b}
    assert client.post(f"/api/items/{a}/mark-duplicate", json=body, headers=_hdr(team, "v", "viewer")).status_code == 403
    assert client.post(f"/api/items/{a}/mark-duplicate", json=body, headers=_hdr(team, "e", "editor")).status_code == 200
