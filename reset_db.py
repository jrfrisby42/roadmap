#!/usr/bin/env python3
"""
Frazil Roadmap — Database Reset Script
=======================================
Wipes ALL team databases and creates a fresh 'development' team.

Usage:
    python reset_db.py                        # interactive confirmation
    python reset_db.py --confirm              # skip confirmation prompt
    python reset_db.py --password mypass      # set admin password (default: prompted)
    python reset_db.py --confirm --password mypass

WARNING: This is irreversible. All projects, activities, comments,
         audit logs, and config will be permanently deleted.
"""

import argparse, hashlib, hmac, json, os, re, secrets, shutil, sqlite3, sys
from datetime import datetime, timezone

TENANTS_DIR  = "/data/tenants"
DEFAULT_TEAM = "development"

# ── Password hashing (mirrors server.py) ─────────────────────────────────────
try:
    import bcrypt as _bcrypt
    def hash_password(plain: str) -> str:
        return _bcrypt.hashpw(plain.encode(), _bcrypt.gensalt(12)).decode()
    HASH_MODE = "bcrypt"
except ImportError:
    def hash_password(plain: str) -> str:
        salt = secrets.token_hex(16)
        h = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt.encode(), 260000).hex()
        return f"$pbkdf2${salt}${h}"
    HASH_MODE = "pbkdf2_hmac"

# ── Default configuration for fresh team ─────────────────────────────────────
DEFAULT_CONFIG = {
    "developers": [],
    "statuses": [
        "Backlogged",
        "Planned",
        "In Progress",
        "In Testing",
        "Released",
    ],
    "changeReasons": [
        "Scope Change",
        "Resource Constraint",
        "Technical Blocker",
        "External Dependency",
        "Priority Shift",
        "Revised Estimate",
        "Partner Delays",
        "Other",
    ],
    "deferReasons": [
        "Not Ready",
        "Deprioritised",
        "Waiting on External",
        "Resource Unavailable",
        "Other",
    ],
    "delayReasons": [],  # legacy — kept for backward compat
    "products": [
        {"name": "Product", "builtin": True},
    ],
    "types": [
        {"name": "Feature",      "color": ""},
        {"name": "Enhancement",  "color": ""},
        {"name": "Maintenance",  "color": ""},
        {"name": "Bug Fix",      "color": ""},
    ],
    "ownerCapacity":          {},
    "statusIgnoreConflicts":  {},
    "typeIgnoreConflicts":    {},
    "statusIsActive":         {"In Progress": True, "In Testing": True},
    "statusIsTerminal":       {"Released": True},
    "statusIsDefault":        {"Backlogged": True},
    "statusIsDeferred":       {"Backlogged": True},
    "jiraProjectMapping":     {},
    "jiraStatusMapping":      {},
    "jiraTypeMapping":        {},
    "jiraSyncConfig":         {"enabled": False, "intervalMinutes": 30},
}

# ── DB schema (mirrors server.py) ─────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    username     TEXT NOT NULL DEFAULT '',
    action       TEXT NOT NULL,
    project_id   INTEGER,
    project_name TEXT,
    changes      TEXT
);
CREATE TABLE IF NOT EXISTS activities (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_type  TEXT NOT NULL,
    source         TEXT NOT NULL DEFAULT 'System',
    item_id        INTEGER,
    item_name      TEXT,
    owner          TEXT,
    project        TEXT,
    created_by     TEXT,
    created_ts     TEXT,
    read_by        TEXT,
    read_ts        TEXT,
    resolved_by    TEXT,
    resolved_ts    TEXT,
    action_taken   TEXT,
    previous_value TEXT,
    new_value      TEXT,
    note           TEXT,
    status         TEXT NOT NULL DEFAULT 'Open',
    message        TEXT
);
CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_ts TEXT NOT NULL,
    edited_ts  TEXT
);
"""

def wipe_all_teams():
    """Delete every team directory under TENANTS_DIR."""
    if not os.path.isdir(TENANTS_DIR):
        print(f"  No tenants directory found at {TENANTS_DIR} — nothing to wipe.")
        return
    removed = 0
    for entry in os.listdir(TENANTS_DIR):
        path = os.path.join(TENANTS_DIR, entry)
        if os.path.isdir(path):
            shutil.rmtree(path)
            print(f"  ✗ Removed team: {entry}")
            removed += 1
    if removed == 0:
        print("  No existing teams found.")
    else:
        print(f"  Wiped {removed} team(s).")

def create_team(team: str, admin_password: str):
    """Create a fresh team DB with default config and admin user."""
    team_dir = os.path.join(TENANTS_DIR, team)
    db_path  = os.path.join(team_dir, "roadmap.db")
    os.makedirs(team_dir, exist_ok=True)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)

    # Write config rows
    cfg = dict(DEFAULT_CONFIG)
    cfg["users"] = [
        {
            "username":          "admin",
            "password":          hash_password(admin_password),
            "builtin":           True,
            "role":              "admin",
            "mustChangePassword": False,
        }
    ]
    for k, v in cfg.items():
        con.execute(
            "INSERT OR REPLACE INTO config(key,value) VALUES(?,?)",
            (k, json.dumps(v))
        )

    # Seed audit log
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    con.execute(
        "INSERT INTO audit_log(ts,username,action,project_name,changes) VALUES(?,?,?,?,?)",
        (ts, "system", "db:reset", "system",
         json.dumps({"note": "Database reset by reset_db.py", "version": "3.2.0"}))
    )

    con.commit()
    con.close()
    print(f"  ✓ Team '{team}' created at {db_path}")

def main():
    parser = argparse.ArgumentParser(
        description="Reset the Frazil Roadmap database to a clean state."
    )
    parser.add_argument("--confirm",  action="store_true",
                        help="Skip the interactive confirmation prompt")
    parser.add_argument("--password", type=str, default=None,
                        help="Admin password for the new team (prompted if not provided)")
    parser.add_argument("--team",     type=str, default=DEFAULT_TEAM,
                        help=f"Team slug to create (default: {DEFAULT_TEAM})")
    args = parser.parse_args()

    team = re.sub(r"[^a-z0-9]", "", args.team.lower())
    if not team:
        print(f"Invalid team name: {args.team}")
        sys.exit(1)

    print()
    print("=" * 58)
    print("  Frazil Roadmap — Database Reset")
    print("=" * 58)
    print(f"  Tenants dir : {TENANTS_DIR}")
    print(f"  New team    : {team}")
    print(f"  Hash mode   : {HASH_MODE}")
    print()
    print("  ⚠  WARNING: This will permanently delete ALL existing")
    print("     team data and cannot be undone.")
    print()

    if not args.confirm:
        answer = input("  Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            print("  Aborted.")
            sys.exit(0)

    # Get admin password
    admin_password = args.password
    if not admin_password:
        import getpass
        while True:
            pw1 = getpass.getpass("  New admin password: ")
            pw2 = getpass.getpass("  Confirm password  : ")
            if pw1 == pw2 and pw1:
                admin_password = pw1
                break
            print("  Passwords do not match or are empty. Try again.")

    print()
    print("  Wiping existing databases…")
    wipe_all_teams()

    print()
    print(f"  Creating fresh '{team}' team…")
    create_team(team, admin_password)

    print()
    print("=" * 58)
    print("  Reset complete.")
    print(f"  Login: team={team}  username=admin  password=<set above>")
    print()
    print("  Next step: restart the server")
    print("    sudo systemctl restart roadmap")
    print("=" * 58)
    print()

if __name__ == "__main__":
    main()
