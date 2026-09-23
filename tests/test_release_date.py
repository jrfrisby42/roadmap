"""RELEASE-DATE-1: releaseDate from Jira's Done resolutiondate, set-once, on create + refresh, with a
one-time backfill. Done-only, MT-day conversion, Jira-wins-over-the-6.53.1-fallback, never-overwrite.

The pure helpers, _construct_pull_item and _refresh_one_pulled are testable directly (they take dicts,
no Jira call). The backfill endpoint's Jira search isn't mocked here (consistent with the suite's
"Jira sync not covered" note); its role gate + jira-config guard are asserted.
"""
import server


# ── _mt_day_from_iso: the off-by-one guard ───────────────────────────────────────
def test_mt_day_late_mountain_evening_stays_same_day():
    # 11:19pm Mountain (offset already MDT) -> same Mountain day (a naive UTC slice would roll to +1)
    assert server._mt_day_from_iso("2026-08-13T23:19:34.174-0600") == "2026-08-13"


def test_mt_day_utc_late_evening_is_previous_mountain_day():
    # 2026-08-14 04:00 UTC = 2026-08-13 22:00 Mountain -> 08-13
    assert server._mt_day_from_iso("2026-08-14T04:00:00+00:00") == "2026-08-13"
    # 2026-08-14 07:00 UTC = 2026-08-14 01:00 Mountain -> 08-14
    assert server._mt_day_from_iso("2026-08-14T07:00:00Z") == "2026-08-14"


def test_mt_day_handles_colonless_offset_and_blank():
    assert server._mt_day_from_iso("2026-02-27T06:25:12.172-0700") == "2026-02-27"
    assert server._mt_day_from_iso("") is None
    assert server._mt_day_from_iso(None) is None


# ── _release_date_from_fields: Done-only ─────────────────────────────────────────
def _fields(res_name, rdate):
    return {"resolution": ({"name": res_name} if res_name else None), "resolutiondate": rdate}


def test_release_date_done_only():
    assert server._release_date_from_fields(_fields("Done", "2026-05-14T15:16:05-0600")) == "2026-05-14"
    assert server._release_date_from_fields(_fields("Won't Do", "2026-05-14T15:16:05-0600")) is None
    assert server._release_date_from_fields(_fields("Duplicate", "2026-05-14T15:16:05-0600")) is None
    assert server._release_date_from_fields(_fields(None, "2026-05-14T15:16:05-0600")) is None
    assert server._release_date_from_fields(_fields("Done", None)) is None
    assert server._release_date_from_fields({}) is None


def test_pull_fetch_fields_includes_resolution():
    assert "resolution,resolutiondate" in server._PULL_FETCH_FIELDS


# ── _construct_pull_item: releaseDate on create (Done-only) ───────────────────────
def _ctx():
    return {"type_rev": {"Story": "Feature"}, "status_eff": {"In Prod": "Released"},
            "overlay": {}, "proj_rev": {"FRAZ": "Fraznet"}, "pods": {}, "assignee_map": {},
            "released": {"Released"}}


def _issue(status="In Prod", res="Done", rdate="2026-05-14T15:16:05-0600"):
    return {"key": "FRAZ-1", "fields": {
        "summary": "X", "issuetype": {"name": "Story"}, "status": {"name": status},
        "project": {"key": "FRAZ"}, "resolution": ({"name": res} if res else None),
        "resolutiondate": rdate, "labels": [], "assignee": None}}


def test_construct_sets_releasedate_when_done():
    item, meta = server._construct_pull_item(_issue(), _ctx(), "admin")
    assert item["releaseDate"] == "2026-05-14"


def test_construct_blank_releasedate_when_not_done():
    item, meta = server._construct_pull_item(_issue(res="Won't Do"), _ctx(), "admin")
    assert item["releaseDate"] == ""
    item2, _ = server._construct_pull_item(_issue(res=None, rdate=None), _ctx(), "admin")
    assert item2["releaseDate"] == ""


# ── _refresh_one_pulled: set-once, Jira wins, 6.53.1 fallback ─────────────────────
_ALL = ["Backlogged", "In Progress", "Released"]


def test_refresh_jira_done_date_wins_and_sets_once():
    cur = {"status": "In Progress", "jiraSource": "pull"}
    chg = server._refresh_one_pulled(cur, _issue(rdate="2026-05-14T15:16:05-0600"), _ctx(), _ALL, today="2026-09-23")
    assert cur["releaseDate"] == "2026-05-14"       # Jira's Done date, NOT `today`
    assert chg["releaseDate"] == ("", "2026-05-14")


def test_refresh_never_overwrites_existing():
    cur = {"status": "In Progress", "jiraSource": "pull", "releaseDate": "2026-01-01"}
    chg = server._refresh_one_pulled(cur, _issue(), _ctx(), _ALL, today="2026-09-23")
    assert cur["releaseDate"] == "2026-01-01"       # untouched
    assert "releaseDate" not in chg


def test_refresh_fallback_when_no_jira_date_but_transition():
    # No resolutiondate in Jira, but this run advances INTO a released status -> 6.53.1 observed stamp
    cur = {"status": "In Progress", "jiraSource": "pull"}
    chg = server._refresh_one_pulled(cur, _issue(res=None, rdate=None), _ctx(), _ALL, today="2026-09-23")
    assert cur["releaseDate"] == "2026-09-23"
    assert chg["releaseDate"] == ("", "2026-09-23")


def test_refresh_no_date_no_transition_no_stamp():
    # Already Released, no Jira date, no transition this run -> nothing to stamp
    cur = {"status": "Released", "jiraSource": "pull"}
    chg = server._refresh_one_pulled(cur, _issue(status="In Prod", res=None, rdate=None), _ctx(), _ALL, today="2026-09-23")
    assert not cur.get("releaseDate")
    assert "releaseDate" not in chg


def test_refresh_wont_do_does_not_stamp_without_transition():
    cur = {"status": "Released", "jiraSource": "pull"}
    chg = server._refresh_one_pulled(cur, _issue(status="In Prod", res="Won't Do"), _ctx(), _ALL, today="2026-09-23")
    assert not cur.get("releaseDate")


# ── endpoint: role gate + jira-config guard (Jira not configured in tests) ────────
def test_backfill_requires_admin(client, team, editor_headers):
    assert client.post("/api/jira/backfill-release-dates", json={"dryRun": True},
                       headers=editor_headers).status_code == 403


def test_backfill_admin_guarded_on_jira_config(client, team, admin_headers):
    # Jira creds are blanked in the test env -> jira_configured() False -> 503 (proves it is wired + guarded)
    r = client.post("/api/jira/backfill-release-dates", json={"dryRun": True}, headers=admin_headers)
    assert r.status_code == 503
