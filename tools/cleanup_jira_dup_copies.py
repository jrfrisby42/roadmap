#!/usr/bin/env python3
"""JIRA-DEDUPE-1 cleanup - one-off removal of the 22 hidden sync-children copies on `development`.

WHY: before JIRA-DEDUPE-1 (6.58.3) sync-children only deduped against the parent's OWN children, so
re-syncing the recurring item #6 created a second (and for two keys a third) hidden child for the same
Jira issue. 6.58.3 stops new ones; this removes the existing copies. J.R. decided 2026-10-01: delete the
later copies (keep the lowest id per key). Out of scope by the same decision: the 41 keys with one
hidden child + one visible FN support item, and the 33 keys shared by several distinct FN tickets.

MECHANISM: the same rows delete_project removes (comments, activities, item_assets, item_departments,
projects), one `delete` audit row per item tagged with the operation, all in ONE BEGIN IMMEDIATE txn.
Plain sqlite3 - the server module is never imported, so boot() never runs.

SAFETY:
- DRY-RUN by default (read-only connection). --commit is required to write.
- Every target is re-verified INSIDE the write txn before anything is deleted: it exists, is hidden, is
  a child of #6, still carries its Jira key, the KEEP item still exists with the same key, and no other
  item references it (parent / recurrence_parent / requires / children). Any mismatch aborts the whole
  run with nothing written.
- --commit first takes a WAL-safe online backup (Connection.backup) - the entire rollback plan.
- Idempotent: an already-deleted target is reported and skipped, so a second run is a no-op.

Run ON PROD:  sudo python3 /tmp/cleanup_jira_dup_copies.py [--commit]
"""
import argparse
import datetime
import json
import sqlite3
import sys

DB = "/data/tenants/development/roadmap.db"
PARENT = 6
ACTOR = "System"
OP = "jira-dedupe-1:cleanup-2026-10-01"

# (drop_id, jira_key, keep_id) - keep = lowest id holding the key under #6.
TARGETS = [
    (118, "FRAZ-10435", 16), (119, "FRAZ-10550", 17), (120, "FRAZ-10602", 18), (121, "FRAZ-10609", 45),
    (122, "FRAZ-10630", 38), (123, "FRAZ-10631", 44), (124, "FRAZ-10643", 89), (125, "FRAZ-10650", 41),
    (105, "FRAZ-10652", 42), (126, "FRAZ-10652", 42), (127, "FRAZ-10664", 49), (128, "FRAZ-10667", 50),
    (109, "FRAZ-10668", 87), (129, "FRAZ-10668", 87), (130, "FRAZ-10669", 86), (131, "FRAZ-10671", 90),
    (132, "FRAZ-10673", 91), (133, "FRAZ-10674", 92), (114, "FRAZ-10675", 93), (134, "FRAZ-10675", 93),
    (135, "FRAZ-10676", 94), (136, "FRAZ-10677", 95),
]


def _blob(c, pid):
    r = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
    return json.loads(r[0]) if r else None


def _keys(item):
    return {str(k).strip().upper() for k in (item.get("jiraTickets") or [])}


def _as_list(v):
    """requires/children are stored as a list, a single id, or null depending on the item's age."""
    if v in (None, ""):
        return []
    return v if isinstance(v, list) else [v]


def _referrers(c, ids):
    """Other items that point at any of `ids` via parent / recurrence_parent / requires / children."""
    hits = []
    for pid, data in c.execute("SELECT id, data FROM projects"):
        if pid in ids:
            continue
        p = json.loads(data)
        refs = set()
        for f in ("parent", "recurrence_parent"):
            try:
                if p.get(f) not in (None, "") and int(p.get(f)) in ids:
                    refs.add(f)
            except (TypeError, ValueError):
                pass
        for r in _as_list(p.get("requires")):
            rid = r.get("id") if isinstance(r, dict) else r
            try:
                if int(rid) in ids:
                    refs.add("requires")
            except (TypeError, ValueError):
                pass
        for ch in _as_list(p.get("children")):
            try:
                if int(ch) in ids:
                    refs.add("children")
            except (TypeError, ValueError):
                pass
        if refs:
            hits.append((pid, sorted(refs)))
    return hits


def verify(c):
    """Return (todo, already_gone, problems)."""
    todo, gone, problems = [], [], []
    for drop, key, keep in TARGETS:
        d = _blob(c, drop)
        if d is None:
            gone.append(drop)
            continue
        k = _blob(c, keep)
        if not d.get("hidden"):
            problems.append(f"#{drop}: not hidden")
        if str(d.get("parent")) != str(PARENT):
            problems.append(f"#{drop}: parent is {d.get('parent')!r}, expected {PARENT}")
        if key not in _keys(d):
            problems.append(f"#{drop}: no longer carries {key} ({sorted(_keys(d))})")
        if k is None:
            problems.append(f"#{drop}: keep item #{keep} is missing")
        elif key not in _keys(k):
            problems.append(f"#{drop}: keep item #{keep} no longer carries {key}")
        todo.append((drop, key, keep, d))
    ids = {t[0] for t in todo}
    for pid, refs in _referrers(c, ids):
        problems.append(f"item #{pid} references a target via {refs}")
    return todo, gone, problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="actually delete (default: read-only dry-run)")
    ap.add_argument("--db", default=DB)
    a = ap.parse_args()

    if not a.commit:
        c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
        todo, gone, problems = verify(c)
        for drop, key, keep, d in todo:
            n_act = c.execute("SELECT COUNT(*) FROM activities WHERE item_id=?", (drop,)).fetchone()[0]
            print(f"would delete #{drop} {d.get('itemKey')} [{key}] keep #{keep} | {d.get('status')} | "
                  f"{n_act} activity rows | {(d.get('name') or '')[:50]}")
        print(f"DRY-RUN: {len(todo)} to delete, {len(gone)} already gone, {len(problems)} problems")
        for p in problems:
            print("  PROBLEM:", p)
        return 1 if problems else 0

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{a.db}.jiradup-bak-{ts}"
    src = sqlite3.connect(a.db)
    dst = sqlite3.connect(backup)
    with dst:
        src.backup(dst)
    dst.close()
    print("backup ->", backup)

    c = src
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("BEGIN IMMEDIATE")
    try:
        todo, gone, problems = verify(c)
        if problems:
            raise RuntimeError("verification failed inside the txn: " + "; ".join(problems))
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        for drop, key, keep, d in todo:
            for tbl in ("comments", "activities", "item_assets", "item_departments"):
                c.execute(f"DELETE FROM {tbl} WHERE item_id=?", (drop,))
            c.execute("DELETE FROM projects WHERE id=?", (drop,))
            c.execute("INSERT INTO audit_log(ts,username,action,project_id,project_name,changes) VALUES(?,?,?,?,?,?)",
                      (now, ACTOR, "delete", drop, d.get("name", ""),
                       json.dumps({"operation": OP, "reason": "duplicate hidden sync-children copy",
                                   "jiraKey": key, "keptId": keep, "itemKey": d.get("itemKey"),
                                   "backup": backup.rsplit("/", 1)[-1]})))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    # post-verify
    left = [t[0] for t in TARGETS if _blob(c, t[0]) is not None]
    kept_ok = all(_blob(c, t[2]) is not None for t in TARGETS)
    print(f"COMMITTED: deleted {len(todo)}, already gone {len(gone)}; remaining targets {left}; keeps intact {kept_ok}")
    return 0 if (not left and kept_ok) else 2


if __name__ == "__main__":
    sys.exit(main())
