"""
TRANSFER-1: cross-team item transfer.

The two teams are separate SQLite files, so a transfer cannot be one transaction; it is
idempotent instead, via a server-owned `externalRefs` marker carrying an operationRef. These
tests exercise: the happy path (fields carried vs dropped), the reserved terminal status, the
idempotency guard (both that it prevents a duplicate AND that removing its key WOULD duplicate),
attachment copy ordering (via a stubbed _s3_copy, since real S3 needs live creds), the pending
list + retry, and the admin-only role gate.
"""
import json
import pytest

import server


# ── helpers ───────────────────────────────────────────────────────────────────
def _headers(team, username="admin", role="admin"):
    token = server.create_token(team, username, role)
    return {"Authorization": f"Bearer {token}", "X-Team": team}


def _fresh_team(slug):
    import os
    os.makedirs(os.path.join(server.TENANTS_DIR, slug), exist_ok=True)
    server.init_team_db(slug)
    return slug


def _set_cfg(team, key, val):
    with server.db(team) as c:
        row = c.execute("SELECT 1 FROM config WHERE key=?", (key,)).fetchone()
        if row:
            c.execute("UPDATE config SET value=? WHERE key=?", (json.dumps(val), key))
        else:
            c.execute("INSERT INTO config(key,value) VALUES(?,?)", (key, json.dumps(val)))


def _make_item(team, **fields):
    data = {"name": "Broken login page", "status": "New", "product": ""}
    data.update(fields)
    with server.db(team) as c:
        server._assign_item_key(c, data)
        data["id"] = server._insert_project(c, data)
    return data["id"]


def _get_item(team, pid):
    with server.db(team) as c:
        return server._get_item_blob(c, pid)


def _all_items(team):
    with server.db(team) as c:
        return [json.loads(r["data"]) for r in c.execute("SELECT data FROM projects").fetchall()]


def _target_team(name="tgtteam"):
    """A portal-open target team exposing one project 'Websites'."""
    t = _fresh_team(name)
    _set_cfg(t, "intakeEnabled", True)
    _set_cfg(t, "products", [{"name": "Websites"}])
    _set_cfg(t, "intakeProjects", [])          # empty = all
    _set_cfg(t, "statuses", ["New", "In Progress", "Done"])
    _set_cfg(t, "statusIsDefault", {"New": True})
    _set_cfg(t, "statusIsTerminal", {"Done": True})
    _set_cfg(t, "types", ["Bug", "Request"])
    _set_cfg(t, "departments", ["Web"])
    return t


@pytest.fixture
def src(team):
    return team


# ── terminality (reserved status) ───────────────────────────────────────────────
def test_reserved_status_is_terminal(src):
    assert server._is_terminal(server.TRANSFERRED_STATUS, src) is True


def test_reserved_status_not_in_team_config(src):
    # No config on any team should contain "Transferred" - it is not team config at all.
    statuses = server._cfg_val(src, "statuses", []) or []
    assert server.TRANSFERRED_STATUS not in statuses
    assert server.TRANSFERRED_STATUS not in (server._cfg_val(src, "statusIsTerminal", {}) or {})


# ── role gate ───────────────────────────────────────────────────────────────────
def test_transfer_admin_only(client, src):
    tgt = _target_team("tgtrole")
    pid = _make_item(src)
    body = {"targetTeam": tgt, "targetProject": "Websites"}
    assert client.post(f"/api/items/{pid}/transfer", json=body,
                       headers=_headers(src, "editor1", "editor")).status_code == 403
    assert client.post(f"/api/items/{pid}/transfer", json=body,
                       headers=_headers(src, "admin", "admin")).status_code == 200


# ── validation ──────────────────────────────────────────────────────────────────
def test_transfer_rejects_same_team_and_bad_target(client, src):
    tgt = _target_team("tgtval")
    pid = _make_item(src)
    # same team
    assert client.post(f"/api/items/{pid}/transfer", json={"targetTeam": src, "targetProject": ""},
                       headers=_headers(src)).status_code == 422
    # unknown project for the target
    assert client.post(f"/api/items/{pid}/transfer", json={"targetTeam": tgt, "targetProject": "Nope"},
                       headers=_headers(src)).status_code == 422
    # a team not accepting transfers (portal closed)
    closed = _fresh_team("closedteam")
    _set_cfg(closed, "intakeEnabled", False)
    assert client.post(f"/api/items/{pid}/transfer", json={"targetTeam": closed, "targetProject": ""},
                       headers=_headers(src)).status_code == 422


# ── happy path: fields carried vs dropped ───────────────────────────────────────
def test_transfer_happy_path_fields(client, src):
    tgt = _target_team("tgthappy")
    pid = _make_item(
        src, name="Cannot reset password", description="steps<br>here",
        priority="2", type="Bug", reporter="Jane", reporterEmail="jane@x.com",
        departments=["Web"], createdAt="2025-01-02T03:04:05+00:00", source="portal",
        # dropped but NOT blocking (A1.1: auto-assign means their presence is not "work"):
        dev="Alice", assignee="bob",
    )
    r = client.post(f"/api/items/{pid}/transfer",
                    json={"targetTeam": tgt, "targetProject": "Websites"}, headers=_headers(src))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] and out["targetTeam"] == tgt and out["targetId"]
    tid = out["targetId"]

    # source tombstoned, NOT completed, cross-linked
    s = _get_item(src, pid)
    assert s["status"] == server.TRANSFERRED_STATUS
    assert "completedAt" not in s          # transferred != completed
    sref = server._find_transfer_ref(s, "transferred-to")
    assert sref and sref["status"] == "linked" and sref["targetId"] == tid

    # target carries the right fields
    t = _get_item(tgt, tid)
    assert t["name"] == "Cannot reset password"
    assert t["description"] == "steps<br>here"
    assert t["priority"] == "2"
    assert t["type"] == "Bug"              # target has an equivalent type
    assert t["reporter"] == "Jane" and t["reporterEmail"] == "jane@x.com"
    assert t["departments"] == ["Web"]     # exact match present on target
    assert t["product"] == "Websites"
    assert t["createdAt"] == "2025-01-02T03:04:05+00:00"   # original preserved
    assert t["status"] == "New"            # target default
    # owner + assignee are dropped (A1.1) though they did not block
    for k in ("dev", "assignee", "sprintId", "release", "jiraTickets", "comments"):
        assert k not in t, f"{k} should not be carried"
    assert t["source"] == "portal"         # A2: carried through (this item's source was portal)
    # target cross-links back
    tref = server._find_transfer_ref(t, "transferred-from")
    assert tref and tref["sourceId"] == pid and tref["operationRef"] == sref["operationRef"]


def test_transfer_drops_unmatched_type_and_department(client, src):
    tgt = _target_team("tgtmap")
    pid = _make_item(src, type="Incident", departments=["Payroll"])   # neither exists on target
    r = client.post(f"/api/items/{pid}/transfer",
                    json={"targetTeam": tgt, "targetProject": "Websites"}, headers=_headers(src))
    assert r.status_code == 200
    t = _get_item(tgt, r.json()["targetId"])
    assert t["type"] == ""            # no equivalent -> dropped
    assert t["departments"] == []     # no exact match -> dropped


def test_transfer_needs_no_target_config_written(client, src):
    tgt = _target_team("tgtnocfg")
    before = server._cfg_val(tgt, "statuses", [])
    pid = _make_item(src)
    client.post(f"/api/items/{pid}/transfer",
                json={"targetTeam": tgt, "targetProject": "Websites"}, headers=_headers(src))
    after = server._cfg_val(tgt, "statuses", [])
    assert before == after                                   # no team config written
    assert server.TRANSFERRED_STATUS not in (after or [])    # reserved status never added to config


# ── idempotency ─────────────────────────────────────────────────────────────────
def test_transfer_idempotent_no_duplicate(client, src):
    tgt = _target_team("tgtidem")
    pid = _make_item(src)
    body = {"targetTeam": tgt, "targetProject": "Websites"}
    r1 = client.post(f"/api/items/{pid}/transfer", json=body, headers=_headers(src))
    assert r1.status_code == 200 and not r1.json().get("already")
    assert len(_all_items(tgt)) == 1

    # already linked -> no-op, no second item, no re-create
    r2 = client.post(f"/api/items/{pid}/transfer", json=body, headers=_headers(src))
    assert r2.status_code == 200 and r2.json().get("already") is True
    assert len(_all_items(tgt)) == 1

    # simulate a stalled retry: flip the source ref back to 'pending' (as if the tombstone had
    # failed). The operationRef guard must FIND the existing target and not create a second.
    s = _get_item(src, pid)
    op = server._find_transfer_ref(s, "transferred-to")["operationRef"]
    with server.db(src) as c:
        s2 = server._get_item_blob(c, pid)
        server._find_transfer_ref(s2, "transferred-to")["status"] = "pending"
        s2["status"] = "New"
        server._save_project(c, pid, s2)
    r3 = client.post(f"/api/items/{pid}/transfer", json=body, headers=_headers(src))
    assert r3.status_code == 200
    assert len(_all_items(tgt)) == 1, "operationRef guard must prevent a duplicate on retry"
    assert r3.json().get("operationRef") == op          # same operation resumed


def test_guard_is_what_prevents_the_duplicate(client, src):
    # Demonstrate the guard FAILING without its key: strip the operationRef from the target's
    # transferred-from ref (breaking the guard), flip the source back to pending, and re-transfer.
    # With the guard's key gone, a SECOND target item is created - proving the guard is load-bearing.
    tgt = _target_team("tgtguard")
    pid = _make_item(src)
    body = {"targetTeam": tgt, "targetProject": "Websites"}
    client.post(f"/api/items/{pid}/transfer", json=body, headers=_headers(src))
    assert len(_all_items(tgt)) == 1

    # break the guard key on the existing target item
    tgt_items = _all_items(tgt)
    with server.db(tgt) as c:
        row = c.execute("SELECT id, data FROM projects").fetchone()
        d = json.loads(row["data"])
        server._find_transfer_ref(d, "transferred-from")["operationRef"] = "GUARD-BROKEN"
        server._save_project(c, row["id"], d)
    # reset the source to pending so the endpoint proceeds to the create step again
    with server.db(src) as c:
        s2 = server._get_item_blob(c, pid)
        server._find_transfer_ref(s2, "transferred-to")["status"] = "pending"
        s2["status"] = "New"
        server._save_project(c, pid, s2)

    client.post(f"/api/items/{pid}/transfer", json=body, headers=_headers(src))
    assert len(_all_items(tgt)) == 2, "without the operationRef guard a retry duplicates the item"


# ── attachments: copied to the new key space, BEFORE the tombstone ──────────────
def test_transfer_copies_attachments_before_tombstone(client, src, monkeypatch):
    copied = []
    monkeypatch.setattr(server, "_s3_copy", lambda s, d: copied.append((s, d)))
    tgt = _target_team("tgtatt")
    pid = _make_item(src, attachments=[
        {"id": "aaa", "key": "items/999/aaa/shot.png", "name": "shot.png",
         "contentType": "image/png", "size": 12}])
    r = client.post(f"/api/items/{pid}/transfer",
                    json={"targetTeam": tgt, "targetProject": "Websites"}, headers=_headers(src))
    assert r.status_code == 200
    tid = r.json()["targetId"]
    t = _get_item(tgt, tid)
    atts = t.get("attachments") or []
    assert len(atts) == 1
    rec = atts[0]
    assert rec["srcId"] == "aaa"                         # provenance recorded
    assert rec["key"].startswith(f"items/{tid}/")        # copied into the NEW item's key space
    assert rec["key"] != "items/999/aaa/shot.png"
    assert copied and copied[0][0] == "items/999/aaa/shot.png"   # copied FROM the source key
    # source tombstoned only after the copy landed
    assert _get_item(src, pid)["status"] == server.TRANSFERRED_STATUS


def test_failed_attachment_copy_leaves_source_live_and_pending(client, src, monkeypatch):
    def boom(s, d):
        raise RuntimeError("S3 down")
    monkeypatch.setattr(server, "_s3_copy", boom)
    tgt = _target_team("tgtfail")
    pid = _make_item(src, attachments=[
        {"id": "bbb", "key": "items/1/bbb/x.png", "name": "x.png", "contentType": "image/png", "size": 5}])
    r = client.post(f"/api/items/{pid}/transfer",
                    json={"targetTeam": tgt, "targetProject": "Websites"}, headers=_headers(src))
    assert r.status_code == 502                          # copy failed -> transfer did not complete
    # source is NOT tombstoned (attachments confirmed before tombstone); the marker stays 'pending'
    # (the on-item Retry surface reads exactly this - the deferred pending-list endpoint is not used)
    s = _get_item(src, pid)
    assert s["status"] != server.TRANSFERRED_STATUS
    assert server._find_transfer_ref(s, "transferred-to")["status"] == "pending"

    # retry with S3 healthy completes it (idempotent - one target item)
    monkeypatch.setattr(server, "_s3_copy", lambda s, d: None)
    r2 = client.post(f"/api/items/{pid}/transfer",
                     json={"targetTeam": tgt, "targetProject": "Websites"}, headers=_headers(src))
    assert r2.status_code == 200
    assert len(_all_items(tgt)) == 1
    assert _get_item(src, pid)["status"] == server.TRANSFERRED_STATUS


def test_pending_endpoint_is_deferred(client, src):
    # ADDENDUM A: the team-wide pending-list endpoint is deferred (log warning + on-item Retry only).
    assert client.get("/api/transfers/pending", headers=_headers(src)).status_code == 404


# ── ADDENDUM A1: eligibility guard (refuse on accumulated team-specific state) ───
def _add_comment(team, pid, body="looks broken"):
    with server.db(team) as c:
        c.execute("INSERT INTO comments(item_id,author,body,created_ts) VALUES(?,?,?,?)",
                  (pid, "someone", body, "2025-01-01 00:00:00 UTC"))


@pytest.mark.parametrize("field,val", [
    ("sprintId", "s1"),
    ("sprintHistory", [{"sprintId": "s1"}]),
    ("release", "r1"),
    ("storyPoints", 5),
    ("jiraTickets", ["ABC-1"]),
    ("assetLinks", [{"id": "a1"}]),
])
def test_transfer_refuses_on_each_blocker(client, src, field, val):
    tgt = _target_team("tgtblk" + field.lower()[:5])
    pid = _make_item(src, **{field: val})
    r = client.post(f"/api/items/{pid}/transfer",
                    json={"targetTeam": tgt, "targetProject": "Websites"}, headers=_headers(src))
    assert r.status_code == 422
    assert "cannot be transferred" in r.json()["detail"]
    # refused BEFORE any mutation - source untouched, no marker, no target item
    s = _get_item(src, pid)
    assert s["status"] != server.TRANSFERRED_STATUS
    assert server._find_transfer_ref(s, "transferred-to") is None
    assert len(_all_items(tgt)) == 0


def test_transfer_refuses_on_comments_and_names_them(client, src):
    tgt = _target_team("tgtcmt")
    pid = _make_item(src)
    _add_comment(src, pid); _add_comment(src, pid)
    r = client.post(f"/api/items/{pid}/transfer",
                    json={"targetTeam": tgt, "targetProject": "Websites"}, headers=_headers(src))
    assert r.status_code == 422
    assert "2 comments" in r.json()["detail"]            # names what is in the way
    assert server._find_transfer_ref(_get_item(src, pid), "transferred-to") is None


def test_owner_and_assignee_do_not_block_and_are_not_carried(client, src):
    tgt = _target_team("tgtown")
    pid = _make_item(src, dev="Alice", assignee="bob")   # auto-assign state - must NOT block
    r = client.post(f"/api/items/{pid}/transfer",
                    json={"targetTeam": tgt, "targetProject": "Websites"}, headers=_headers(src))
    assert r.status_code == 200
    t = _get_item(tgt, r.json()["targetId"])
    assert "dev" not in t and "assignee" not in t        # dropped, but they never blocked


def test_source_is_carried_not_hardcoded(client, src):
    tgt = _target_team("tgtsrc")
    pid = _make_item(src, source="internal")             # a non-portal origin
    r = client.post(f"/api/items/{pid}/transfer",
                    json={"targetTeam": tgt, "targetProject": "Websites"}, headers=_headers(src))
    assert r.status_code == 200
    assert _get_item(tgt, r.json()["targetId"])["source"] == "internal"   # carried unchanged, not "portal"


def test_transfer_with_no_reporter_email_completes_and_sends_nothing(client, src, monkeypatch):
    sent = []
    monkeypatch.setattr(server, "mail_configured", lambda: True)     # force the email path ON...
    monkeypatch.setattr(server, "send_email", lambda *a, **k: sent.append(a))
    tgt = _target_team("tgtnoem")
    pid = _make_item(src, reporterEmail="", source="internal")
    r = client.post(f"/api/items/{pid}/transfer",
                    json={"targetTeam": tgt, "targetProject": "Websites"}, headers=_headers(src))
    assert r.status_code == 200                           # completes
    assert _get_item(src, pid)["status"] == server.TRANSFERRED_STATUS
    assert sent == []                                     # no email attempted
