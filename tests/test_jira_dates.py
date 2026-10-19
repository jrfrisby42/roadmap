"""JIRA-DATES-1: the pulled-item refresh keeps start/due in step with Jira.

Jira owns a pulled item's schedule: "Start date" (customfield_10025) -> start, duedate -> due. A differing
Jira value is applied; an empty Jira value keeps Flow's (same rule as pod/assignee). Pure, Jira mocked."""
import server

STATUSES = ["New", "In Progress", "Released"]
CTX = {"status_eff": {"In Dev": "In Progress"}, "pods": [], "assignee_map": {}, "released": {"Released"},
       "terminal": {"Released": True}}


def _iss(start=None, due=None, status="In Dev"):
    f = {"status": {"name": status}, "issuetype": {"name": "Story"}, "project": {"key": "FRAZ"}}
    if start is not None: f["customfield_10025"] = start
    if due is not None:   f["duedate"] = due
    return {"key": "FRAZ-1", "fields": f}


def test_fills_missing_dates():
    cur = {"status": "In Progress"}
    chg = server._refresh_one_pulled(cur, _iss("2026-09-21", "2026-10-02"), CTX, STATUSES)
    assert cur["start"] == "2026-09-21" and cur["due"] == "2026-10-02"
    assert chg["start"] == ("", "2026-09-21") and chg["due"] == ("", "2026-10-02")


def test_follows_a_changed_jira_date():
    cur = {"status": "In Progress", "start": "2026-09-08", "due": "2026-09-14"}
    chg = server._refresh_one_pulled(cur, _iss("2026-09-08", "2026-09-18"), CTX, STATUSES)
    assert cur["due"] == "2026-09-18" and chg["due"] == ("2026-09-14", "2026-09-18")
    assert "start" not in chg                                   # unchanged field is not reported


def test_empty_jira_date_keeps_flow_value():
    cur = {"status": "In Progress", "start": "2026-09-08", "due": "2026-09-14"}
    chg = server._refresh_one_pulled(cur, _iss(None, ""), CTX, STATUSES)
    assert cur["start"] == "2026-09-08" and cur["due"] == "2026-09-14"
    assert "start" not in chg and "due" not in chg


def test_datetime_shaped_value_is_trimmed_to_a_day():
    cur = {"status": "In Progress"}
    server._refresh_one_pulled(cur, _iss("2026-09-21T00:00:00.000+0000", None), CTX, STATUSES)
    assert cur["start"] == "2026-09-21"


def test_matching_dates_are_a_no_op():
    cur = {"status": "In Progress", "start": "2026-09-21", "due": "2026-10-02"}
    assert server._refresh_one_pulled(cur, _iss("2026-09-21", "2026-10-02"), CTX, STATUSES) == {}


def test_refresh_fields_registry_includes_dates():
    assert {"start", "due"} <= set(server._PULL_REFRESH_FIELDS)
    assert set(f for f, _ in server._PULL_REFRESH_DATES) == {"start", "due"}
