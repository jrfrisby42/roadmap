"""Slack notifications (Tier 1 channel + Tier 2 DMs) - config + dispatch gating.

Network is never exercised: dispatch is inert without a configured transport, and the config
round-trip only touches the config table + /api/all.
"""
import server


def test_slacknotify_in_valid_keys():
    assert "slackNotify" in server.VALID_KEYS


def test_slacknotify_config_roundtrip(client, team, admin_headers):
    a0 = client.get("/api/all", headers=admin_headers).json()
    assert a0["slackNotify"] == {}
    assert a0["slackWebhookPresent"] is False
    assert a0["slackBotTokenPresent"] is False

    r = client.put("/api/config/slackNotify",
                   json={"enabled": True, "types": ["mention", "assigned"], "mode": "both"},
                   headers=admin_headers)
    assert r.status_code == 200

    a1 = client.get("/api/all", headers=admin_headers).json()
    assert a1["slackNotify"] == {"enabled": True, "types": ["mention", "assigned"], "mode": "both"}
    assert a1["slackWebhookPresent"] is False   # no .env transports in tests
    assert a1["slackBotTokenPresent"] is False


def test_slack_transports_absent_by_default(team):
    assert server._slack_webhook(team) == ""
    assert server._slack_bot_token(team) == ""


def test_slack_dispatch_inert_without_transport(team):
    # No webhook/bot env, slackNotify disabled by default -> early return, no raise, no network.
    assert server._slack_dispatch(team, "mention", 1, "Item", "msg", "actor", ["someone"]) is None


def test_slack_dispatch_inert_dm_mode_without_token(team, monkeypatch, admin_headers, client):
    # Enable DM mode but provide no bot token -> want_dm False, want_channel False -> inert.
    client.put("/api/config/slackNotify",
               json={"enabled": True, "types": ["mention"], "mode": "dm"}, headers=admin_headers)
    assert server._slack_dispatch(team, "mention", 1, "Item", "msg", "actor", ["someone"]) is None


def test_slack_user_id_inert_without_token(team):
    # No token -> '' without any network call.
    assert server._slack_user_id(team, "", "someone@example.com") == ""


def test_user_email_lookup(team, admin_headers, client):
    client.put("/api/config/users",
               json=[{"username": "jdoe", "email": "jdoe@example.com"}],
               headers=admin_headers)
    assert server._user_email(team, "jdoe") == "jdoe@example.com"
    assert server._user_email(team, "nobody") == ""


def test_slack_test_endpoint_400_without_transport(client, team, admin_headers):
    r = client.post("/api/slack/test", json={}, headers=admin_headers)
    assert r.status_code == 400   # neither webhook nor bot token configured


def test_channel_msg_names_recipient(team, admin_headers, client):
    # Channel posts should name the recipient instead of the DM-style "you".
    client.put("/api/config/users",
               json=[{"username": "alice", "name": "Alice Jones", "email": "alice@x.com"},
                     {"username": "bob", "email": "bob@x.com"}],
               headers=admin_headers)
    # mention: "you" -> recipient display name (name field wins; username fallback)
    assert server._slack_channel_msg(team, "jr.frisby mentioned you in a comment", ["alice"]) \
        == "jr.frisby mentioned Alice Jones in a comment"
    assert server._slack_channel_msg(team, "jr.frisby mentioned you in a comment", ["bob"]) \
        == "jr.frisby mentioned bob in a comment"
    # reply: "your" -> "<name>'s"
    assert server._slack_channel_msg(team, "jr.frisby replied to your comment on X", ["alice"]) \
        == "jr.frisby replied to Alice Jones's comment on X"
    # status change has no "you" -> unchanged
    assert server._slack_channel_msg(team, "jr.frisby changed status to Done", ["alice"]) \
        == "jr.frisby changed status to Done"


def test_intake_notify_team_config_roundtrip(client, team, admin_headers):
    assert "intakeNotifyTeam" in server.VALID_KEYS
    a0 = client.get("/api/all", headers=admin_headers).json()
    assert a0["intakeNotifyTeam"] is False   # default off
    r = client.put("/api/config/intakeNotifyTeam", json=True, headers=admin_headers)
    assert r.status_code == 200
    a1 = client.get("/api/all", headers=admin_headers).json()
    assert a1["intakeNotifyTeam"] is True


def test_intake_team_usernames_admins_editors(team, admin_headers, client):
    client.put("/api/config/users", json=[
        {"username": "boss", "role": "admin"},
        {"username": "dev1", "role": "editor"},
        {"username": "looker", "role": "viewer"},
        {"username": "contrib1", "role": "contributor"},
    ], headers=admin_headers)
    got = set(server._intake_team_usernames(team))
    assert got == {"boss", "dev1"}   # admins + editors only


# ── AC-AUDIENCE-1: new portal ticket bell = admins + assignee; no assignee -> admins + editors ──────────
def _users(client, admin_headers):
    client.put("/api/config/users", json=[
        {"username": "boss", "role": "admin"},
        {"username": "dev1", "role": "editor"},
        {"username": "dev2", "role": "editor"},
        {"username": "looker", "role": "viewer"},
    ], headers=admin_headers)


def test_new_ticket_with_assignee_notifies_admins_and_assignee_only(team, admin_headers, client):
    _users(client, admin_headers)
    got = set(server._intake_new_ticket_usernames(team, {"assignee": "dev2"}))
    assert got == {"boss", "dev2"}          # dev1 (an unrelated editor) is no longer notified


def test_new_ticket_without_assignee_falls_back_to_admins_and_editors(team, admin_headers, client):
    _users(client, admin_headers)
    assert set(server._intake_new_ticket_usernames(team, {"assignee": ""})) == {"boss", "dev1", "dev2"}
    assert set(server._intake_new_ticket_usernames(team, {})) == {"boss", "dev1", "dev2"}


def test_new_ticket_assigned_to_an_admin_is_not_doubled(team, admin_headers, client):
    _users(client, admin_headers)
    got = server._intake_new_ticket_usernames(team, {"assignee": "boss"})
    assert sorted(got) == ["boss"]


# ── SLACK-COVERAGE-1: who Slack DMs can reach ───────────────────────────────────────────────────────────
def _cov_users(client, admin_headers):
    client.put("/api/config/users", json=[
        {"username": "admin", "role": "admin", "email": "admin@x.com"},
        {"username": "ann", "role": "editor", "email": "Ann@X.com"},
        {"username": "bob", "role": "editor", "email": "bob@x.com"},
        {"username": "cy", "role": "viewer"},
        {"username": "dee", "role": "editor", "email": "dee@x.com"},
    ], headers=admin_headers)


def test_slack_coverage_classifies_everyone(team, admin_headers, client, monkeypatch):
    _cov_users(client, admin_headers)
    monkeypatch.setattr(server, "_slack_bot_token", lambda t: "xoxb-test")
    answers = {"admin@x.com": ("found", "U1"), "ann@x.com": ("found", "U2"),
               "bob@x.com": ("not_found", ""), "dee@x.com": ("error", "missing_scope")}
    seen = []
    monkeypatch.setattr(server, "_slack_lookup", lambda t, tok, email: seen.append(email) or answers[email])
    r = client.get("/api/slack/coverage", headers=admin_headers).json()
    assert r["botToken"] is True and r["total"] == 5 and r["matched"] == 2
    st = {x["username"]: x["status"] for x in r["rows"]}
    assert st == {"admin": "matched", "ann": "matched", "bob": "not_found", "cy": "no_email", "dee": "error"}
    assert [x["username"] for x in r["rows"]][:3] == ["dee", "bob", "cy"]          # problems listed first
    assert next(x for x in r["rows"] if x["username"] == "dee")["detail"] == "missing_scope"
    assert "ann@x.com" in seen                                                     # looked up lower-cased


def test_slack_coverage_without_a_bot_token(team, admin_headers, client, monkeypatch):
    _cov_users(client, admin_headers)
    monkeypatch.setattr(server, "_slack_bot_token", lambda t: "")
    r = client.get("/api/slack/coverage", headers=admin_headers).json()
    assert r["botToken"] is False and r["rows"] == []


def test_slack_coverage_is_admin_only(team, client):
    h = {"Authorization": f"Bearer {server.create_token(team, 'ed', 'editor')}", "X-Team": team}
    assert client.get("/api/slack/coverage", headers=h).status_code == 403


def test_slack_user_id_does_not_cache_a_slack_error(monkeypatch):
    """A missing_scope / network error must not be remembered as 'no Slack account' (it would stick until restart)."""
    team = "cachetest"
    server._slack_uid_cache.pop(team, None)
    calls = {"n": 0}

    def flaky(t, tok, email):
        calls["n"] += 1
        return ("error", "missing_scope") if calls["n"] == 1 else ("found", "U9")
    monkeypatch.setattr(server, "_slack_lookup", flaky)
    assert server._slack_user_id(team, "xoxb", "a@x.com") == ""
    assert server._slack_user_id(team, "xoxb", "a@x.com") == "U9"          # retried, not cached as not-found
    assert server._slack_user_id(team, "xoxb", "a@x.com") == "U9" and calls["n"] == 2   # now cached
