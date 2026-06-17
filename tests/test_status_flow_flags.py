"""Drag-order = authoritative status flow rank (server side of the change).

The /beta flow RANK reads the drag-ordered `statuses` config; terminal (done) and the
readiness-floor (Approved) come from `statusIsTerminal` / `statusIsApproved`. New teams
seed only the terminal anchor that actually exists in the default set ("Released"); the
guarded migration seeds a flag ONLY when unset AND the matching status name exists in the
team's `statuses` — never clobbering custom setups.
"""
import server


def _cfg(client, headers, key):
    return client.get("/api/all", headers=headers).json().get(key)


def _remigrate(team):
    server._migrated_teams.discard(team)
    server._migrate_config_keys(team)


def test_new_team_seeds_terminal_only(client, team, admin_headers):
    # "Released" is in the default statuses → terminal seeded. No "Approved" in the
    # default set → no readiness-floor seed.
    assert _cfg(client, admin_headers, "statusIsTerminal") == {"Released": True}
    assert _cfg(client, admin_headers, "statusIsApproved") == {}


def test_migration_seeds_terminal_for_default_team(client, team, admin_headers):
    with server.db(team) as c:
        c.execute("DELETE FROM config WHERE key IN ('statusIsTerminal','statusIsApproved')")
    _remigrate(team)
    assert _cfg(client, admin_headers, "statusIsTerminal") == {"Released": True}
    assert _cfg(client, admin_headers, "statusIsApproved") == {}   # Approved not a default status


def test_migration_seeds_approved_when_status_present(client, team, admin_headers):
    # A team whose statuses include "Approved" gets the readiness-floor seed.
    client.put("/api/config/statuses",
               json=["Backlog", "Approved", "In Progress", "Released"], headers=admin_headers)
    with server.db(team) as c:
        c.execute("DELETE FROM config WHERE key IN ('statusIsTerminal','statusIsApproved')")
    _remigrate(team)
    assert _cfg(client, admin_headers, "statusIsTerminal") == {"Released": True}
    assert _cfg(client, admin_headers, "statusIsApproved") == {"Approved": True}


def test_migration_skips_when_default_names_absent(client, team, admin_headers):
    client.put("/api/config/statuses", json=["Todo", "Doing", "Done"], headers=admin_headers)
    with server.db(team) as c:
        c.execute("DELETE FROM config WHERE key IN ('statusIsTerminal','statusIsApproved')")
    _remigrate(team)
    assert _cfg(client, admin_headers, "statusIsTerminal") == {}
    assert _cfg(client, admin_headers, "statusIsApproved") == {}


def test_migration_does_not_clobber_existing(client, team, admin_headers):
    client.put("/api/config/statusIsTerminal", json={"Done": True}, headers=admin_headers)
    client.put("/api/config/statusIsApproved", json={"Ready": True}, headers=admin_headers)
    _remigrate(team)
    assert _cfg(client, admin_headers, "statusIsTerminal") == {"Done": True}
    assert _cfg(client, admin_headers, "statusIsApproved") == {"Ready": True}
