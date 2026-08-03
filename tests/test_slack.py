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
