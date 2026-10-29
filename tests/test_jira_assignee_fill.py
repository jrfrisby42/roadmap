"""JIRA-ASSIGNEE-1: Flow items fill a BLANK assignee from their Jira ticket, and the assignee map seeds better.

Measured on development (2026-10-29): 31 active items had no assignee; the pulled-item refresh already
copies a mapped Jira assignee, but Flow-created items were skipped entirely, and the seed only matched a
Flow username spelled exactly first.last, so people whose username is an email address never mapped (and
the admin screen could not show them to map by hand). Now: the per-item sync fills a blank assignee from
the primary ticket when the Jira person is mapped (never overwrites), and the seed also matches the part
before '@' of a username or email. Jira mocked throughout."""
import json

import server

ACCT_JANE, ACCT_TIM, ACCT_NOBODY = "acct-jane", "acct-tim", "acct-nobody"


def _set_cfg(team, key, value):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value)))


def _item(team, **f):
    with server.db(team) as c:
        return server._insert_project(c, {"name": "Thing", "status": "Planned", **f})


def _blob(team, pid):
    with server.db(team) as c:
        return json.loads(c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()[0])


def _mock_jira(monkeypatch, assignee_by_key):
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_fetch_jira_feature_flags", lambda t: set())

    def fake(method, path, payload=None, *a, **k):
        key = path.split("/issue/")[1].split("?")[0]
        acct = assignee_by_key.get(key)
        return {"fields": {"summary": key, "status": {"name": "In Dev"},
                           "assignee": ({"accountId": acct, "displayName": acct} if acct else None)}}
    monkeypatch.setattr(server, "_jira_req", fake)


def _sync(team, pid):
    return server.jira_pull_sync(pid, {"_suppressNotify": True}, None,
                                 {"team": team, "username": "admin", "role": "admin"})


# ── the sync fills a blank assignee ──────────────────────────────────────────────────────────────────

def test_blank_assignee_is_filled_from_a_mapped_jira_person(team, monkeypatch):
    _set_cfg(team, "jiraAssigneeMap", {ACCT_JANE: "jane.doe"})
    _mock_jira(monkeypatch, {"FRAZ-1": ACCT_JANE})
    pid = _item(team, jiraTickets=["FRAZ-1"])
    r = _sync(team, pid)
    assert r["assigneeFilled"] == "jane.doe"
    assert _blob(team, pid)["assignee"] == "jane.doe"
    assert "jane.doe" in server._get_watchers(team, pid)


def test_an_existing_assignee_is_never_changed(team, monkeypatch):
    _set_cfg(team, "jiraAssigneeMap", {ACCT_JANE: "jane.doe"})
    _mock_jira(monkeypatch, {"FRAZ-1": ACCT_JANE})
    pid = _item(team, jiraTickets=["FRAZ-1"], assignee="bob.jones")
    r = _sync(team, pid)
    assert r["assigneeFilled"] is None
    assert _blob(team, pid)["assignee"] == "bob.jones"


def test_unmapped_or_unassigned_in_jira_leaves_it_blank(team, monkeypatch):
    _set_cfg(team, "jiraAssigneeMap", {ACCT_JANE: "jane.doe"})
    _mock_jira(monkeypatch, {"FRAZ-1": ACCT_NOBODY, "FRAZ-2": None})
    a, b = _item(team, jiraTickets=["FRAZ-1"]), _item(team, jiraTickets=["FRAZ-2"])
    assert _sync(team, a)["assigneeFilled"] is None and not _blob(team, a).get("assignee")
    assert _sync(team, b)["assigneeFilled"] is None and not _blob(team, b).get("assignee")


def test_the_primary_ticket_decides(team, monkeypatch):
    _set_cfg(team, "jiraAssigneeMap", {ACCT_JANE: "jane.doe", ACCT_TIM: "tim.cottrell@x.com"})
    _mock_jira(monkeypatch, {"FRAZ-1": ACCT_TIM, "FRAZ-2": ACCT_JANE})
    pid = _item(team, jiraTickets=["FRAZ-1", "FRAZ-2"])
    _sync(team, pid)
    assert _blob(team, pid)["assignee"] == "tim.cottrell@x.com"


def test_a_noop_sync_keeps_the_conflict_token(team, monkeypatch):
    _set_cfg(team, "jiraAssigneeMap", {})
    _mock_jira(monkeypatch, {"FRAZ-1": ACCT_JANE})
    pid = _item(team, jiraTickets=["FRAZ-1"])
    _sync(team, pid)                                   # first sync records the Jira status
    with server.db(team) as c:
        ts1 = c.execute("SELECT updated_ts FROM projects WHERE id=?", (pid,)).fetchone()[0]
    _sync(team, pid)
    with server.db(team) as c:
        ts2 = c.execute("SELECT updated_ts FROM projects WHERE id=?", (pid,)).fetchone()[0]
    assert ts1 == ts2


# ── the seed matches more Flow spellings ─────────────────────────────────────────────────────────────

def test_seed_matches_a_username_that_is_an_email():
    merged, matched, unmatched, _ = server._seed_assignee_map(
        [{"accountId": ACCT_TIM, "displayName": "Tim Cottrell"}], ["tim.cottrell@company.com"], {})
    assert merged == {ACCT_TIM: "tim.cottrell@company.com"} and matched == 1 and unmatched == []


def test_seed_matches_the_users_email():
    users = [{"username": "utsav.lamichhanne", "email": "utsav.lamichhane@company.com"}]
    merged, matched, _, _ = server._seed_assignee_map(
        [{"accountId": "acct-u", "displayName": "Utsav Lamichhane"}], ["utsav.lamichhanne"], {}, users)
    assert merged == {"acct-u": "utsav.lamichhanne"} and matched == 1


def test_seed_never_guesses_between_two_people():
    users = [{"username": "jdoe1", "email": "jane.doe@a.com"}, {"username": "jdoe2", "email": "jane.doe@b.com"}]
    merged, matched, unmatched, _ = server._seed_assignee_map(
        [{"accountId": ACCT_JANE, "displayName": "Jane Doe"}], ["jdoe1", "jdoe2"], {}, users)
    assert merged == {} and matched == 0 and unmatched == ["Jane Doe"]


def test_seed_an_exact_username_wins_over_an_alias():
    users = [{"username": "someone.else", "email": "jane.doe@a.com"}]
    merged, _, _, _ = server._seed_assignee_map(
        [{"accountId": ACCT_JANE, "displayName": "Jane Doe"}], ["jane.doe", "someone.else"], {}, users)
    assert merged == {ACCT_JANE: "jane.doe"}


def test_assignable_users_endpoint_is_admin_only_and_hides_service_accounts(client, team, admin_headers,
                                                                            editor_headers, monkeypatch):
    monkeypatch.setattr(server, "jira_configured", lambda: True)
    monkeypatch.setattr(server, "_jira_assignable_users", lambda t: [
        {"accountId": "b", "displayName": "Zed Last"}, {"accountId": "a", "displayName": "Amy First"},
        {"accountId": "s", "displayName": "Claude Agent for Jira"}])
    assert client.get("/api/jira/assignable-users", headers=editor_headers).status_code == 403
    r = client.get("/api/jira/assignable-users", headers=admin_headers)
    assert r.status_code == 200
    assert [u["displayName"] for u in r.json()["users"]] == ["Amy First", "Zed Last"]
