"""Optional Blocked status — Stage A (config source of truth, behavior-neutral).

`statusIsBlocked` is a single-select { statusName: bool } config like statusIsTesting/
statusIsReleased. Seeds empty (= feature off). Behavior bindings are Stage B.
"""
import server


def _cfg(client, headers, key):
    return client.get("/api/all", headers=headers).json().get(key)


def test_status_is_blocked_in_valid_keys():
    assert "statusIsBlocked" in server.VALID_KEYS


def test_new_team_seeds_blocked_empty(client, team, admin_headers):
    assert _cfg(client, admin_headers, "statusIsBlocked") == {}   # off by default


def test_put_persists_single_select(client, team, admin_headers):
    r = client.put("/api/config/statusIsBlocked", json={"Blocked": True}, headers=admin_headers)
    assert r.status_code == 200
    assert _cfg(client, admin_headers, "statusIsBlocked") == {"Blocked": True}


def test_put_requires_admin(client, team, editor_headers):
    assert client.put("/api/config/statusIsBlocked", json={"Blocked": True},
                      headers=editor_headers).status_code == 403


# ── Stage B: server-side status→flag binding (update_project) ──────────────────

def _mk(client, headers, **f):
    return client.post("/api/projects", json={"name": "Item", "status": "Planned", **f},
                       headers=headers).json()


def _flag(team, pid):
    with server.db(team) as c:
        c.execute("INSERT INTO activities(activity_type,item_id,status,source,created_ts) "
                  "VALUES('Blocked',?,'Open','User','2026-01-01 00:00:00 UTC')", (pid,))


def _blocked_statuses(team, pid):
    with server.db(team) as c:
        return [r[0] for r in c.execute(
            "SELECT status FROM activities WHERE item_id=? AND activity_type='Blocked'", (pid,)).fetchall()]


def test_leaving_blocked_clears_flag_and_stash(client, team, admin_headers):
    client.put("/api/config/statusIsBlocked", json={"Blocked": True}, headers=admin_headers)
    pid = _mk(client, admin_headers, status="Blocked", preBlockStatus="In Progress")["id"]
    _flag(team, pid)
    assert _blocked_statuses(team, pid) == ["Open"]
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Item", "status": "In Progress", "preBlockStatus": "In Progress"},
                   headers=admin_headers)
    assert r.status_code == 200
    assert _blocked_statuses(team, pid) == ["Auto-Cleared"]   # flag auto-cleared
    assert "preBlockStatus" not in r.json()                   # stash stripped


def test_staying_blocked_keeps_flag_and_stash(client, team, admin_headers):
    client.put("/api/config/statusIsBlocked", json={"Blocked": True}, headers=admin_headers)
    pid = _mk(client, admin_headers, status="Blocked")["id"]
    _flag(team, pid)
    r = client.put(f"/api/projects/{pid}",
                   json={"name": "Renamed", "status": "Blocked", "preBlockStatus": "Planned"},
                   headers=admin_headers)
    assert _blocked_statuses(team, pid) == ["Open"]           # not cleared
    assert r.json().get("preBlockStatus") == "Planned"        # stash kept


def test_no_blocked_status_no_clear(client, team, admin_headers):
    # statusIsBlocked empty (default) → moving status never touches Blocked activities
    pid = _mk(client, admin_headers, status="Blocked")["id"]
    _flag(team, pid)
    client.put(f"/api/projects/{pid}", json={"name": "Item", "status": "In Progress"},
               headers=admin_headers)
    assert _blocked_statuses(team, pid) == ["Open"]           # unchanged (feature off)

