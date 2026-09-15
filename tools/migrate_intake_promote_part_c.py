#!/usr/bin/env python3
"""INTAKE-PROMOTE-1 Part C - one-off migration of legacy intake/{team}/ attachments to items/{pid}/.

WHY: Part B (shipped) copies NEW submissions on submit, but items created before Part B still carry
intake/{team}/ keys. The intake/ prefix cannot be lifecycle-expired while any live item references it,
so those legacy objects must be copied to items/{pid}/ and their records rewritten BEFORE J.R. applies
the intake/ lifecycle rule. Applying the rule first would delete live data.

MECHANISM: reuses the SHIPPED, verified server._promote_intake_attachments (same s3.copy_object, same
_attachment_key dest, same SSE-KMS, same drop-on-copy-failure) - no reimplemented copy. It then rewrites
each item's $.attachments with a surgical json_set (never a wholesale blob rewrite). The source intake/
object is NOT deleted (the lifecycle rule does that later, preserving a reversal window).

SAFETY (pre-run corrections applied):
- DRY-RUN by default and TRULY read-only: dry-run does NOT import server, so boot() never runs and no
  config keys are written (Item 4). --commit imports server (boot's idempotent config backfill runs then,
  exactly as on any service restart; it is unrelated to attachments and the per-team backup below still
  captures the exact attachment state for rollback).
- --commit backs up each team DB with the SQLite online-backup API (Connection.backup), which is
  WAL-safe under the two live gunicorn workers - a raw shutil.copy could capture a torn snapshot, and the
  backup is the entire rollback plan (Item 1).
- After the writes, per-team VERIFY (Item 2): (a) every touched item's attachment COUNT is unchanged
  (no drop, no duplicate), and (b) each item blob is byte-identical apart from $.attachments (proves
  json_set was surgical). Plus: zero intake-keyed attachments remain.
- One audit_log row per team recording the migration + counts, since json_set bypasses update_project
  and would otherwise leave no trail (Item 3).
- Idempotent: an items/-keyed attachment is skipped, so a second run is a no-op.
- The rendered/thumbnail screenshot check on both teams is a browser step run AFTER this, not here.

Run ON PROD with the app venv:  sudo /opt/roadmap/venv/bin/python tools/migrate_intake_promote_part_c.py [--commit]
"""
import argparse
import collections
import datetime
import glob
import json
import os
import sqlite3
import sys

DEFAULT_TENANTS = "/data/tenants"


def _dbs(tenants):
    return sorted(glob.glob(os.path.join(tenants, "*", "roadmap.db")))


def _connect_ro(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _needs(atts, team):
    return [a for a in (atts or [])
            if isinstance(a, dict) and str(a.get("key") or "").startswith(f"intake/{team}/")]


def _blob_sans_atts(p):
    """Canonical JSON of the item blob with $.attachments removed - for the surgical-write proof."""
    return json.dumps({k: v for k, v in p.items() if k != "attachments"}, sort_keys=True)


def dry_run(tenants):
    plan = collections.Counter()
    for db in _dbs(tenants):
        team = os.path.basename(os.path.dirname(db))
        con = _connect_ro(db)
        items = 0
        for row in con.execute("SELECT id, data FROM projects").fetchall():
            try:
                p = json.loads(row["data"])
            except Exception:
                continue
            nd = _needs(p.get("attachments"), team)
            if nd:
                items += 1
                plan[team] += len(nd)
        con.close()
        if items:
            print(f"[{team}] items needing migration: {items}  attachments: {plan[team]}")
    print("---")
    print("PLAN_PRECOUNT_BY_TEAM", dict(plan), "TOTAL", sum(plan.values()))
    print("DRY-RUN only - server NOT imported, boot() NOT run, nothing copied or written. "
          "Re-run with --commit to migrate.")


def commit_run(ts):
    import server  # boot() runs here - documented; idempotent config backfill, unrelated to attachments
    tenants = getattr(server, "TENANTS_DIR", DEFAULT_TENANTS)
    grand = collections.Counter()
    for db in _dbs(tenants):
        team = os.path.basename(os.path.dirname(db))
        # ---- pre-capture (read-only): targets + per-item baseline (count, blob-sans-attachments) ----
        con = _connect_ro(db)
        targets, pre = [], {}
        for row in con.execute("SELECT id, data FROM projects").fetchall():
            try:
                p = json.loads(row["data"])
            except Exception:
                continue
            if _needs(p.get("attachments"), team):
                targets.append((row["id"], p.get("attachments")))
                pre[row["id"]] = (len(p.get("attachments") or []), _blob_sans_atts(p))
        con.close()
        if not targets:
            continue
        pre_intake = sum(len(_needs(a, team)) for _, a in targets)
        print(f"[{team}] migrating {len(targets)} items / {pre_intake} intake-keyed attachments")

        # ---- backup (WAL-safe online backup - the rollback plan) ----
        backup = f"{db}.partc-bak-{ts}"
        _src = sqlite3.connect(db)
        _dst = sqlite3.connect(backup)
        with _dst:
            _src.backup(_dst)   # sqlite online backup: consistent even under concurrent WAL writers
        _src.close()
        _dst.close()
        print(f"[{team}] DB backed up (online backup) -> {backup}")

        # ---- migrate: reuse the shipped promote, rewrite $.attachments surgically ----
        dropped = 0
        for pid, atts in targets:
            promoted, drp = server._promote_intake_attachments(team, pid, atts)
            dropped += len(drp)
            if promoted != atts:
                with server.db(team) as c:
                    c.execute("UPDATE projects SET data=json_set(data, '$.attachments', json(?)) WHERE id=?",
                              (json.dumps(promoted), pid))

        # ---- verify (Item 2): count unchanged (a) + blob byte-identical apart from $.attachments (b) ----
        con = _connect_ro(db)
        count_ok = count_bad = blob_ok = blob_bad = still_intake = 0
        for pid, (precount, preblob) in pre.items():
            row = con.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
            p = json.loads(row["data"])
            postcount = len(p.get("attachments") or [])
            count_ok, count_bad = (count_ok + 1, count_bad) if postcount == precount else (count_ok, count_bad + 1)
            blob_ok, blob_bad = (blob_ok + 1, blob_bad) if _blob_sans_atts(p) == preblob else (blob_ok, blob_bad + 1)
            still_intake += len(_needs(p.get("attachments"), team))
        con.close()

        # ---- audit row (Item 3): one summary per team; json_set bypassed update_project ----
        migrated = pre_intake - still_intake - dropped
        try:
            server.write_audit(team, "intake:migrate-part-c", "System", None, "", changes={
                "items": len(targets), "attachmentsMigrated": migrated, "dropped": dropped,
                "backup": os.path.basename(backup)})
        except Exception as e:
            print(f"[{team}] audit write failed (non-fatal): {e}")

        print(f"[{team}] RESULT migrated={migrated} dropped={dropped} intake_remaining={still_intake} "
              f"| VERIFY count_ok={count_ok} count_bad={count_bad} blob_ok={blob_ok} blob_bad={blob_bad}")
        grand["migrated"] += migrated
        grand["dropped"] += dropped
        grand["intake_remaining"] += still_intake
        grand["count_bad"] += count_bad
        grand["blob_bad"] += blob_bad
    print("---")
    print("GRAND", dict(grand))
    ok = grand["dropped"] == 0 and grand["intake_remaining"] == 0 and grand["count_bad"] == 0 and grand["blob_bad"] == 0
    print("MIGRATION_CLEAN" if ok else "MIGRATION_HAS_ISSUES_REVIEW_ABOVE")
    print("Sources NOT deleted - reversal = restore the per-team .partc-bak DB until the intake/ "
          "lifecycle rule runs. Verify streaming + the rendered screenshot, THEN J.R. applies the rule.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="actually copy + rewrite (default is a read-only dry-run)")
    ap.add_argument("--tenants", default=DEFAULT_TENANTS, help="tenants dir (dry-run only; --commit uses server.TENANTS_DIR)")
    args = ap.parse_args()
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"=== INTAKE-PROMOTE-1 Part C migration [{'COMMIT' if args.commit else 'DRY-RUN'}] {ts} ===")
    if args.commit:
        commit_run(ts)
    else:
        dry_run(args.tenants)


if __name__ == "__main__":
    main()
