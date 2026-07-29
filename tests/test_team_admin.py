"""Team Admin page - server surface.

Covers the config keys the page reads/writes: assethubConnection (mapping), the derived
assethubConfigured + the new assethubCredentialPresent presence bool (so the page can say which
half is missing without ever seeing the key), and enabledViews (per-team view allowlist; null =
all). Rail/route enforcement, the which-half-missing copy, and the toggle UI are frontend and
verified via Chrome MCP. Config writes are admin-only (server-enforced).
"""
import json

import server

KEY = "ahk_aaaaaaaaaaaaaaaa_" + "b" * 52


def _all(client, headers):
    return client.get("/api/all", headers=headers).json()


# ── enabledViews (per-team view visibility) ──────────────────────────────────────

def test_enabled_views_defaults_null_all_enabled(client, team, admin_headers):
    assert _all(client, admin_headers)["enabledViews"] is None   # null = every view enabled


def test_enabled_views_saves_and_reads_back(client, team, admin_headers):
    r = client.put("/api/config/enabledViews", json=["gantt", "list", "dashboard"], headers=admin_headers)
    assert r.status_code == 200
    ev = _all(client, admin_headers)["enabledViews"]
    # the picked views land; the always-on pair is injected by the write-path guard (Item 3)
    assert set(ev) == {"gantt", "list", "dashboard", "my-home", "admin"}


def test_enabled_views_saved_set_persists_not_reseeded(client, team, admin_headers):
    client.put("/api/config/enabledViews", json=["gantt"], headers=admin_headers)
    server.init_team_db(team)                                                # simulate a boot/migration pass
    ev = _all(client, admin_headers)["enabledViews"]
    assert ev is not None and "gantt" in ev                                  # presence-only: the saved allowlist is not reseeded to null


def test_enabled_views_write_injects_always_on_pair(client, team, admin_headers):
    # The exact case that failed live at 5.8.0: PUT ["admin"] must NOT lock the team out.
    client.put("/api/config/enabledViews", json=["admin"], headers=admin_headers)
    ev = _all(client, admin_headers)["enabledViews"]
    assert "my-home" in ev and "admin" in ev            # server injected the always-on pair
    client.put("/api/config/enabledViews", json=["gantt"], headers=admin_headers)
    assert set(_all(client, admin_headers)["enabledViews"]) >= {"gantt", "my-home", "admin"}
    client.put("/api/config/enabledViews", json=[], headers=admin_headers)
    assert set(_all(client, admin_headers)["enabledViews"]) == {"my-home", "admin"}   # empty -> only the floor, no lockout


def test_enabled_views_null_body_rejected_reset_is_via_ui(client, team, admin_headers):
    # 4B.2 decision: a literal null body is REJECTED (422) by the required-body config endpoint.
    # Reset-to-all-enabled is done through the admin UI (enabling every view stores the full
    # allowlist) or by deleting the config key - never a null write. Documented, not a bug.
    h = dict(admin_headers); h["Content-Type"] = "application/json"
    r = client.put("/api/config/enabledViews", content="null", headers=h)
    assert r.status_code == 422


def test_jira_gate_default_true_admin_only_not_reseeded(client, team, admin_headers, editor_headers):
    # Reuses the existing jiraEnabled flag as the per-team Jira permission gate.
    assert client.put("/api/config/jiraEnabled", json=False, headers=editor_headers).status_code in (401, 403)   # non-admin cannot flip it
    assert client.put("/api/config/jiraEnabled", json=False, headers=admin_headers).status_code == 200
    server.init_team_db(team)                                    # a false value is not re-seeded true (presence-only)
    with server.db(team) as c:
        stored = json.loads(c.execute("SELECT value FROM config WHERE key='jiraEnabled'").fetchone()["value"])
    assert stored is False


def test_config_write_is_admin_only(client, team, editor_headers):
    assert client.put("/api/config/enabledViews", json=["gantt"], headers=editor_headers).status_code in (401, 403)
    assert client.put("/api/config/assethubConnection", json={"assethubTeam": "IT"}, headers=editor_headers).status_code in (401, 403)


# ── AssetHub mapping + which-half-missing signals ────────────────────────────────

def test_mapping_saves_and_reads_back(client, team, admin_headers):
    val = {"providerEnvironment": "production", "assethubTeam": "IT"}
    assert client.put("/api/config/assethubConnection", json=val, headers=admin_headers).status_code == 200
    assert _all(client, admin_headers)["assethubConnection"] == val


def test_cleared_mapping_not_reseeded(client, team, admin_headers):
    client.put("/api/config/assethubConnection", json={}, headers=admin_headers)
    server.init_team_db(team)
    assert _all(client, admin_headers)["assethubConnection"] == {}          # presence-only, stays cleared


def test_which_half_missing_signals(client, team, admin_headers, monkeypatch):
    # both absent
    monkeypatch.delenv("ASSETHUB_API_KEY_" + team.upper(), raising=False)
    client.put("/api/config/assethubConnection", json={}, headers=admin_headers)
    a = _all(client, admin_headers)
    assert a["assethubConfigured"] is False and a["assethubCredentialPresent"] is False and a["assethubConnection"] == {}
    # mapping present, credential still absent
    client.put("/api/config/assethubConnection", json={"providerEnvironment": "production", "assethubTeam": "IT"}, headers=admin_headers)
    a = _all(client, admin_headers)
    assert a["assethubConfigured"] is False and a["assethubCredentialPresent"] is False and a["assethubConnection"]
    # both present -> configured
    monkeypatch.setenv("ASSETHUB_API_KEY_" + team.upper(), KEY)
    a = _all(client, admin_headers)
    assert a["assethubConfigured"] is True and a["assethubCredentialPresent"] is True


def test_credential_present_bool_never_leaks_key(client, team, admin_headers, monkeypatch):
    monkeypatch.setenv("ASSETHUB_API_KEY_" + team.upper(), KEY)
    raw = client.get("/api/all", headers=admin_headers).text
    assert '"assethubCredentialPresent": true' in raw or '"assethubCredentialPresent":true' in raw
    assert KEY not in raw and "ahk_" not in raw          # presence yes, the key never
