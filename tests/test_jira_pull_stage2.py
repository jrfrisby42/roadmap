"""JIRA-PULL-1 Stage 2 - candidate query + dedupe. Returns a list; creates nothing.

Pure-logic guards for: the floor -> JQL-bound tz conversion (the off-by-one J.R. flagged), the JQL
construction (project + floor + selected types, so the full history is never fetched), and the
presence-only dedupe (93 keys are multi-referenced, so the check must test presence, never assume the
mapped id is 'the' item). The live count against production runs in the report, not here (needs Jira).
"""
import server


# ── the floor -> JQL-bound tz conversion ──────────────────────────────────────────────────────────────
def test_floor_bound_denver_account_keeps_mountain_midnight():
    # the development floor, as an account in America/Denver: the JQL bound must be Mountain midnight.
    assert server._pull_floor_to_jql_bound("2026-08-01T00:00:00-06:00", "America/Denver") == "2026-08-01 00:00"


def test_floor_bound_utc_account_shifts_to_the_same_instant():
    # a UTC account: the SAME instant must be expressed as 06:00 so Jira (interpreting in UTC) lands on
    # the same moment - not 00:00, which would be 6h early (the off-by-one).
    assert server._pull_floor_to_jql_bound("2026-08-01T00:00:00-06:00", "UTC") == "2026-08-01 06:00"


def test_floor_bound_accepts_z_form():
    assert server._pull_floor_to_jql_bound("2026-08-01T06:00:00Z", "America/Denver") == "2026-08-01 00:00"


def test_floor_bound_naive_is_assumed_account_local():
    assert server._pull_floor_to_jql_bound("2026-08-01T00:00:00", "America/Denver") == "2026-08-01 00:00"


def test_floor_bound_empty():
    assert server._pull_floor_to_jql_bound("", "America/Denver") == ""


# ── the JQL construction ──────────────────────────────────────────────────────────────────────────────
def test_pull_jql_shape():
    jql = server._jira_pull_jql("2026-08-01 00:00", ["Story", "Epic", "Roadmap Item"], ["FRAZ"])
    assert 'project in (FRAZ)' in jql
    assert 'created >= "2026-08-01 00:00"' in jql
    assert 'issuetype in ("Story", "Epic", "Roadmap Item")' in jql   # multi-word types quoted
    assert jql.endswith("ORDER BY created ASC")


def test_pull_jql_quotes_defensively():
    # a stray quote in a type name must not break out of the JQL string
    jql = server._jira_pull_jql("2026-08-01 00:00", ['Sto"ry'], ["FRAZ"])
    assert '"Story"' in jql   # the inner quote is stripped


# ── presence-only dedupe ──────────────────────────────────────────────────────────────────────────────
def test_filter_presence_only():
    issues = [{"key": "FRAZ-1"}, {"key": "FRAZ-2"}, {"key": "FRAZ-3"}]
    flow_keys = {"FRAZ-2"}                       # already referenced by a Flow item
    candidates, already = server._filter_pull_candidates(issues, flow_keys)
    assert [c["key"] for c in candidates] == ["FRAZ-1", "FRAZ-3"]
    assert already == 1


def test_filter_uses_key_presence_not_mapped_id():
    # a key referenced by MANY Flow items (93 exist on dev) is still just 'present' - dropped once, and the
    # filter never looks at which item id it maps to.
    issues = [{"key": "FRAZ-10550"}, {"key": "FRAZ-NEW"}]
    flow_keys = {"FRAZ-10550"}                   # set membership = presence, id-agnostic
    candidates, already = server._filter_pull_candidates(issues, flow_keys)
    assert [c["key"] for c in candidates] == ["FRAZ-NEW"] and already == 1


# ── endpoint guard paths (no Jira call when the feature isn't configured) ─────────────────────────────
def test_candidates_endpoint_floor_not_set(client, team, admin_headers):
    # a fresh team has floor "" -> the endpoint returns empty with a reason, WITHOUT calling Jira.
    r = client.get("/api/jira/pull-candidates", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["candidateCount"] == 0 and body["reason"] == "floor not set" and body["candidates"] == []


def test_candidates_endpoint_no_types(client, team, admin_headers):
    # floor set but no pull types selected -> empty, no Jira call.
    client.post("/api/jira/pull-floor", json={"value": "2026-08-01T00:00:00-06:00"}, headers=admin_headers)
    r = client.get("/api/jira/pull-candidates", headers=admin_headers).json()
    assert r["candidateCount"] == 0 and r["reason"] == "no pull types selected"


def test_candidates_endpoint_admin_gated(client, team, viewer_headers):
    # a non-admin cannot enumerate pull candidates.
    r = client.get("/api/jira/pull-candidates", headers=viewer_headers)
    assert r.status_code in (401, 403)
