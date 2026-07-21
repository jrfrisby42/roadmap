"""Planning Stage 1: sprint lifecycle + durable snapshots.

Covers the server surface: the new Discarded state, the snapshot field round-trip,
the enhanced audit (reason/detail passthrough), and the boot-time snapshot backfill
migration (_migrate_sprint_snapshots) - build correctness + idempotency.
"""
import json
import server


def _sprint(**kw):
    s = {"id": "sp1", "name": "Sprint 1", "goal": "", "startDate": "2026-06-01",
         "endDate": "2026-06-15", "state": "Planned", "scope": "global", "carryOver": 0}
    s.update(kw)
    return s


# ── Discarded state ─────────────────────────────────────────────────────────────
def test_discarded_state_accepted(client, team, admin_headers):
    payload = {"sprints": [_sprint(state="Discarded")]}
    assert client.put("/api/sprints", json=payload, headers=admin_headers).status_code == 200
    got = client.get("/api/sprints", headers=admin_headers).json()["sprints"]
    assert got[0]["state"] == "Discarded"


def test_bogus_state_still_rejected(client, team, admin_headers):
    assert client.put("/api/sprints", json={"sprints": [_sprint(state="Nope")]},
                      headers=admin_headers).status_code == 422


# ── Snapshot round-trip ─────────────────────────────────────────────────────────
def test_snapshot_field_round_trips(client, team, admin_headers):
    snap = {"takenAt": "2026-06-15", "items": [
        {"id": 1, "key": "FRAZ-1", "name": "Alpha", "points": 3, "outcome": "completed"},
        {"id": 2, "key": "FRAZ-2", "name": "Beta", "points": 5, "outcome": "carried-over"}]}
    payload = {"sprints": [_sprint(state="Completed", snapshot=snap)]}
    assert client.put("/api/sprints", json=payload, headers=admin_headers).status_code == 200
    got = client.get("/api/sprints", headers=admin_headers).json()["sprints"][0]
    assert got["snapshot"]["items"][1]["outcome"] == "carried-over"
    assert got["snapshot"]["takenAt"] == "2026-06-15"


# ── Enhanced audit ──────────────────────────────────────────────────────────────
def test_audit_reason_detail_passthrough(client, team, admin_headers):
    client.put("/api/sprints",
               json={"sprints": [_sprint(state="Discarded")],
                     "reason": "sprint-discard", "detail": 'Discarded "Sprint 1" (2 items)'},
               headers=admin_headers)
    with server.db(team) as c:
        row = c.execute("SELECT changes FROM audit_log WHERE action='beta:sprints' "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    changes = json.loads(row["changes"])
    assert changes["reason"] == "sprint-discard"
    assert "Discarded" in changes["detail"]


def test_audit_plain_save_has_no_reason(client, team, admin_headers):
    client.put("/api/sprints", json={"sprints": [_sprint()]}, headers=admin_headers)
    with server.db(team) as c:
        row = c.execute("SELECT changes FROM audit_log WHERE action='beta:sprints' "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    changes = json.loads(row["changes"])
    assert changes["count"] == 1 and "reason" not in changes


# ── Snapshot backfill migration ───────────────────────────────────────────────
def _seed_backfill(team):
    sprints = [
        {"id": "sp1", "name": "Sprint 1", "state": "Completed", "endDate": "2026-06-15", "carryOver": 2},
        {"id": "sp2", "name": "Sprint 2", "state": "Planned"},
    ]
    projects = [
        # finished, stayed in sp1 (no sprint-planning activity)
        (1, {"itemKey": "FRAZ-1", "name": "Alpha", "storyPoints": 3, "sprintId": "sp1", "status": "Done"}),
        # carried over from sp1 to sp2
        (2, {"itemKey": "FRAZ-2", "name": "Beta", "storyPoints": 5, "sprintId": "sp2", "status": "In Progress"}),
        # returned to backlog from sp1
        (3, {"itemKey": "FRAZ-3", "name": "Gamma", "storyPoints": 2, "sprintId": "", "status": "Planned"}),
    ]
    acts = [
        (2, "Sprint planning: Sprint 1 completed → moved to Sprint 2", "2026-06-15T10:00:00"),
        (3, "Sprint planning: Sprint 1 completed → returned to backlog", "2026-06-15T10:00:00"),
    ]
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES('sprints',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(sprints),))
        for pid, data in projects:
            c.execute("INSERT INTO projects(id,data) VALUES(?,?)", (pid, json.dumps(data)))
        for item_id, msg, ts in acts:
            c.execute("INSERT INTO activities(activity_type,source,item_id,message,created_ts) "
                      "VALUES('note','System',?,?,?)", (item_id, msg, ts))


def _sp1_snapshot(team):
    with server.db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='sprints'").fetchone()
    sprints = json.loads(row["value"])
    return next(s for s in sprints if s["id"] == "sp1").get("snapshot")


def test_migration_builds_snapshot(team):
    _seed_backfill(team)
    server._migrate_sprint_snapshots(team)
    snap = _sp1_snapshot(team)
    assert snap and snap["reconstructed"] is True
    by_id = {it["id"]: it for it in snap["items"]}
    assert set(by_id) == {1, 2, 3}
    assert by_id[1]["outcome"] == "completed"          # finished, stayed
    assert by_id[2]["outcome"] == "carried-over"       # moved to Sprint 2
    assert by_id[3]["outcome"] == "returned-to-backlog"
    assert by_id[1]["key"] == "FRAZ-1" and by_id[2]["points"] == 5


def test_migration_is_idempotent(team):
    _seed_backfill(team)
    server._migrate_sprint_snapshots(team)
    first = _sp1_snapshot(team)
    server._migrate_sprint_snapshots(team)   # second pass must not rebuild/overwrite
    assert _sp1_snapshot(team) == first


def test_migration_skips_when_snapshot_exists(team):
    # a Completed sprint that already has a (live-captured) snapshot is left untouched
    live = {"takenAt": "2026-06-15", "items": [{"id": 9, "key": "X-9", "name": "keep", "points": 1, "outcome": "completed"}]}
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES('sprints',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (json.dumps([{"id": "sp1", "name": "Sprint 1", "state": "Completed", "snapshot": live}]),))
    server._migrate_sprint_snapshots(team)
    snap = _sp1_snapshot(team)
    assert snap == live and "reconstructed" not in snap
