"""JIRA-PULL-1 Stage 1 - config + admin, ships inert.

Stage 1 lands every config key, the admin surface, the assignee-map seeding helpers, and the pull-only
status map, WITHOUT anything pulling. These tests assert the keys exist and are readable, the floor is
set-once (never rewound, not writable via the generic PUT), the per-type/assignee keys are admin-editable,
the three pull-status mappings live in a PULL-ONLY map (not jiraStatusMapping, so the existing linked-item
sync is unchanged), and the pure seed helpers behave. No pull runs; nothing is created.
"""
import server


def _all(client, admin_headers):
    return client.get("/api/all", headers=admin_headers).json()


# ── 1.1 / config keys present + readable, defaults correct ────────────────────────────────────────────
def test_pull_config_keys_present_and_default(client, team, admin_headers):
    d = _all(client, admin_headers)
    assert d["jiraPullFloor"] == ""                 # not set = no pull
    assert d["jiraPullTypes"] == []                 # nothing selected = inert
    assert d["jiraAssigneeMap"] == {}
    # 1.4: the three status mappings live here (pull-only), NOT in jiraStatusMapping
    assert d["jiraPullStatusMap"] == {
        "Code Review": "In Progress", "QA Approved": "In Testing", "Released": "Released",
    }


# ── 1.4 invariant: the existing status resolver / push map is untouched ────────────────────────────────
def test_existing_status_resolver_does_not_gain_the_three(client, team, admin_headers):
    # Configure a realistic forward map like development's, then confirm the EXISTING reverse resolver
    # (used by the linked-item sync + push) still does NOT know Code Review / QA Approved / (Jira) Released.
    fwd = {"In Progress": "In Dev", "In Testing": "In QA", "Released": "In Prod", "New": "NEW"}
    client.put("/api/config/jiraStatusMapping", json=fwd, headers=admin_headers)
    assert server._jira_status_to_roadmap("Code Review", team) is None
    assert server._jira_status_to_roadmap("QA Approved", team) is None
    assert server._jira_status_to_roadmap("Released", team) is None      # Jira "Released" still unmapped for the existing sync
    assert server._jira_status_to_roadmap("In Prod", team) == "Released" # In Prod -> Released, unchanged
    # and jiraStatusMapping was not mutated to carry the three
    d = _all(client, admin_headers)
    assert d["jiraStatusMapping"] == fwd


# ── 1.1 the floor is set-once (never rewound, never advanced) ──────────────────────────────────────────
def test_floor_is_set_once(client, team, admin_headers):
    r1 = client.post("/api/jira/pull-floor", json={"value": "2026-09-18T00:00:00Z"}, headers=admin_headers).json()
    assert r1["changed"] is True and r1["floor"] == "2026-09-18T00:00:00Z"
    # a second call cannot move it (backward OR forward)
    r2 = client.post("/api/jira/pull-floor", json={"value": "2020-01-01T00:00:00Z"}, headers=admin_headers).json()
    assert r2["changed"] is False and r2["floor"] == "2026-09-18T00:00:00Z"
    r3 = client.post("/api/jira/pull-floor", json={"value": "2030-01-01T00:00:00Z"}, headers=admin_headers).json()
    assert r3["changed"] is False and r3["floor"] == "2026-09-18T00:00:00Z"
    assert _all(client, admin_headers)["jiraPullFloor"] == "2026-09-18T00:00:00Z"


def test_floor_not_writable_via_generic_put(client, team, admin_headers):
    # jiraPullFloor is deliberately OUT of VALID_KEYS - the generic config route must refuse it so it
    # can only move through the set-once endpoint.
    assert "jiraPullFloor" not in server.VALID_KEYS
    r = client.put("/api/config/jiraPullFloor", json="2019-01-01T00:00:00Z", headers=admin_headers)
    assert r.status_code == 400
    assert _all(client, admin_headers)["jiraPullFloor"] == ""   # unchanged


def test_floor_defaults_to_now_when_no_value(client, team, admin_headers):
    r = client.post("/api/jira/pull-floor", json={}, headers=admin_headers).json()
    assert r["changed"] is True and r["floor"]   # a non-empty ISO stamp


# ── 1.2 / 1.3 the per-type + assignee keys are admin-editable and persist ──────────────────────────────
def test_pull_types_editable_and_persist(client, team, admin_headers):
    assert "jiraPullTypes" in server.VALID_KEYS
    client.put("/api/config/jiraPullTypes", json=["Story", "Epic"], headers=admin_headers)
    assert _all(client, admin_headers)["jiraPullTypes"] == ["Story", "Epic"]


def test_assignee_map_editable_and_persist(client, team, admin_headers):
    assert "jiraAssigneeMap" in server.VALID_KEYS
    client.put("/api/config/jiraAssigneeMap", json={"acc:1": "jane.doe"}, headers=admin_headers)
    assert _all(client, admin_headers)["jiraAssigneeMap"] == {"acc:1": "jane.doe"}


def test_pull_status_map_editable(client, team, admin_headers):
    assert "jiraPullStatusMap" in server.VALID_KEYS
    client.put("/api/config/jiraPullStatusMap", json={"Code Review": "In Progress"}, headers=admin_headers)
    assert _all(client, admin_headers)["jiraPullStatusMap"] == {"Code Review": "In Progress"}


# ── 1.3 pure seed helpers ─────────────────────────────────────────────────────────────────────────────
def test_display_to_username():
    assert server._jira_display_to_username("Jane Doe") == "jane.doe"
    assert server._jira_display_to_username("Mary Jane Watson") == "mary.watson"   # first + last
    assert server._jira_display_to_username("  Jacob   Smith ") == "jacob.smith"
    assert server._jira_display_to_username("Cher") is None            # single token
    assert server._jira_display_to_username("") is None
    assert server._jira_display_to_username("Claude Agent for Jira") is None   # service account


def test_seed_assignee_map_matches_and_reports():
    jira_users = [
        {"accountId": "a1", "displayName": "Jane Doe"},        # matches jane.doe
        {"accountId": "a2", "displayName": "Nomatch Person"},  # no Flow user
        {"accountId": "a3", "displayName": "Claude Agent for Jira"},  # service account -> skipped
    ]
    flow = ["jane.doe", "bob.jones"]
    merged, matched, unmatched, skipped = server._seed_assignee_map(jira_users, flow, {})
    assert merged == {"a1": "jane.doe"}
    assert matched == 1
    assert unmatched == ["Nomatch Person"]
    assert skipped == ["Claude Agent for Jira"]


def test_seed_assignee_map_never_clobbers_existing():
    # an accountId already mapped (a manual correction) is left exactly as-is, even if the display name
    # would now match a different Flow user.
    jira_users = [{"accountId": "a1", "displayName": "Jane Doe"}]
    merged, matched, unmatched, skipped = server._seed_assignee_map(
        jira_users, ["jane.doe"], {"a1": "corrected.user"})
    assert merged == {"a1": "corrected.user"}   # untouched
    assert matched == 0
