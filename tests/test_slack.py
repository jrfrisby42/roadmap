"""Slack notifications (Tier 1) - config round-trip + dispatch gating.

Network is never exercised: dispatch is inert without a configured webhook, and the config
round-trip only touches the config table + /api/all.
"""
import server


def test_slacknotify_in_valid_keys():
    assert "slackNotify" in server.VALID_KEYS


def test_slacknotify_config_roundtrip(client, team, admin_headers):
    # default: absent/empty + no webhook env -> {} and presence False
    a0 = client.get("/api/all", headers=admin_headers).json()
    assert a0.get("slackNotify") in ({}, None) or a0["slackNotify"] == {}
    assert a0.get("slackWebhookPresent") is False

    r = client.put("/api/config/slackNotify",
                   json={"enabled": True, "types": ["mention", "assigned"]},
                   headers=admin_headers)
    assert r.status_code == 200

    a1 = client.get("/api/all", headers=admin_headers).json()
    assert a1["slackNotify"] == {"enabled": True, "types": ["mention", "assigned"]}
    assert a1["slackWebhookPresent"] is False   # still no .env webhook in tests


def test_slack_dispatch_inert_without_webhook(team):
    # No SLACK_WEBHOOK_<TEAM> env in the test environment -> early return, no raise, no network.
    assert server._slack_dispatch(team, "mention", 1, "Item", "msg", "actor") is None


def test_slack_dispatch_inert_when_disabled(team, monkeypatch):
    # Webhook present but slackNotify disabled (default) -> still inert (no thread/network).
    slug = "".join(ch for ch in team.upper() if ch.isalnum())
    monkeypatch.setenv("SLACK_WEBHOOK_" + slug, "https://hooks.slack.example/T/B/xxx")
    # slackNotify defaults to {} (enabled falsy) -> returns before any post.
    assert server._slack_dispatch(team, "mention", 1, "Item", "msg", "actor") is None


def test_slack_test_endpoint_400_without_webhook(client, team, admin_headers):
    r = client.post("/api/slack/test", json={}, headers=admin_headers)
    assert r.status_code == 400   # no webhook configured
