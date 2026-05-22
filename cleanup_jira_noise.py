"""
Cleanup script: Remove no-change Jira sync entries from the audit log.

Deletes audit_log rows where:
  - action = 'jira:pull'
  - statusChanged is false (no status update)
  - no feature flag changes recorded

These are the entries the server now suppresses going forward.
Run once to clean up historical noise.

Usage:
  python cleanup_jira_noise.py              # dry run (shows what would be deleted)
  python cleanup_jira_noise.py --apply      # actually delete
  python cleanup_jira_noise.py --team demo  # only clean one team
"""
import sys, os, json, sqlite3

TENANTS_DIR = "/data/tenants"
DRY_RUN = "--apply" not in sys.argv
TARGET_TEAM = None

for i, arg in enumerate(sys.argv):
    if arg == "--team" and i + 1 < len(sys.argv):
        TARGET_TEAM = sys.argv[i + 1].strip().lower()

if DRY_RUN:
    print("═" * 60)
    print("  DRY RUN — no data will be deleted")
    print("  Add --apply to actually delete")
    print("═" * 60)
    print()

# Find all team databases
if not os.path.isdir(TENANTS_DIR):
    print(f"ERROR: {TENANTS_DIR} not found")
    sys.exit(1)

teams = []
for d in sorted(os.listdir(TENANTS_DIR)):
    db_path = os.path.join(TENANTS_DIR, d, "roadmap.db")
    if os.path.isfile(db_path):
        if TARGET_TEAM and d != TARGET_TEAM:
            continue
        teams.append((d, db_path))

if not teams:
    print("No team databases found" + (f" (filtered to: {TARGET_TEAM})" if TARGET_TEAM else ""))
    sys.exit(1)

total_audit_deleted = 0
total_acts_deleted = 0

for team_name, db_path in teams:
    print(f"\n{'─' * 50}")
    print(f"  Team: {team_name}")
    print(f"  DB:   {db_path}")
    print(f"{'─' * 50}")

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    # ── Audit log cleanup ─────────────────────────────────────────────────
    # Find jira:pull entries where statusChanged is false and no FF changes
    rows = con.execute(
        "SELECT id, ts, project_name, changes FROM audit_log WHERE action = 'jira:pull'"
    ).fetchall()

    to_delete = []
    for r in rows:
        if not r["changes"]:
            # No changes JSON at all — definitely noise
            to_delete.append(r["id"])
            continue
        try:
            ch = json.loads(r["changes"])
        except (json.JSONDecodeError, TypeError):
            continue  # malformed — leave it alone

        status_changed = ch.get("statusChanged", False)
        ff_changed = ch.get("ffChanged", False)

        if not status_changed and not ff_changed:
            to_delete.append(r["id"])

    print(f"  Audit log: {len(rows)} total jira:pull entries")
    print(f"  Audit log: {len(to_delete)} are no-change (will {'DELETE' if not DRY_RUN else 'be deleted'})")
    print(f"  Audit log: {len(rows) - len(to_delete)} have real changes (keeping)")

    if to_delete and not DRY_RUN:
        # Delete in batches of 500
        for i in range(0, len(to_delete), 500):
            batch = to_delete[i:i+500]
            placeholders = ",".join("?" * len(batch))
            con.execute(f"DELETE FROM audit_log WHERE id IN ({placeholders})", batch)
        con.commit()
        print(f"  ✓ Deleted {len(to_delete)} audit entries")

    total_audit_deleted += len(to_delete)

    # ── Activities cleanup ────────────────────────────────────────────────
    # Remove Jira Sync activities that are skipped regressions (no-change noise)
    act_rows = con.execute(
        "SELECT id, new_value, message FROM activities WHERE activity_type = 'Jira Sync'"
    ).fetchall()

    acts_to_delete = []
    for r in act_rows:
        # Check new_value JSON for skipped flag
        if r["new_value"]:
            try:
                nv = json.loads(r["new_value"])
                if nv.get("skipped"):
                    acts_to_delete.append(r["id"])
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
        # Also catch message-based patterns
        msg = r["message"] or ""
        if "skipped (regression" in msg or "status unchanged" in msg:
            acts_to_delete.append(r["id"])

    print(f"  Activities: {len(act_rows)} total Jira Sync entries")
    print(f"  Activities: {len(acts_to_delete)} are no-change noise (will {'DELETE' if not DRY_RUN else 'be deleted'})")
    print(f"  Activities: {len(act_rows) - len(acts_to_delete)} have real changes (keeping)")

    if acts_to_delete and not DRY_RUN:
        for i in range(0, len(acts_to_delete), 500):
            batch = acts_to_delete[i:i+500]
            placeholders = ",".join("?" * len(batch))
            con.execute(f"DELETE FROM activities WHERE id IN ({placeholders})", batch)
        con.commit()
        print(f"  ✓ Deleted {len(acts_to_delete)} activity entries")

    total_acts_deleted += len(acts_to_delete)
    con.close()

print(f"\n{'═' * 60}")
if DRY_RUN:
    print(f"  DRY RUN COMPLETE")
    print(f"  Would delete {total_audit_deleted} audit entries across {len(teams)} team(s)")
    print(f"  Would delete {total_acts_deleted} activity entries across {len(teams)} team(s)")
    print(f"  Run with --apply to execute")
else:
    print(f"  CLEANUP COMPLETE")
    print(f"  Deleted {total_audit_deleted} audit entries across {len(teams)} team(s)")
    print(f"  Deleted {total_acts_deleted} activity entries across {len(teams)} team(s)")
print(f"{'═' * 60}")
