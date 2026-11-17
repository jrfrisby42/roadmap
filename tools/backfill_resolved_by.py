#!/usr/bin/env python3
"""RESOLVED-BY-1 - one-time backfill of the Resolved by tag (Support / Development) from Jira.

The server sync tags newly finished items going forward; this fills in history. It reuses the shipped
server._resolved_by_fill (same rules, same blob-only json_set writes, a person's pick never touched).

  Dry run (default, no writes):  sudo /opt/roadmap/venv/bin/python tools/backfill_resolved_by.py --team development --days 365 --teams Support
  Apply:                         ... --commit

--teams previews (dry run) or switches on (commit) config `resolvedByTeams` for those Teams; without it the
Org's current setting is used. --commit first backs up the Org DB with the SQLite online-backup API
(WAL-safe under the live workers) to roadmap.db.rb-bak-<ts>; restoring that file is the rollback.
Importing server runs its normal idempotent boot, exactly as a service restart does.
"""
import argparse
import datetime
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", required=True)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--teams", default="", help="comma-separated Teams to tag, e.g. Support")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    import server
    tagged = [t.strip() for t in a.teams.split(",") if t.strip()] or None
    if tagged:
        known = server._cfg_val(a.team, "developers", []) or []
        bad = [t for t in tagged if t not in known]
        if bad:
            sys.exit(f"Unknown Team(s) on {a.team}: {bad}. Known: {known}")
    if not a.commit:
        out = server._resolved_by_fill(a.team, "resolved-by-backfill", days=a.days, dry=True, tagged_override=tagged)
        print(json.dumps(out, indent=2))
        print("DRY RUN - nothing written. Re-run with --commit to apply.")
        return
    path = server.team_db_path(a.team)
    bak = path + ".rb-bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    src = sqlite3.connect(path)
    dst = sqlite3.connect(bak)
    with dst:
        src.backup(dst)
    src.close(); dst.close()
    print("backup:", bak)
    if tagged:
        with server.db(a.team) as c:
            old = c.execute("SELECT value FROM config WHERE key='resolvedByTeams'").fetchone()
            c.execute("INSERT OR REPLACE INTO config(key, value) VALUES('resolvedByTeams', ?)", (json.dumps(tagged),))
            server._write_audit_c(c, "config:resolvedByTeams", "resolved-by-backfill", None, "",
                                  changes={"from": json.loads(old["value"]) if old else [], "to": tagged})
        print("resolvedByTeams set to", tagged)
    out = server._resolved_by_fill(a.team, "resolved-by-backfill", days=a.days, dry=False)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
