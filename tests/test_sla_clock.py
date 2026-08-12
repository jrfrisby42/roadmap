"""SLA-3 Stage 2: status-pause SLA clock (basis + pausedSince).

Two forward-only, server-owned item fields drive the pause:
  - basis:       the SLA clock start (fallback createdAt). Advanced past time spent in a waiting
                 status on resume; restarted at the revival timestamp when an item leaves a parked
                 (statusIsParked, A1-1) status.
  - pausedSince: when the CURRENT wait began (set on entering a waiting status, cleared on leave).

Parking keys off statusIsParked (a many-valued map seeded from statusIsDeferred, A1-1), NOT
statusIsDeferred itself. The pure helpers _maintain_sla_clock / _sla_add_delta are unit-tested here;
the PUT integration proves the fields are stamped, are server-owned (a client PUT cannot forge them),
and that each change writes an item-history activity.
"""
import json

import server

WAIT = {"Waiting": True}
PARK = {"Parked": True}
NOW = "2026-08-10T12:00:00+00:00"


# ── _sla_add_delta ───────────────────────────────────────────────────────────────
def test_sla_add_delta_advances_by_interval():
    # base + (to - from): 1 day of wait pushes the basis forward exactly one day.
    got = server._sla_add_delta("2026-08-01T00:00:00+00:00",
                                "2026-08-02T00:00:00+00:00",
                                "2026-08-03T00:00:00+00:00")
    assert got.startswith("2026-08-02T00:00:00")


def test_sla_add_delta_unparseable_returns_base():
    assert server._sla_add_delta("nonsense", NOW, NOW) == "nonsense"


# ── _maintain_sla_clock: transitions ───────────────────────────────────────────────
def test_no_status_change_is_noop():
    old = {"status": "In Progress"}
    merged = {"status": "In Progress", "basis": "2026-08-01T00:00:00+00:00"}
    assert server._maintain_sla_clock(old, merged, WAIT, PARK, NOW) == []
    assert merged["basis"] == "2026-08-01T00:00:00+00:00"


def test_enter_waiting_sets_paused_since():
    old = {"status": "In Progress"}
    merged = {"status": "Waiting"}
    changes = server._maintain_sla_clock(old, merged, WAIT, PARK, NOW)
    assert merged["pausedSince"] == NOW
    assert any(f == "pausedSince" for f, *_ in changes)


def test_resume_from_waiting_advances_basis_and_clears_paused():
    old = {"status": "Waiting"}
    merged = {"status": "In Progress",
              "basis": "2026-08-01T00:00:00+00:00",
              "pausedSince": "2026-08-02T00:00:00+00:00"}   # a full day paused, ending at NOW
    server._maintain_sla_clock(old, merged, WAIT, PARK, NOW)
    # basis advanced by (NOW - pausedSince). NOW is 2026-08-10T12:00 -> delta 8d12h, so
    # basis 08-01 00:00 + 8d12h = 08-09 12:00.
    assert merged["basis"].startswith("2026-08-09T12:00:00")
    assert "pausedSince" not in merged


def test_resume_without_paused_since_does_not_advance_R3():
    old = {"status": "Waiting"}
    merged = {"status": "In Progress", "basis": "2026-08-01T00:00:00+00:00"}   # no pausedSince
    server._maintain_sla_clock(old, merged, WAIT, PARK, NOW)
    assert merged["basis"] == "2026-08-01T00:00:00+00:00"   # unchanged - no write-on-read
    assert "pausedSince" not in merged


def test_close_from_waiting_advances_basis_R2():
    # waiting -> terminal is a "leave waiting" too, so the basis advances past the final wait.
    old = {"status": "Waiting"}
    merged = {"status": "Done",
              "basis": "2026-08-01T00:00:00+00:00",
              "pausedSince": "2026-08-02T00:00:00+00:00"}
    server._maintain_sla_clock(old, merged, WAIT, PARK, NOW)
    assert merged["basis"].startswith("2026-08-09T12:00:00")
    assert "pausedSince" not in merged


def test_enter_parked_clears_paused_and_no_basis_change_T8():
    old = {"status": "Waiting"}
    merged = {"status": "Parked",
              "basis": "2026-08-01T00:00:00+00:00",
              "pausedSince": "2026-08-02T00:00:00+00:00"}
    server._maintain_sla_clock(old, merged, WAIT, PARK, NOW)
    assert "pausedSince" not in merged
    assert merged["basis"] == "2026-08-01T00:00:00+00:00"   # no clock while parked


def test_revive_from_parked_restarts_basis_T8():
    old = {"status": "Parked"}
    merged = {"status": "In Progress", "basis": "2026-08-01T00:00:00+00:00"}
    server._maintain_sla_clock(old, merged, WAIT, PARK, NOW)
    assert merged["basis"] == NOW           # revival restarts the clock (not a resume)
    assert "pausedSince" not in merged


# ── PUT integration: stamped, server-owned, audited ────────────────────────────────
def _seed(client, admin_headers):
    client.put("/api/config/statuses",
               json=["New", "In Progress", "Waiting", "Done"], headers=admin_headers)
    client.put("/api/config/statusIsTerminal", json={"Done": True}, headers=admin_headers)
    client.put("/api/config/statusIsWaiting", json={"Waiting": True}, headers=admin_headers)


def _blob(team, pid):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()["data"])


def test_put_pause_resume_round_trip(client, team, admin_headers):
    _seed(client, admin_headers)
    pid = client.post("/api/projects", json={"name": "T", "status": "New"},
                      headers=admin_headers).json()["id"]
    base = _blob(team, pid)
    # Enter Waiting -> pausedSince stamped.
    r = client.put(f"/api/projects/{pid}", json={**base, "status": "Waiting"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    paused = _blob(team, pid)
    assert paused.get("pausedSince")
    # Resume -> pausedSince cleared, basis present.
    r = client.put(f"/api/projects/{pid}", json={**paused, "status": "In Progress"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    resumed = _blob(team, pid)
    assert "pausedSince" not in resumed
    assert resumed.get("basis")


def test_put_client_cannot_forge_basis(client, team, admin_headers):
    _seed(client, admin_headers)
    pid = client.post("/api/projects", json={"name": "T", "status": "New"},
                      headers=admin_headers).json()["id"]
    base = _blob(team, pid)
    # A client PUT that tries to set basis/pausedSince directly is ignored (server-owned).
    r = client.put(f"/api/projects/{pid}",
                   json={**base, "basis": "2000-01-01T00:00:00+00:00", "pausedSince": "2000-01-01T00:00:00+00:00"},
                   headers=admin_headers)
    assert r.status_code == 200, r.text
    b = _blob(team, pid)
    assert b.get("basis") != "2000-01-01T00:00:00+00:00"
    assert not b.get("pausedSince")


def test_put_pause_writes_history(client, team, admin_headers):
    _seed(client, admin_headers)
    pid = client.post("/api/projects", json={"name": "T", "status": "New"},
                      headers=admin_headers).json()["id"]
    base = _blob(team, pid)
    client.put(f"/api/projects/{pid}", json={**base, "status": "Waiting"}, headers=admin_headers)
    with server.db(team) as c:
        rows = c.execute("SELECT activity_type, message FROM activities WHERE item_id=? AND activity_type='SLA clock'",
                         (pid,)).fetchall()
    assert rows, "expected an SLA clock history activity on pause"
    assert any("pausedSince" in (r["message"] or "") for r in rows)


# ── A1-1: statusIsParked (many-valued), precedence, migration seed ──────────────────
SLA_ON = {"enabled": True, "resolution": {"1": 4}, "atRiskPct": 80}


def test_digest_parked_many_valued_all_unmeasured():
    # A team can park MORE than one status (the whole point of the separate map). Every parked
    # status is unmeasured, regardless of how overdue it looks.
    parked = {"Inactive": True, "Backlogged": True}
    now = server._digest_parse_iso("2026-08-10T12:00:00+00:00")
    for st in ("Inactive", "Backlogged"):
        p = {"priority": "1", "status": st, "createdAt": "2026-08-01T00:00:00+00:00"}
        assert server._digest_sla_kind(p, SLA_ON, {}, now, {}, parked) is None


def test_parked_wins_over_waiting_in_digest():
    # A status flagged BOTH parked and waiting is treated as parked (unmeasured), not paused.
    now = server._digest_parse_iso("2026-08-10T12:00:00+00:00")
    p = {"priority": "1", "status": "Frozen", "createdAt": "2026-08-01T00:00:00+00:00"}
    got = server._digest_sla_kind(p, SLA_ON, {}, now, {"Frozen": True}, {"Frozen": True})
    assert got is None   # parked branch returns before the waiting branch


def test_parked_wins_over_waiting_in_maintain():
    # Entering a both-flagged status parks (clears pausedSince, no basis advance), never sets pausedSince.
    old = {"status": "In Progress"}
    merged = {"status": "Frozen", "basis": "2026-08-01T00:00:00+00:00"}
    server._maintain_sla_clock(old, merged, {"Frozen": True}, {"Frozen": True}, NOW)
    assert "pausedSince" not in merged
    assert merged["basis"] == "2026-08-01T00:00:00+00:00"


def test_migration_seeds_parked_from_deferred(team):
    # statusIsParked is seeded ONCE from statusIsDeferred, with a marker, without touching deferred.
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES('statusIsDeferred',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps({"Inactive": True}),))
        # Clear any prior seed so the migration runs fresh for this assertion.
        c.execute("DELETE FROM config WHERE key IN ('statusIsParked','statusIsParkedSeeded')")
    server._migrated_teams.discard(team)   # _migrate_config_keys runs once per team per process; reset it
    server._migrate_config_keys(team)
    with server.db(team) as c:
        parked = json.loads(c.execute("SELECT value FROM config WHERE key='statusIsParked'").fetchone()["value"])
        marker = c.execute("SELECT value FROM config WHERE key='statusIsParkedSeeded'").fetchone()
        deferred = json.loads(c.execute("SELECT value FROM config WHERE key='statusIsDeferred'").fetchone()["value"])
    assert parked == {"Inactive": True}          # seeded from deferred
    assert marker is not None                    # one-shot marker present
    assert deferred == {"Inactive": True}        # deferred itself untouched


def test_put_park_no_pausedSince_and_revives_basis(client, team, admin_headers):
    # Parking an item stamps no pausedSince; leaving it restarts basis at the revival time.
    client.put("/api/config/statuses", json=["New", "In Progress", "Parked"], headers=admin_headers)
    client.put("/api/config/statusIsParked", json={"Parked": True}, headers=admin_headers)
    pid = client.post("/api/projects", json={"name": "T", "status": "New"},
                      headers=admin_headers).json()["id"]
    base = _blob(team, pid)
    client.put(f"/api/projects/{pid}", json={**base, "status": "Parked"}, headers=admin_headers)
    parked = _blob(team, pid)
    assert not parked.get("pausedSince")
    client.put(f"/api/projects/{pid}", json={**parked, "status": "In Progress"}, headers=admin_headers)
    revived = _blob(team, pid)
    assert revived.get("basis")                  # basis set at revival
