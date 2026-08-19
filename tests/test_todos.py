"""TODO-1: private per-user personal to-dos (My Home "To-dos" tab).

Privacy is the requirement, not a detail. The load-bearing guard is
test_privacy_todos_are_per_user: one user's to-dos are never returned to or reachable by another user,
and a cross-user PUT/DELETE/clear-completed is 404 (existence not confirmed), never 403. It is
demonstrably falsifiable - drop the `username` from a write's WHERE clause and the 404 assertions fail
(see the deliverable's revert demo).
"""
import os

import server


def _hdr(team, user, role="editor"):
    return {"Authorization": f"Bearer {server.create_token(team, user, role)}", "X-Team": team}


def _mk(client, hdr, title="Call the vendor", **fields):
    return client.post("/api/my/todos", json={"title": title, **fields}, headers=hdr)


def _ids(client, hdr):
    return {t["id"] for t in client.get("/api/my/todos", headers=hdr).json()["todos"]}


# ── THE PRIVACY GUARD ───────────────────────────────────────────────────────────────────────────────
def test_privacy_todos_are_per_user(client, team):
    alice, bob = _hdr(team, "alice"), _hdr(team, "bob")
    a = _mk(client, alice, title="Alice secret").json()["todo"]["id"]
    _mk(client, bob, title="Bob thing")
    # Bob's list never contains Alice's row.
    assert a not in _ids(client, bob)
    assert "Alice secret" not in {t["title"] for t in client.get("/api/my/todos", headers=bob).json()["todos"]}
    # Cross-user writes are 404 (NOT 403 - never confirm the row exists).
    assert client.put(f"/api/my/todos/{a}", json={"title": "hax"}, headers=bob).status_code == 404
    assert client.delete(f"/api/my/todos/{a}", headers=bob).status_code == 404
    # ...and Alice's row is untouched by any of it.
    assert a in _ids(client, alice)
    # TODO-2A: the completed-log filter is not a privacy hole - Bob's ?status=done never sees Alice's
    # completed row (username stays in the WHERE regardless of the filter).
    client.put(f"/api/my/todos/{a}", json={"status": "Done"}, headers=alice)
    bob_done = client.get("/api/my/todos?status=done", headers=bob).json()["todos"]
    assert all(t["id"] != a for t in bob_done)
    assert a in {t["id"] for t in client.get("/api/my/todos?status=done", headers=alice).json()["todos"]}


def test_username_from_token_not_body(client, team):
    # A client-supplied username is IGNORED, not honored: the row is owned by the token's user.
    alice = _hdr(team, "alice")
    t = client.post("/api/my/todos", json={"title": "x", "username": "bob"}, headers=alice).json()["todo"]
    assert t["username"] == "alice"


# ── CRUD ─────────────────────────────────────────────────────────────────────────────────────────────
def test_create_list_rename_complete_uncomplete_delete(client, team):
    h = _hdr(team, "alice")
    tid = _mk(client, h, title="Draft email").json()["todo"]["id"]
    assert {t["title"] for t in client.get("/api/my/todos", headers=h).json()["todos"]} == {"Draft email"}
    # rename
    assert client.put(f"/api/my/todos/{tid}", json={"title": "Send email"}, headers=h).json()["todo"]["title"] == "Send email"
    # complete stamps completed_ts + status Done
    done = client.put(f"/api/my/todos/{tid}", json={"status": "Done"}, headers=h).json()["todo"]
    assert done["status"] == "Done" and done["completed_ts"]
    # un-complete clears completed_ts
    reopened = client.put(f"/api/my/todos/{tid}", json={"status": "Today"}, headers=h).json()["todo"]
    assert reopened["status"] == "Today" and reopened["completed_ts"] is None
    # delete removes it
    assert client.delete(f"/api/my/todos/{tid}", headers=h).status_code == 200
    assert client.get("/api/my/todos", headers=h).json()["todos"] == []


def test_new_todo_defaults_to_today(client, team):
    h = _hdr(team, "alice")
    assert _mk(client, h).json()["todo"]["status"] == "Today"


# ── VALIDATION ─────────────────────────────────────────────────────────────────────────────────────
def test_empty_or_whitespace_title_rejected(client, team):
    h = _hdr(team, "alice")
    assert client.post("/api/my/todos", json={"title": ""}, headers=h).status_code == 400
    assert client.post("/api/my/todos", json={"title": "   "}, headers=h).status_code == 400
    tid = _mk(client, h).json()["todo"]["id"]
    assert client.put(f"/api/my/todos/{tid}", json={"title": "  "}, headers=h).status_code == 400


def test_invalid_status_rejected_on_post_and_put(client, team):
    h = _hdr(team, "alice")
    assert client.post("/api/my/todos", json={"title": "x", "status": "In Progress"}, headers=h).status_code == 400
    tid = _mk(client, h).json()["todo"]["id"]
    assert client.put(f"/api/my/todos/{tid}", json={"status": "Nope"}, headers=h).status_code == 400
    # every real TODO_STATUS is accepted
    for s in server.TODO_STATUSES:
        assert client.put(f"/api/my/todos/{tid}", json={"status": s}, headers=h).status_code == 200


def test_cross_user_write_is_404_not_403(client, team):
    a = _mk(client, _hdr(team, "alice")).json()["todo"]["id"]
    bob = _hdr(team, "bob")
    assert client.put(f"/api/my/todos/{a}", json={"title": "x"}, headers=bob).status_code == 404
    assert client.delete(f"/api/my/todos/{a}", headers=bob).status_code == 404


# ── DUE DATE (incl the clear path) ───────────────────────────────────────────────────────────────────
def test_due_date_set_change_clear_and_malformed(client, team):
    h = _hdr(team, "alice")
    tid = _mk(client, h).json()["todo"]["id"]
    assert client.put(f"/api/my/todos/{tid}", json={"due_date": "2026-08-20"}, headers=h).json()["todo"]["due_date"] == "2026-08-20"
    assert client.put(f"/api/my/todos/{tid}", json={"due_date": "2026-09-01"}, headers=h).json()["todo"]["due_date"] == "2026-09-01"
    # clear via empty string
    assert client.put(f"/api/my/todos/{tid}", json={"due_date": ""}, headers=h).json()["todo"]["due_date"] is None
    # set again, clear via null
    client.put(f"/api/my/todos/{tid}", json={"due_date": "2026-08-20"}, headers=h)
    assert client.put(f"/api/my/todos/{tid}", json={"due_date": None}, headers=h).json()["todo"]["due_date"] is None
    # malformed -> 400 (POST and PUT)
    assert client.post("/api/my/todos", json={"title": "x", "due_date": "20/08/2026"}, headers=h).status_code == 400
    assert client.put(f"/api/my/todos/{tid}", json={"due_date": "not-a-date"}, headers=h).status_code == 400


# ── TODO-2A: status filter + counts + no destructive clear endpoint ──────────────────────────────────
def test_status_filter_open_done_and_counts(client, team):
    h = _hdr(team, "alice")
    o1 = _mk(client, h, title="open-1").json()["todo"]["id"]
    _mk(client, h, title="open-2")
    d1 = _mk(client, h, title="done-1").json()["todo"]["id"]
    client.put(f"/api/my/todos/{d1}", json={"status": "Done"}, headers=h)
    # ?status=open -> only non-Done; ?status=done -> only Done; counts always carry both.
    open_res = client.get("/api/my/todos?status=open", headers=h).json()
    assert {t["title"] for t in open_res["todos"]} == {"open-1", "open-2"}
    assert open_res["counts"] == {"open": 2, "done": 1}
    done_res = client.get("/api/my/todos?status=done", headers=h).json()
    assert {t["title"] for t in done_res["todos"]} == {"done-1"}
    assert done_res["counts"] == {"open": 2, "done": 1}
    # completed log is newest-completed first
    d2 = _mk(client, h, title="done-2").json()["todo"]["id"]
    client.put(f"/api/my/todos/{d2}", json={"status": "Done"}, headers=h)
    done_titles = [t["title"] for t in client.get("/api/my/todos?status=done", headers=h).json()["todos"]]
    assert done_titles[0] == "done-2"   # most recently completed first
    _ = o1


def test_clear_completed_endpoint_removed(client, team):
    # Option A: the hard-delete-all endpoint is gone (single-click hard delete no longer reachable).
    h = _hdr(team, "alice")
    assert client.post("/api/my/todos/clear-completed", headers=h).status_code in (404, 405)


# ── MIGRATION IDEMPOTENCY ────────────────────────────────────────────────────────────────────────────
def test_migration_idempotent(client, team):
    # init_team_db already ran (fixture). Running it again on an existing DB must not raise or wipe rows.
    tid = _mk(client, _hdr(team, "alice")).json()["todo"]["id"]
    server.init_team_db(team)
    server.init_team_db(team)
    assert tid in _ids(client, _hdr(team, "alice"))


# ── EXPORT EXCLUSION ─────────────────────────────────────────────────────────────────────────────────
def test_export_excludes_todos(client, team, admin_headers):
    _mk(client, _hdr(team, "alice"), title="PRIVATE-TODO")
    dump = client.get("/api/export", headers=admin_headers).json()
    assert "todos" not in dump
    assert "PRIVATE-TODO" not in server.json.dumps(dump)


def test_todos_require_auth(client, team):
    assert client.get("/api/my/todos").status_code == 401


# ── REM-2: item link (Reminder to-dos) ────────────────────────────────────────────────────────────────
def _mk_item(client, admin_hdr, name="Linkable"):
    r = client.post("/api/projects", json={"name": name}, headers=admin_hdr)
    assert r.status_code in (200, 201), r.text
    it = r.json()
    return it["id"], it.get("itemKey")


def _set_item_key(team, iid, key):
    with server.db(team) as c:
        r = c.execute("SELECT data FROM projects WHERE id=?", (iid,)).fetchone()
        d = server.json.loads(r["data"]); d["itemKey"] = key
        c.execute("UPDATE projects SET data=? WHERE id=?", (server.json.dumps(d), iid))


def test_reminder_link_stores_id_and_derives_key(client, team, admin_headers):
    # item_key is DERIVED server-side from the item, NEVER trusted from the client.
    iid, _ = _mk_item(client, admin_headers, name="Support ticket")
    _set_item_key(team, iid, "TEST-42")
    h = _hdr(team, "alice")
    t = client.post("/api/my/todos",
                    json={"title": "Follow up", "item_id": iid, "item_key": "BOGUS-999"},
                    headers=h).json()["todo"]
    assert t["item_id"] == iid
    assert t["item_key"] == "TEST-42"          # derived from the item
    assert t["item_key"] != "BOGUS-999"        # client value ignored
    # returned on read in every filter mode
    assert client.get("/api/my/todos", headers=h).json()["todos"][0]["item_key"] == "TEST-42"
    assert client.get("/api/my/todos?status=open", headers=h).json()["todos"][0]["item_key"] == "TEST-42"


def test_reminder_null_link_is_plain_todo(client, team):
    h = _hdr(team, "alice")
    t = client.post("/api/my/todos", json={"title": "just a todo"}, headers=h).json()["todo"]
    assert t["item_id"] is None and t["item_key"] is None
    t2 = client.post("/api/my/todos", json={"title": "x", "item_id": None}, headers=h).json()["todo"]
    assert t2["item_id"] is None and t2["item_key"] is None
    t3 = client.post("/api/my/todos", json={"title": "y", "item_id": ""}, headers=h).json()["todo"]
    assert t3["item_id"] is None and t3["item_key"] is None


def test_reminder_cross_team_item_id_rejected(client, team, admin_headers):
    # An item that exists in ANOTHER team must not be linkable from this team (per-team DB: not found here).
    other = "remxteam"
    os.makedirs(os.path.join(server.TENANTS_DIR, other), exist_ok=True)
    server.init_team_db(other)
    other_admin = {"Authorization": f"Bearer {server.create_token(other, 'admin', 'admin')}", "X-Team": other}
    oid, _ = _mk_item(client, other_admin, name="Other team item")
    h = _hdr(team, "alice")
    assert client.post("/api/my/todos", json={"title": "hax", "item_id": oid}, headers=h).status_code == 400
    # non-numeric id is rejected too
    assert client.post("/api/my/todos", json={"title": "x", "item_id": "abc"}, headers=h).status_code == 400


def test_reminder_writes_zero_activities(client, team, admin_headers):
    iid, _ = _mk_item(client, admin_headers, name="No activity please")
    h = _hdr(team, "alice")
    def _acts():
        r = client.get("/api/activities", headers=admin_headers).json()
        return r if isinstance(r, list) else r.get("activities", [])
    before = _acts()
    client.post("/api/my/todos", json={"title": "Follow up", "item_id": iid}, headers=h)
    after = _acts()
    assert len(after) == len(before)                                   # the reminder created NO activity
    assert all((a.get("activity_type") != "Reminder") for a in after)  # and certainly not a "Reminder" one
