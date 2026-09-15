#!/usr/bin/env python3
"""INTAKE-PROMOTE-1 Part C - one-off migration of legacy intake/{team}/ attachments to items/{pid}/.

WHY: Part B (shipped) copies NEW submissions on submit, but items created before Part B still carry
intake/{team}/ keys. The intake/ prefix cannot be lifecycle-expired while any live item references it,
so those legacy objects must be copied to items/{pid}/ and their records rewritten BEFORE J.R. applies
the intake/ lifecycle rule. Applying the rule first would delete live data.

MECHANISM: reuses the SHIPPED, verified server._promote_intake_attachments (same s3.copy_object, same
_attachment_key dest, same SSE-KMS handling, same drop-on-copy-failure) - this script does NOT
reimplement the copy. It then rewrites each item's $.attachments with a surgical json_set (never a
wholesale blob rewrite, so concurrent state is not clobbered). The source intake/ object is NOT deleted
(the lifecycle rule does that later, preserving a reversal window).

SAFETY:
- DRY-RUN by default. Nothing is copied or written unless --commit is passed.
- --commit backs up each team DB (byte-exact cp, filename printed) BEFORE its first write.
- Idempotent: an items/-keyed attachment is skipped (pass-through), so a second run is a no-op.
- Expected counts are printed before and compared after; per-team outcome counts are summarised.
- Run ON PROD with the app venv:  sudo /opt/roadmap/venv/bin/python tools/migrate_intake_promote_part_c.py [--commit]
"""
import argparse
import collections
import datetime
import glob
import json
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, "/opt/roadmap")
import server  # noqa: E402  (needs the app venv: fastapi + boto3)

TENANTS = getattr(server, "TENANTS_DIR", "/data/tenants")


def _team_dbs():
    return sorted(glob.glob(os.path.join(TENANTS, "*", "roadmap.db")))


def _needs(atts, team):
    return [a for a in (atts or [])
            if isinstance(a, dict) and str(a.get("key") or "").startswith(f"intake/{team}/")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="actually copy + rewrite (default is dry-run)")
    args = ap.parse_args()
    mode = "COMMIT" if args.commit else "DRY-RUN"
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"=== INTAKE-PROMOTE-1 Part C migration [{mode}] {ts} ===")

    plan = collections.Counter()          # per-team attachments that need migration (pre-count)
    outcome = collections.Counter()       # copied_rewritten / already_items / source_missing_or_failed
    per_team = collections.Counter()

    for db in _team_dbs():
        team = os.path.basename(os.path.dirname(db))
        # pre-count (read-only)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        targets = []  # (pid, atts)
        for row in con.execute("SELECT id, data FROM projects").fetchall():
            try:
                p = json.loads(row["data"])
            except Exception:
                continue
            if _needs(p.get("attachments"), team):
                targets.append((row["id"], p.get("attachments")))
                plan[team] += len(_needs(p.get("attachments"), team))
        con.close()
        if not targets:
            continue
        print(f"[{team}] items needing migration: {len(targets)}  attachments: {plan[team]}")

        if not args.commit:
            continue

        # --- COMMIT path: backup this team DB before the first write ---
        backup = f"{db}.partc-bak-{ts}"
        shutil.copy2(db, backup)
        print(f"[{team}] DB backed up -> {backup}")

        for pid, atts in targets:
            try:
                promoted, dropped = server._promote_intake_attachments(team, pid, atts)  # copies intake/->items/
            except Exception as e:
                print(f"[{team}] item {pid}: promote raised (should not) - skipped: {e}")
                outcome["failed"] += len(_needs(atts, team))
                continue
            for a in dropped:
                outcome["source_missing_or_failed"] += 1
            newly = sum(1 for r in promoted if str(r.get("key") or "").startswith(f"items/{pid}/"))
            # rewrite ONLY if something changed, via a surgical json_set (no wholesale blob rewrite)
            if promoted != atts:
                with server.db(team) as c:
                    c.execute("UPDATE projects SET data=json_set(data, '$.attachments', json(?)) WHERE id=?",
                              (json.dumps(promoted), pid))
            # count how many are now items/-keyed vs were already
            for a in atts:
                k = str(a.get("key") or "")
                if k.startswith(f"intake/{team}/"):
                    outcome["copied_rewritten"] += 1  # attempted; drops counted above are a subset
                elif k.startswith("items/"):
                    outcome["already_items"] += 1
            per_team[team] += newly

    print("---")
    print("PLAN_PRECOUNT_BY_TEAM", dict(plan), "TOTAL", sum(plan.values()))
    if args.commit:
        print("OUTCOME", dict(outcome))
        print("NOW_ITEMS_KEYED_BY_TEAM", dict(per_team))
        print("NOTE: sources NOT deleted - reversal = restore the per-team .partc-bak DB (or the old keys) "
              "until the intake/ lifecycle rule runs. Verify streaming, THEN J.R. applies the rule.")
    else:
        print("DRY-RUN only - nothing copied or written. Re-run with --commit to migrate.")


if __name__ == "__main__":
    main()
