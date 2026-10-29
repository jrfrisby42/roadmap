"""ITEM-PAGE-PARITY-1: the editor status rules are enforced by update_project for every screen and the API.

- An editor cannot set a Released status (other terminal statuses such as Done are fine). Exception: a
  recurring item whose due date has passed - the client auto-release runs in editors' browsers too.
- An editor cannot move an ACTIVE item that has a Team back to a non-active, non-terminal status; parking
  statuses (Blocked / off-flow / waiting / parked) are exempt, so flagging Blocked still works.
- Admins are unrestricted; contributors keep their own rules (tested elsewhere)."""
import json

import server

CFG = {
    "statuses": ["Backlogged", "Approved", "In Progress", "In Testing", "Blocked", "Waiting",
                 "Released", "Done"],
    "statusIsActive": {"In Progress": True, "In Testing": True},
    "statusIsTerminal": {"Released": True, "Done": True},
    "statusIsReleased": {"Released": True},
    "statusIsBlocked": {"Blocked": True},
    "statusIsWaiting": {"Waiting": True},
}


def _cfg(team):
    with server.db(team) as c:
        for k, v in CFG.items():
            c.execute("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)", (k, json.dumps(v)))


def _item(team, **f):
    with server.db(team) as c:
        return server._insert_project(c, {"name": "Thing", "status": "Approved", "dev": "Pod A", **f})


def _put(client, h, pid, **f):
    r = client.get("/api/all", headers=h)
    p = next(x for x in r.json()["projects"] if x["id"] == pid)
    return client.put(f"/api/projects/{pid}", headers=h, json={**p, **f})


def test_editor_cannot_set_released(client, team, editor_headers):
    _cfg(team)
    pid = _item(team, status="In Testing")
    r = _put(client, editor_headers, pid, status="Released")
    assert r.status_code == 403 and "Released" in r.json()["detail"]


def test_editor_can_close_out_as_done(client, team, editor_headers):
    _cfg(team)
    pid = _item(team, status="In Progress")
    assert _put(client, editor_headers, pid, status="Done").status_code == 200


def test_editor_may_move_between_active_statuses(client, team, editor_headers):
    _cfg(team)
    pid = _item(team, status="In Testing")
    assert _put(client, editor_headers, pid, status="In Progress").status_code == 200


def test_editor_cannot_move_an_active_item_back(client, team, editor_headers):
    _cfg(team)
    pid = _item(team, status="In Progress")
    r = _put(client, editor_headers, pid, status="Approved")
    assert r.status_code == 403 and "back" in r.json()["detail"]


def test_parking_statuses_are_not_a_regression(client, team, editor_headers):
    _cfg(team)
    for target in ("Blocked", "Waiting"):
        pid = _item(team, status="In Progress")
        assert _put(client, editor_headers, pid, status=target).status_code == 200, target


def test_active_item_without_a_team_is_not_locked(client, team, editor_headers):
    _cfg(team)
    pid = _item(team, status="In Progress", dev="")
    assert _put(client, editor_headers, pid, status="Approved").status_code == 200


def test_unchanged_status_and_other_edits_pass(client, team, editor_headers):
    _cfg(team)
    pid = _item(team, status="Released", releaseDate="2026-10-01")   # already released: re-save is fine
    assert _put(client, editor_headers, pid, name="Renamed").status_code == 200


def test_overdue_recurring_item_may_be_auto_released_by_an_editor(client, team, editor_headers):
    _cfg(team)
    pid = _item(team, status="In Progress", recurrence="weekly", due="2020-01-06")
    assert _put(client, editor_headers, pid, status="Released", releaseDate="2020-01-06").status_code == 200
    pid2 = _item(team, status="In Progress", recurrence="weekly", due="2999-01-04")   # not due yet
    assert _put(client, editor_headers, pid2, status="Released").status_code == 403


def test_admin_is_unrestricted(client, team, admin_headers):
    _cfg(team)
    pid = _item(team, status="In Progress")
    assert _put(client, admin_headers, pid, status="Approved").status_code == 200
    assert _put(client, admin_headers, pid, status="Released").status_code == 200
