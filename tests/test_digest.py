"""IT/Ops weekly queue-health digest (Stage B).

The digest builder (_digest_summary) + sender (send_team_digests) + preview endpoint. SES is
never hit - server.send_email is monkeypatched to capture what would be sent. Items are seeded
directly (createdAt/completedAt are server-owned, so they can't be set through the API)."""
import json
from datetime import datetime, timezone, timedelta

import server


def _cfg(team, key, val):
    with server.db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(val)))


def _iso(days_ago, hours=0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours)).isoformat()


def _seed(team, items):
    with server.db(team) as c:
        nid = (c.execute("SELECT MAX(id) FROM projects").fetchone()[0] or 0) + 1
        for it in items:
            it = {"id": nid, **it}
            c.execute("INSERT INTO projects(id,data) VALUES(?,?)", (nid, json.dumps(it)))
            server._reindex_project(c, nid, it)
            nid += 1


def _base_config(team):
    _cfg(team, "statusIsTerminal", {"Done": True})
    _cfg(team, "slaTargets", {"enabled": True, "resolution": {"1": 4, "2": 24, "3": 72, "4": 168}, "atRiskPct": 80})


# ── the summary builder ──────────────────────────────────────────────────────────
def test_digest_summary_counts(team):
    _base_config(team)
    _seed(team, [
        {"name": "Open urgent breached", "status": "In Progress", "priority": 1, "createdAt": _iso(5), "due": (datetime.now(timezone.utc)-timedelta(days=1)).date().isoformat()},
        {"name": "Open aged", "status": "New", "priority": 4, "createdAt": _iso(40)},
        {"name": "Open fresh", "status": "New", "priority": 3, "createdAt": _iso(1)},
        {"name": "Closed recently", "status": "Done", "priority": 2, "createdAt": _iso(10), "completedAt": _iso(2)},
        {"name": "Closed long ago", "status": "Done", "priority": 2, "createdAt": _iso(40), "completedAt": _iso(20)},
    ])
    term = server._cfg_val(team, "statusIsTerminal", {})
    sla = server._cfg_val(team, "slaTargets", {})
    with server.db(team) as c:
        items = [json.loads(r["data"]) for r in c.execute("SELECT data FROM projects").fetchall()]
    now = datetime.now(timezone.utc)
    s = server._digest_summary(items, term, sla, now)
    assert s["open"] == 3
    assert s["overdue"] == 1                       # the one past-due open item
    assert s["sla_breached"] == 2                  # urgent(5d>4h) + aged(40d>7d); the fresh P3 (1d of 3d) is on-track
    assert s["aged_30"] == 1                       # the 40-day-old open item
    assert s["closed_7d"] == 1                     # closed 2 days ago (not the 20-day-old close)
    # oldest_open sorted oldest-first
    ages = [it["age"] for it in s["oldest_open"]]
    assert ages == sorted(ages, reverse=True)
    assert s["oldest_open"][0]["name"] == "Open aged"


def test_digest_render_shapes(team):
    _base_config(team)
    _seed(team, [{"name": "X", "status": "New", "priority": 1, "createdAt": _iso(5)}])
    with server.db(team) as c:
        items = [json.loads(r["data"]) for r in c.execute("SELECT data FROM projects").fetchall()]
    s = server._digest_summary(items, {"Done": True}, server._cfg_val(team, "slaTargets", {}), datetime.now(timezone.utc))
    subj, text, html_body = server._render_digest_email(team, "", s, "http://x")   # sla_enabled defaults True
    assert "Queue health" in subj and team in subj
    assert "Open" in text and "Past SLA target" in text   # DIGEST-WATCH-1: "SLA breached" relabelled -> "Past SLA target"
    assert "<table" in html_body


# ── DIGEST-WATCH-1: the SLA section is gated on slaTargets.enabled ───────────────────────────────────
def _sla_sample():
    return {"open": 10, "overdue": 3, "sla_breached": 5, "sla_atrisk": 2,
            "aged_30": 4, "closed_7d": 6, "oldest_open": []}


def test_digest_omits_sla_section_when_off():
    s = _sla_sample()
    subj, text, html_body = server._render_digest_email("acme", "", s, "http://x", sla_enabled=False)
    assert "SLA" not in subj                                    # subject carries no SLA count
    assert "Past SLA target" not in html_body and "SLA at risk" not in html_body   # both tiles ABSENT
    assert "Past SLA target" not in text and "SLA at risk" not in text
    for label in ("Open", "Overdue", "Aged 30+ days", "Closed last 7 days"):        # universal tiles remain
        assert label in html_body and label in text


def test_digest_includes_sla_section_when_on():
    s = _sla_sample()
    subj, text, html_body = server._render_digest_email("acme", "", s, "http://x", sla_enabled=True)
    assert "past SLA target" in subj                           # relabelled, present
    assert "Past SLA target" in html_body and "SLA at risk" in html_body
    assert "SLA breached" not in html_body                     # the old alarm label is gone everywhere


# ── the sender ────────────────────────────────────────────────────────────────────
# NOTE: send_team_digests scans EVERY team in the tenants dir, and teams persist across the
# session, so these tests assert on THIS team's unique recipient addresses, never on global totals.
def test_send_team_digests_uses_existing_recipients(team, monkeypatch):
    _base_config(team)
    inbox, hw = f"{team}-inbox@example.com", f"{team}-hw@example.com"
    _cfg(team, "digestConfig", {"enabled": True})
    _cfg(team, "intakeNotifyEmail", inbox)
    _cfg(team, "departmentMeta", {"Hardware": {"emails": [hw]}})
    _seed(team, [
        {"name": "A", "status": "New", "priority": 1, "createdAt": _iso(5), "departments": ["Hardware"]},
        {"name": "B", "status": "New", "priority": 2, "createdAt": _iso(1)},
    ])
    sent = []
    monkeypatch.setattr(server, "send_email", lambda to, subj, text, html=None: sent.append((to, subj)))
    server.send_team_digests(verbose=False)
    tos = {t for t, _ in sent}
    # team-wide -> intakeNotifyEmail, per-dept -> the dept notify list
    assert inbox in tos
    assert hw in tos


def test_send_team_digests_skips_disabled(team, monkeypatch):
    inbox = f"{team}-disabled@example.com"
    _base_config(team)
    _cfg(team, "digestConfig", {"enabled": False})
    _cfg(team, "intakeNotifyEmail", inbox)
    _seed(team, [{"name": "A", "status": "New", "priority": 1, "createdAt": _iso(5)}])
    sent = []
    monkeypatch.setattr(server, "send_email", lambda to, subj, text, html=None: sent.append(to))
    server.send_team_digests(verbose=False)
    assert inbox not in sent                        # this disabled team is never sent


def test_send_team_digests_best_effort(team, monkeypatch):
    # A send failure must not abort the run (best-effort per recipient).
    _base_config(team)
    _cfg(team, "digestConfig", {"enabled": True})
    _cfg(team, "intakeNotifyEmail", f"{team}-boom@example.com")
    _seed(team, [{"name": "A", "status": "New", "priority": 1, "createdAt": _iso(5)}])
    def boom(*a, **k):
        raise RuntimeError("SES down")
    monkeypatch.setattr(server, "send_email", boom)
    server.send_team_digests(verbose=False)         # must not raise


# ── the preview endpoint ────────────────────────────────────────────────────────────
def test_preview_requires_email(client, team, admin_headers, monkeypatch):
    _base_config(team)
    monkeypatch.setattr(server, "send_email", lambda *a, **k: None)
    # admin has no email on their user -> 400 asks for one
    r = client.post("/api/admin/send-digest-preview", json={}, headers=admin_headers)
    assert r.status_code == 400


def test_preview_sends_with_email(client, team, admin_headers, monkeypatch):
    _base_config(team)
    _seed(team, [{"name": "A", "status": "New", "priority": 1, "createdAt": _iso(5)}])
    sent = []
    monkeypatch.setattr(server, "send_email", lambda to, subj, text, html=None: sent.append((to, subj)))
    r = client.post("/api/admin/send-digest-preview", json={"email": "me@example.com"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["sentTo"] == "me@example.com"
    assert sent and sent[0][0] == "me@example.com"


def test_preview_is_admin_only(client, team, editor_headers, viewer_headers):
    for h in (editor_headers, viewer_headers):
        assert client.post("/api/admin/send-digest-preview", json={"email": "x@y.com"}, headers=h).status_code in (401, 403)
