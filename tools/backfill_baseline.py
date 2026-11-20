#!/usr/bin/env python3
"""GANTT-BASELINE-1 - rebuild a past start-of-quarter baseline from the audit log.

Baselines start when the feature ships, so the quarter already under way has no snapshot from its first day. This
rebuilds one: it takes every item as it is now, walks the audit log back to --as-of (newest change first) undoing
start / due / revised / status / type / parent / hidden / archived / sprint changes ("from" values), drops items
created after --as-of, then runs the shipped server._baseline_items on the result. Config (project types, finished
statuses, sprints) is today's. Items deleted since --as-of cannot be rebuilt (counted in the report).

  Dry run (default, no writes):  /opt/roadmap/venv/bin/python tools/backfill_baseline.py --team development --as-of 2026-10-01
  Apply:                         ... --commit

--commit first backs up the Org DB with the SQLite online-backup API (WAL-safe) to roadmap.db.baseline-bak-<ts>, then
writes the quarter's automatic baseline (replacing one taken later in the quarter, e.g. on first use after the
deploy) with taken_at = --as-of. One audit row. Importing server runs its normal idempotent boot, as a restart does.
Run as the service user (ubuntu) so no root-owned DB files are left behind.
"""
import argparse
import datetime
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TRACK = ("start", "due", "revised", "status", "type", "parent", "hidden", "archived", "sprintId", "startAuto")
_AUTO_START = re.compile(r"Start date set to (\d{4}-\d\d-\d\d) \(moved to an active status\)")


def _undo_values(ch):
    """{field: value before the change} for the fields a baseline depends on, from any audit `changes` shape:
    {"field": {"from", "to"}} (edits), {"fields": {...}} (Jira refresh), {"changes": {...}}, or [from, to] pairs."""
    out = {}
    if not isinstance(ch, dict):
        return out
    srcs = [ch] + [ch[k] for k in ("fields", "changes") if isinstance(ch.get(k), dict)]
    for src in srcs:
        for k in TRACK:
            v = src.get(k)
            if isinstance(v, dict) and "from" in v:
                out[k] = v["from"]
            elif isinstance(v, (list, tuple)) and len(v) == 2:
                out[k] = v[0]
    return out


def _ts(s):
    s = (s or "").strip()
    try:
        if s.endswith(" UTC"):
            return datetime.datetime.strptime(s[:-4], "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
        d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", required=True)
    ap.add_argument("--as-of", required=True, help="YYYY-MM-DD (start of that day, Mountain Time)")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    import server
    path = os.path.join(server.TENANTS_DIR, a.team, "roadmap.db")
    day = datetime.date.fromisoformat(a.as_of)
    cut = datetime.datetime(day.year, day.month, day.day, tzinfo=server._MT_ZONE).astimezone(datetime.timezone.utc)
    qkey, qname = server._quarter_key(day)

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = {r["id"]: json.loads(r["data"]) for r in con.execute("SELECT id, data FROM projects")}
    now_rows = {k: json.loads(json.dumps(v)) for k, v in rows.items()}   # deep copy, keeping the int ids
    cfg = server._baseline_cfg(con)
    created_after, deleted_after, undone, unknown_dates = set(), set(), {}, 0
    for r in con.execute("SELECT * FROM audit_log ORDER BY id DESC"):
        t = _ts(r["ts"])
        if t is None or t < cut:
            break                                           # ids grow with time: everything older is before --as-of
        pid = r["project_id"]
        if r["action"] in ("create", "intake:create", "jira:pull-create") and pid in rows:
            created_after.add(pid)
        if r["action"] == "delete" and pid is not None and pid not in rows:
            deleted_after.add(pid)
        if pid not in rows:
            continue
        try:
            ch = json.loads(r["changes"]) if r["changes"] else None
        except Exception:
            ch = None
        vals = _undo_values(ch)
        m = _AUTO_START.search(r["changes"] or "")
        if m and rows[pid].get("start") == m.group(1):
            vals.setdefault("start", None)
            vals.setdefault("startAuto", None)
        if not vals:
            if r["changes"] and re.search(r'"(start|due|revised)"', r["changes"]):
                unknown_dates += 1
            continue
        for k, v in vals.items():
            if v in (None, ""):
                rows[pid].pop(k, None)
            else:
                rows[pid][k] = v
        undone.setdefault(pid, set()).update(vals)
    con.close()
    for pid, p in list(rows.items()):
        ca = _ts(str(p.get("createdAt") or ""))
        if pid in created_after or (ca and ca >= cut):
            rows.pop(pid)

    then, now = server._baseline_items(rows, cfg), server._baseline_items(now_rows, cfg)
    moved = sorted(((v["n"], v["d"], now[k]["d"]) for k, v in then.items() if k in now and now[k]["d"] != v["d"]),
                   key=lambda x: x[0].lower())
    print(json.dumps({
        "team": a.team, "asOf": a.as_of, "quarter": qkey, "name": qname,
        "projectsInBaseline": len(then), "projectsPlannedNow": len(now),
        "itemsWithChangesUndone": len(undone), "itemsCreatedSinceSkipped": len(created_after | {p for p in now_rows if p not in rows}),
        "itemsDeletedSinceNotRebuilt": len(deleted_after), "dateAuditRowsNotUnderstood": unknown_dates,
        "projectsWhoseDueMovedSince": len(moved),
        "sampleMoves": [f"{n}: {d0} -> {d1}" for n, d0, d1 in moved[:15]],
    }, indent=1))
    if not a.commit:
        print("DRY RUN - nothing written.")
        return
    bak = path + ".baseline-bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    src, dst = sqlite3.connect(path), sqlite3.connect(bak)
    with dst:
        src.backup(dst)
    dst.close()
    print("backup:", bak)
    with src:
        src.execute("BEGIN IMMEDIATE")
        if src.execute("SELECT 1 FROM baselines WHERE quarter=?", (qkey,)).fetchone():
            src.execute("UPDATE baselines SET name=?, taken_at=?, taken_by=?, items=? WHERE quarter=?",
                        (qname, cut.isoformat(), "rebuilt from history", json.dumps(then), qkey))
        else:
            src.execute("INSERT INTO baselines(name, taken_at, taken_by, quarter, items) VALUES(?,?,?,?,?)",
                        (qname, cut.isoformat(), "rebuilt from history", qkey, json.dumps(then)))
        src.execute("INSERT INTO audit_log(ts,username,action,project_id,project_name,changes) VALUES(?,?,?,?,?,?)",
                    (datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), "system",
                     "baseline:rebuild", None, "", json.dumps({"name": qname, "asOf": a.as_of, "projects": len(then)})))
    src.close()
    print(f"Wrote {qname} ({len(then)} projects) as of {a.as_of}.")


if __name__ == "__main__":
    main()
