"""ROLLUP-SYNC-1: an unscoped parent (children, no own start+due) gets its roll-up WRITTEN.

Rule A (the Jira convention): any child started -> the first active status; every child finished -> the
least-progressed finished status (+ releaseDate / completedAt). Forward-only, bottom-up, team only when
empty, scoped parents untouched, reserved statuses ignored."""
import server

STS = ["New", "Approved", "In Progress", "In Testing", "Released", "Done"]


def _setup(client, h):
    client.put("/api/config/statuses", json=STS, headers=h)
    client.put("/api/config/statusIsActive", json={"In Progress": True, "In Testing": True}, headers=h)
    client.put("/api/config/statusIsTerminal", json={"Released": True, "Done": True}, headers=h)
    client.put("/api/config/statusIsReleased", json={"Released": True}, headers=h)
    client.put("/api/config/statusIsDefault", json={"New": True}, headers=h)
    client.put("/api/config/developers", json=["Wasatch", "Everest"], headers=h)


def _mk(client, h, **f):
    r = client.post("/api/projects", json={"name": f.pop("name", "x"), **f}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _get(client, h, pid):
    return next(p for p in client.get("/api/all", headers=h).json()["projects"] if p["id"] == pid)


def _set(client, h, pid, **f):
    cur = _get(client, h, pid); cur.update(f)
    r = client.put(f"/api/projects/{pid}", json=cur, headers=h)
    assert r.status_code == 200, r.text


def test_any_child_started_moves_parent_in_progress_and_sets_team(client, admin_headers):
    h = admin_headers; _setup(client, h)
    par = _mk(client, h, name="Epic", status="Approved")
    a = _mk(client, h, name="A", status="New", dev="Wasatch", parent=par)
    _mk(client, h, name="B", status="New", dev="Wasatch", parent=par)
    assert _get(client, h, par)["status"] == "Approved"
    _set(client, h, a, status="In Testing")
    p = _get(client, h, par)
    assert p["status"] == "In Progress" and p["dev"] == "Wasatch"
    assert not p.get("completedAt")


def test_all_children_finished_finishes_parent_with_dates(client, admin_headers):
    h = admin_headers; _setup(client, h)
    par = _mk(client, h, name="Epic", status="Approved")
    a = _mk(client, h, name="A", status="In Progress", dev="Everest", parent=par)
    b = _mk(client, h, name="B", status="In Progress", dev="Everest", parent=par)
    _set(client, h, a, status="Released", releaseDate="2026-09-10")
    assert _get(client, h, par)["status"] == "In Progress"           # one still open
    _set(client, h, b, status="Released", releaseDate="2026-09-25")
    p = _get(client, h, par)
    assert p["status"] == "Released" and p["releaseDate"] == "2026-09-25" and p.get("completedAt")


def test_mixed_finished_takes_least_progressed_finished_status(client, admin_headers):
    h = admin_headers; _setup(client, h)
    par = _mk(client, h, name="Epic", status="New")
    _mk(client, h, name="B", status="Released", releaseDate="2026-09-01", parent=par)
    _mk(client, h, name="A", status="Done", parent=par)   # Done ranks after Released: the parent stays at Released
    server._rollup_sync(server_team(client, h))
    assert _get(client, h, par)["status"] == "Released"


def test_forward_only_never_regresses(client, admin_headers):
    h = admin_headers; _setup(client, h)
    par = _mk(client, h, name="Epic", status="In Testing")
    _mk(client, h, name="A", status="In Progress", parent=par)
    server._rollup_sync(server_team(client, h))
    assert _get(client, h, par)["status"] == "In Testing"


def test_explicit_team_is_kept(client, admin_headers):
    h = admin_headers; _setup(client, h)
    par = _mk(client, h, name="Epic", status="New", dev="Everest")
    _mk(client, h, name="A", status="In Progress", dev="Wasatch", parent=par)
    server._rollup_sync(server_team(client, h))
    assert _get(client, h, par)["dev"] == "Everest"


def test_scoped_parent_with_own_dates_is_untouched(client, admin_headers):
    h = admin_headers; _setup(client, h)
    par = _mk(client, h, name="Epic", status="New", start="2026-10-05", due="2026-11-02", dueWeeks=4)
    _mk(client, h, name="A", status="Released", releaseDate="2026-10-10", dev="Wasatch", parent=par)
    server._rollup_sync(server_team(client, h))
    p = _get(client, h, par)
    assert p["status"] == "New" and not p.get("dev")


def test_nested_rolls_up_bottom_up(client, admin_headers):
    h = admin_headers; _setup(client, h)
    ri = _mk(client, h, name="Roadmap Item", status="New")
    ep = _mk(client, h, name="Epic", status="New", parent=ri)
    _mk(client, h, name="Story", status="Released", releaseDate="2026-09-30", dev="Wasatch", parent=ep)
    server._rollup_sync(server_team(client, h))
    assert _get(client, h, ep)["status"] == "Released"
    r = _get(client, h, ri)
    assert r["status"] == "Released" and r["dev"] == "Wasatch" and r["releaseDate"] == "2026-09-30"


def test_reserved_children_are_ignored(client, admin_headers):
    h = admin_headers; _setup(client, h)
    par = _mk(client, h, name="Epic", status="New")
    _mk(client, h, name="A", status="New", parent=par)
    dup = _mk(client, h, name="Dup", status="New", parent=par)
    with server.db(server_team(client, h)) as c:   # Duplicate is a reserved, non-selectable status
        c.execute("UPDATE projects SET data=json_set(data,'$.status','Duplicate') WHERE id=?", (dup,))
    server._rollup_sync(server_team(client, h))
    assert _get(client, h, par)["status"] == "New"                   # a Duplicate is not "finished work"


def test_dry_run_endpoint_reports_without_writing_and_is_admin_only(client, admin_headers, editor_headers):
    h = admin_headers; _setup(client, h)
    par = _mk(client, h, name="Epic", status="New")
    _mk(client, h, name="A", status="Released", releaseDate="2026-09-01", parent=par)
    with server.db(server_team(client, h)) as c:   # undo the create-hook sync to exercise the endpoint
        c.execute("UPDATE projects SET data=json_set(data,'$.status','New','$.releaseDate','','$.completedAt','') WHERE id=?", (par,))
    r = client.post("/api/rollup/sync", json={"dryRun": True}, headers=h).json()
    assert r["dryRun"] and r["changed"] == 1 and r["changes"][0]["fields"]["status"]["to"] == "Released"
    assert _get(client, h, par)["status"] == "New"
    assert client.post("/api/rollup/sync", json={}, headers=editor_headers).status_code == 403
    assert client.post("/api/rollup/sync", json={}, headers=h).json()["changed"] == 1
    assert _get(client, h, par)["status"] == "Released"


def server_team(client, h):
    return h["X-Team"]
