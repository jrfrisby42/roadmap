"""OCC self-conflict fix: add_comment echoes the item's post-write `updated_ts` so the client can
refresh its optimistic-concurrency baseline. Without it, the first non-reporter comment stamps
firstResponseAt (which bumps updated_ts), and the poster's NEXT guarded edit 409s against themselves.

This covers the server enabler only; the client baseline refresh (_syncItemBaseTs, wired into the
three comment-post paths) is frontend JS with no pytest harness.
"""
import server


def _mk(client, admin_headers, **fields):
    r = client.post("/api/projects", json={"name": "Ticket", "status": "New", **fields}, headers=admin_headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _row_updated_ts(team, pid):
    with server.db(team) as c:
        return c.execute("SELECT updated_ts FROM projects WHERE id=?", (pid,)).fetchone()["updated_ts"]


def test_comment_response_echoes_current_item_updated_ts(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    r = client.post("/api/comments", json={"item_id": pid, "body": "hi"}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["item_updated_ts"] == _row_updated_ts(team, pid)   # exactly the row's current stamp


def test_echo_reflects_the_first_response_bump(client, team, admin_headers, editor_headers):
    # A non-reporter comment stamps firstResponseAt -> bumps updated_ts. The echoed value must be the
    # POST-bump stamp (the whole point: the client syncs to this, so the next guarded edit won't 409).
    pid = _mk(client, admin_headers, reporter="reporter_x")   # reporter != commenter (editor)
    before = _row_updated_ts(team, pid)
    r = client.post("/api/comments", json={"item_id": pid, "body": "on it"}, headers=editor_headers)
    assert r.status_code == 200
    echoed = r.json()["item_updated_ts"]
    after = _row_updated_ts(team, pid)
    assert echoed == after                 # echo is the post-write stamp
    assert echoed != before                # and it did move (first-response stamp fired)


def test_echo_matches_a_later_baseline_for_a_guarded_put(client, team, admin_headers, editor_headers):
    # End to end at the API layer: echo the comment's stamp, then send it as the OCC baseline on a
    # status change - the server must accept it (no 409), proving the echoed value is a valid baseline.
    pid = _mk(client, admin_headers, reporter="reporter_x")
    r = client.post("/api/comments", json={"item_id": pid, "body": "on it"}, headers=editor_headers)
    base = r.json()["item_updated_ts"]
    put = client.put(f"/api/projects/{pid}",
                     json={"status": "In Progress", "_baseUpdatedTs": base}, headers=editor_headers)
    assert put.status_code == 200, put.text   # accepted; the stale-baseline 409 is exactly what we fixed


def test_stale_baseline_still_409s(client, team, admin_headers, editor_headers):
    # Guard the guard: the pre-comment stamp is now STALE and must still 409 - proving the fix works by
    # refreshing the baseline, not by weakening optimistic concurrency.
    pid = _mk(client, admin_headers, reporter="reporter_x")
    stale = _row_updated_ts(team, pid)
    client.post("/api/comments", json={"item_id": pid, "body": "on it"}, headers=editor_headers)  # bumps updated_ts
    put = client.put(f"/api/projects/{pid}",
                     json={"status": "In Progress", "_baseUpdatedTs": stale}, headers=editor_headers)
    assert put.status_code == 409
    assert put.json()["detail"]["error"] == "conflict"


def test_echo_present_even_when_no_bump(client, team, admin_headers):
    # A reporter's own comment is NOT a first response, so updated_ts does not move - the echo is still
    # present and equal to the (unchanged) current stamp, so the client sync is a harmless no-op.
    pid = _mk(client, admin_headers)   # reporter defaults to creator (admin)
    before = _row_updated_ts(team, pid)
    r = client.post("/api/comments", json={"item_id": pid, "body": "reporter note"}, headers=admin_headers)
    assert r.json()["item_updated_ts"] == before
