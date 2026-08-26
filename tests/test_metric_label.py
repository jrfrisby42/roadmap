"""METRIC-LABEL-1: team-defined display label for the numeric metric.

`metricLabel` is a per-team string config (default "Story Points") that renames the storyPoints
metric across the UI. The item field key `storyPoints` is unchanged - only the presentation is
configurable. These tests cover the server surface: VALID_KEYS, the default, the /api/all return,
persistence, and - the re-seed finding - that a populated custom label survives a restart while an
empty/missing value falls back to the default.
"""
import json

import server


def _cfg(client, headers, key):
    return client.get("/api/all", headers=headers).json().get(key)


def test_metric_label_in_valid_keys():
    assert "metricLabel" in server.VALID_KEYS


def test_new_team_defaults_to_story_points(client, team, admin_headers):
    assert _cfg(client, admin_headers, "metricLabel") == "Story Points"


def test_put_persists_custom_label(client, team, admin_headers):
    r = client.put("/api/config/metricLabel", json="Time Spent (hrs)", headers=admin_headers)
    assert r.status_code == 200
    assert _cfg(client, admin_headers, "metricLabel") == "Time Spent (hrs)"


def test_put_requires_admin(client, team, editor_headers):
    assert client.put("/api/config/metricLabel", json="Effort",
                      headers=editor_headers).status_code == 403


def test_custom_label_survives_restart(client, team, admin_headers):
    # The re-seed finding: metricLabel is NOT presence-only, but a non-empty label is truthy, so the
    # falsy-treated-as-missing re-seed never fires on it - a custom label is preserved across a boot.
    client.put("/api/config/metricLabel", json="Time Spent (hrs)", headers=admin_headers)
    server._migrated_teams.discard(team)
    server._migrate_config_keys(team)
    assert _cfg(client, admin_headers, "metricLabel") == "Time Spent (hrs)"


def test_empty_label_falls_back_to_default_in_api_all(client, team, admin_headers):
    # An empty string is a meaningless label; /api/all coalesces it to the default so the client
    # never renders a blank column header.
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES('metricLabel',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(""),))
    assert _cfg(client, admin_headers, "metricLabel") == "Story Points"


def test_missing_key_seeded_default_on_migrate(client, team, admin_headers):
    with server.db(team) as c:
        c.execute("DELETE FROM config WHERE key='metricLabel'")
    server._migrated_teams.discard(team)
    server._migrate_config_keys(team)
    with server.db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='metricLabel'").fetchone()
    assert row and json.loads(row["value"]) == "Story Points"
