"""`richTextEditor` config flag — the /beta Tiptap editor master switch (Stage 1).

Boolean, default ON. Single toggle reverts Description (and later Comments) to the
classic lightweight editor with no redeploy. Presence-only seed: an admin's explicit
False is never clobbered by the migration. Admin-gated like every config key.
"""
import json

import server


def _cfg(client, headers, key):
    return client.get("/api/all", headers=headers).json().get(key)


def _remigrate(team):
    server._migrated_teams.discard(team)
    server._migrate_config_keys(team)


def test_rich_text_editor_in_valid_keys():
    assert "richTextEditor" in server.VALID_KEYS


def test_new_team_defaults_on(client, team, admin_headers):
    assert _cfg(client, admin_headers, "richTextEditor") is True


def test_api_all_exposes_bool(client, team, admin_headers):
    assert isinstance(_cfg(client, admin_headers, "richTextEditor"), bool)


def test_put_persists_and_round_trips(client, team, admin_headers):
    assert client.put("/api/config/richTextEditor", json=False,
                      headers=admin_headers).status_code == 200
    assert _cfg(client, admin_headers, "richTextEditor") is False
    assert client.put("/api/config/richTextEditor", json=True,
                      headers=admin_headers).status_code == 200
    assert _cfg(client, admin_headers, "richTextEditor") is True


def test_put_requires_admin(client, team, editor_headers):
    assert client.put("/api/config/richTextEditor", json=False,
                      headers=editor_headers).status_code == 403


def test_migration_seeds_when_missing(client, team, admin_headers):
    with server.db(team) as c:
        c.execute("DELETE FROM config WHERE key='richTextEditor'")
    _remigrate(team)
    assert _cfg(client, admin_headers, "richTextEditor") is True


def test_migration_does_not_clobber_explicit_false(client, team, admin_headers):
    # An admin turned the editor OFF — re-running the migration must NOT turn it back on
    # (presence-only key: seed only when absent).
    client.put("/api/config/richTextEditor", json=False, headers=admin_headers)
    _remigrate(team)
    assert _cfg(client, admin_headers, "richTextEditor") is False
