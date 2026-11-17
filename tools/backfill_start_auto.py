#!/usr/bin/env python3
"""GANTT-SPRINT-GUESS-1 - mark existing starts that Flow chose itself (`startAuto`).

Before 6.62.57 the Jira sync's "moved to an active status" rule set a missing start without saying so; its audit row
reads "Start date set to YYYY-MM-DD (moved to an active status)". An item whose start still equals that date gets
`startAuto: true`, so the Gantt can prefer its sprint's dates over that guess. A start anyone changed since is left alone.

  Dry run (default, no writes):  /opt/roadmap/venv/bin/python tools/backfill_start_auto.py --team development
  Apply:                         ... --commit

--commit first backs up the Org DB with the SQLite online-backup API (WAL-safe) to roadmap.db.startauto-bak-<ts>;
restoring it is the rollback. Writes are surgical json_set (no updated_ts bump), with one audit row. Run as the
service user (ubuntu) so no root-owned DB files are left behind.
"""
import argparse
import datetime
import json
import os
import re
import sqlite3

_PAT = re.compile(r"Start date set to (\d{4}-\d\d-\d\d) \(moved to an active status\)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", required=True)
    ap.add_argument("--tenants", default=os.environ.get("FRAZIL_TENANTS_DIR", "/data/tenants"))
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    path = os.path.join(a.tenants, a.team, "roadmap.db")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    hits = {}
    for pid, ch in con.execute("SELECT project_id, changes FROM audit_log WHERE changes LIKE '%moved to an active status%' ORDER BY id"):
        for m in _PAT.finditer(ch or ""):
            hits[pid] = m.group(1)                      # last record wins (audit rows are read in id order)
    todo = []
    for pid, d in hits.items():
        row = con.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
        if not row:
            continue
        p = json.loads(row[0])
        if p.get("start") == d and not p.get("startAuto"):
            todo.append((pid, p.get("itemKey") or f"#{pid}"))
    con.close()
    print(json.dumps({"team": a.team, "autoStartRecords": len(hits), "toMark": len(todo), "keys": [k for _, k in todo]}, indent=1))
    if not a.commit or not todo:
        print("DRY RUN - nothing written." if not a.commit else "Nothing to do.")
        return
    bak = path + ".startauto-bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    src, dst = sqlite3.connect(path), sqlite3.connect(bak)
    with dst:
        src.backup(dst)
    dst.close()
    print("backup:", bak)
    with src:
        src.execute("BEGIN IMMEDIATE")
        for pid, _ in todo:
            src.execute("UPDATE projects SET data=json_set(data, '$.startAuto', json('true')) WHERE id=?", (pid,))
        src.execute("INSERT INTO audit_log(ts,username,action,project_id,project_name,changes) VALUES(?,?,?,?,?,?)",
                    (datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), "start-auto-backfill",
                     "gantt:start-auto-backfill", None, "", json.dumps({"count": len(todo), "keys": [k for _, k in todo]})))
    src.close()
    print("marked:", len(todo))


if __name__ == "__main__":
    main()
