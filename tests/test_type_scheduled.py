"""Per-type "Scheduled" flag — Stage 1 (config source of truth, behavior-neutral).

`typeScheduled` = { typeName: bool } — sibling of `typeIgnoreConflicts`. Defaults
ON for every type (new teams seed it; existing teams get it seeded to
{every type: True}); read through isScheduledType() on the client (missing = True).
This stage only asserts the server contract; no consumer reads it yet.
"""
import json

import server


def _config(client, headers, key):
    return client.get("/api/all", headers=headers).json().get(key)


def test_type_scheduled_in_valid_keys():
    assert "typeScheduled" in server.VALID_KEYS


def test_new_team_defaults_all_types_scheduled(client, team, admin_headers):
    # init_team_db seeds the three default types ON.
    ts = _config(client, admin_headers, "typeScheduled")
    assert ts == {"Feature": True, "Enhancement": True, "Maintenance": True}


def test_api_all_exposes_type_scheduled(client, team, admin_headers):
    assert isinstance(_config(client, admin_headers, "typeScheduled"), dict)


def test_put_persists_and_round_trips(client, team, admin_headers):
    new_map = {"Feature": True, "Enhancement": True, "Maintenance": False}
    r = client.put("/api/config/typeScheduled", json=new_map, headers=admin_headers)
    assert r.status_code == 200
    assert _config(client, admin_headers, "typeScheduled") == new_map


def test_put_requires_admin(client, team, editor_headers):
    assert client.put("/api/config/typeScheduled", json={"Feature": False},
                      headers=editor_headers).status_code == 403


def test_migration_seeds_existing_team_from_current_types(client, team, admin_headers):
    # Simulate a team created before the key existed: drop it, then re-run the migration.
    with server.db(team) as c:
        c.execute("DELETE FROM config WHERE key='typeScheduled'")
    server._migrated_teams.discard(team)
    server._migrate_config_keys(team)
    with server.db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='typeScheduled'").fetchone()
    seeded = json.loads(row["value"])
    assert seeded == {"Feature": True, "Enhancement": True, "Maintenance": True}


def test_migration_does_not_overwrite_existing_choices(client, team, admin_headers):
    # An admin already unchecked a type — re-running the migration must not clobber it.
    client.put("/api/config/typeScheduled",
               json={"Feature": True, "Enhancement": False, "Maintenance": True},
               headers=admin_headers)
    server._migrated_teams.discard(team)
    server._migrate_config_keys(team)
    assert _config(client, admin_headers, "typeScheduled") == \
        {"Feature": True, "Enhancement": False, "Maintenance": True}
