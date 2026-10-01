"""ACT-NOISE-1: the client rules engine re-POSTs its System alerts (At Risk / Needs Decision / Needs Date
Check) on every page load in every tab. A re-post is a re-evaluation of a standing condition, so the server:
  - never bumps created_ts (that made month-old alerts read as "just now" and float to the top);
  - honours a human close (Dismissed / Resolved, e.g. Confirm Dates) while the item is unchanged, for up to
    RULE_ALERT_SNOOZE_DAYS - before this the next page load re-raised it straight back into the queue;
  - converges concurrent posts on ONE open row (BEGIN IMMEDIATE around the check-then-insert).
User-raised alerts keep their prior behaviour.
"""
import threading
from datetime import datetime, timedelta, timezone

import server


def _mk(client, headers, **fields):
    body = {"name": "Item", "status": "Planned", **fields}
    return client.post("/api/projects", json=body, headers=headers).json()["id"]


def _post(client, headers, item_id, atype="Needs Decision", source="System", message="m1"):
    return client.post("/api/activities", headers=headers, json={
        "activity_type": atype, "source": source, "item_id": item_id, "item_name": "X",
        "created_by": source, "message": message, "status": "Open"}).json()


def _open_rows(team, item_id, atype="Needs Decision"):
    with server.db(team) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM activities WHERE item_id=? AND activity_type=? AND status IN ('Open','Read')",
            (item_id, atype)).fetchall()]


def _set(team, sql, args):
    with server.db(team) as c:
        c.execute(sql, args)


def _close(client, headers, aid, status):
    r = client.put(f"/api/activities/{aid}", headers=headers, json={"status": status, "resolved_by": "admin"})
    assert r.status_code == 200, r.text


def test_rule_repost_refreshes_message_without_bumping_created_ts(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    a = _post(client, admin_headers, pid, message="old")
    _set(team, "UPDATE activities SET created_ts=?, note=? WHERE id=?", ("2026-05-01 00:00:00 UTC", "n", a["id"]))
    b = _post(client, admin_headers, pid, message="new")
    assert b["id"] == a["id"]
    assert b["created_ts"] == "2026-05-01 00:00:00 UTC"
    assert b["message"] == "new" and b["note"] == "n"
    assert len(_open_rows(team, pid)) == 1


def test_user_repost_keeps_prior_bump_behaviour(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    a = _post(client, admin_headers, pid, atype="At Risk", source="User", message="old")
    _set(team, "UPDATE activities SET created_ts=? WHERE id=?", ("2026-05-01 00:00:00 UTC", a["id"]))
    b = _post(client, admin_headers, pid, atype="At Risk", source="User", message="new")
    assert b["id"] == a["id"] and b["created_ts"] != "2026-05-01 00:00:00 UTC" and b["note"] == "new"


def test_dismissed_rule_alert_is_not_re_raised(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    a = _post(client, admin_headers, pid)
    _close(client, admin_headers, a["id"], "Dismissed")
    again = _post(client, admin_headers, pid)
    assert again["id"] == a["id"] and again["status"] == "Dismissed"
    assert _open_rows(team, pid) == []


def test_confirmed_dates_rule_alert_is_not_re_raised(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    a = _post(client, admin_headers, pid, atype="Needs Date Check")
    _close(client, admin_headers, a["id"], "Resolved")
    _post(client, admin_headers, pid, atype="Needs Date Check")
    assert _open_rows(team, pid, "Needs Date Check") == []


def test_item_change_after_dismiss_re_raises(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    a = _post(client, admin_headers, pid)
    _close(client, admin_headers, a["id"], "Dismissed")
    later = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    _set(team, "UPDATE projects SET updated_ts=? WHERE id=?", (later, pid))
    b = _post(client, admin_headers, pid)
    assert b["id"] != a["id"] and b["status"] == "Open"
    assert len(_open_rows(team, pid)) == 1


def test_snooze_expires(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    a = _post(client, admin_headers, pid)
    _close(client, admin_headers, a["id"], "Dismissed")
    old = (datetime.now(timezone.utc) - timedelta(days=server.RULE_ALERT_SNOOZE_DAYS + 1)).strftime("%Y-%m-%d %H:%M:%S UTC")
    _set(team, "UPDATE activities SET resolved_ts=? WHERE id=?", (old, a["id"]))
    _set(team, "UPDATE projects SET updated_ts=? WHERE id=?", ("2026-01-01T00:00:00+00:00", pid))
    b = _post(client, admin_headers, pid)
    assert b["id"] != a["id"] and b["status"] == "Open"


def test_auto_cleared_rule_alert_re_raises_when_condition_returns(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    a = _post(client, admin_headers, pid)
    _close(client, admin_headers, a["id"], "Auto-Cleared")
    b = _post(client, admin_headers, pid)
    assert b["id"] != a["id"] and b["status"] == "Open"


def test_dismissed_user_alert_still_re_raises(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    a = _post(client, admin_headers, pid, atype="At Risk", source="User")
    _close(client, admin_headers, a["id"], "Dismissed")
    b = _post(client, admin_headers, pid, atype="At Risk", source="User")
    assert b["id"] != a["id"] and b["status"] == "Open"


def test_concurrent_rule_posts_converge_on_one_row(client, team, admin_headers):
    pid = _mk(client, admin_headers)
    body = {"activity_type": "Needs Decision", "source": "System", "item_id": pid,
            "item_name": "X", "message": "m", "status": "Open"}
    barrier = threading.Barrier(8)
    errors = []

    def go():
        try:
            barrier.wait()
            server._insert_activity(dict(body), team)
        except Exception as e:   # pragma: no cover - surfaced by the assert below
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert not errors
    assert len(_open_rows(team, pid)) == 1
