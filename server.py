"""
Frazil Roadmap — Multi-tenant FastAPI + SQLite backend
=======================================================
Run:   python server.py
Then open:  http://localhost:8000

Teams:
  Each team gets its own SQLite database at /data/tenants/{team}/roadmap.db
  The current team is selected at login and sent as X-Team header on every request.
  To add a new team:  python server.py --new-team acme

Jira integration:
  Create a .env file next to server.py with:
    JIRA_BASE_URL=https://freezingpointllc.atlassian.net
    JIRA_EMAIL=you@example.com
    JIRA_API_TOKEN=your_api_token_here
"""

import json, os, re, sqlite3, base64, time, hashlib, hmac, sys, html, logging, secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from fastapi import FastAPI, HTTPException, Body, Request as FRequest, Header, Depends, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import Optional, List

log = logging.getLogger("frazil")
logging.basicConfig(level=logging.INFO)

# ── Handle --new-team CLI ─────────────────────────────────────────────────────
if "--new-team" in sys.argv:
    idx = sys.argv.index("--new-team")
    if idx + 1 >= len(sys.argv):
        print("Usage: python server.py --new-team <teamname>")
        sys.exit(1)
    raw = sys.argv[idx + 1]
    slug = re.sub(r"[^a-z0-9]", "", raw.lower())
    if not slug:
        print(f"Invalid team name: {raw}")
        sys.exit(1)
    team_dir = os.path.join("/data/tenants", slug)
    if os.path.exists(team_dir):
        print(f"Team '{slug}' already exists at {team_dir}")
        sys.exit(0)
    os.makedirs(team_dir, exist_ok=True)
    _init_pw = secrets.token_urlsafe(12)
    print(f"Team '{slug}' created at {team_dir}")
    print(f"Initial login: admin / {_init_pw}")
    print(f"  (password change will be required on first login)")
    print(f"Link: https://roadmap.yourdomain.com?team={slug}")
    # Write the initial password into a temp file so init_team_db can pick it up
    _pw_file = os.path.join(team_dir, ".init_password")
    with open(_pw_file, "w") as _f:
        _f.write(_init_pw)
    sys.exit(0)

# ── Load .env ─────────────────────────────────────────────────────────────────
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# ── Jira config ───────────────────────────────────────────────────────────────
JIRA_BASE  = os.environ.get("JIRA_BASE_URL", "https://freezingpointllc.atlassian.net").rstrip("/")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_TOKEN = os.environ.get("JIRA_API_TOKEN", "")

def _jira_auth_header():
    return "Basic " + base64.b64encode(f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()).decode()

def jira_configured():
    return bool(JIRA_EMAIL and JIRA_TOKEN)

# Jira custom field ID for the "Feature Flag" label field
JIRA_FF_FIELD = "customfield_10064"


def round_up_to_quarter(val) -> float:
    """Round a parallelResources value UP to the nearest 0.25.
    Minimum return value is 1.0.

    Examples:
      1.01 → 1.25,  1.26 → 1.50,  2.62 → 2.75,  3.00 → 3.00
    """
    try:
        n = float(val)
    except (TypeError, ValueError):
        return 1.0
    if n <= 0:
        return 1.0
    import math
    r = math.ceil(n * 4) / 4
    return max(1.0, r)


def _fetch_jira_feature_flags(ticket: str) -> set:
    """Fetch all unique Feature Flag labels (customfield_10064) from a Jira ticket
    and its entire child hierarchy (all descendants, any depth).
    Uses JQL 'issueFunction in subtasksOf' style or parent-chain queries.
    Returns a set of distinct flag strings."""
    flags = set()
    try:
        from urllib.parse import quote as _quote
        data = _jira_req("GET", f"/rest/api/3/issue/{ticket}?fields=issuetype,subtasks,{JIRA_FF_FIELD}")
        f = data.get("fields", {})

        # Flags on the root ticket itself
        root_flags = f.get(JIRA_FF_FIELD) or []
        if isinstance(root_flags, list):
            flags.update(str(fl).strip() for fl in root_flags if fl)

        issue_type      = f.get("issuetype", {}).get("name", "").lower()
        hierarchy_level = f.get("issuetype", {}).get("hierarchyLevel", 0)

        if "epic" in issue_type:
            # Epic (level 1): fetch direct children via JQL
            jql_enc = _quote(f"parent={ticket} ORDER BY created ASC")
            search = _jira_req("GET", f"/rest/api/3/search/jql?jql={jql_enc}&fields={JIRA_FF_FIELD}&maxResults=100")
            for issue in (search.get("issues") or []):
                child_flags = ((issue.get("fields") or {}).get(JIRA_FF_FIELD)) or []
                if isinstance(child_flags, list):
                    flags.update(str(fl).strip() for fl in child_flags if fl)

        elif hierarchy_level >= 2 or ("roadmap" in issue_type or "initiative" in issue_type):
            # Roadmap Item / Initiative (level 2+): fetch ALL descendants at any depth
            # Step 1: get direct children (Epics)
            jql_enc = _quote(f"parent={ticket} ORDER BY created ASC")
            search = _jira_req("GET", f"/rest/api/3/search/jql?jql={jql_enc}&fields=issuetype,{JIRA_FF_FIELD}&maxResults=100")
            child_epics = []
            for issue in (search.get("issues") or []):
                child_ff = ((issue.get("fields") or {}).get(JIRA_FF_FIELD)) or []
                if isinstance(child_ff, list):
                    flags.update(str(fl).strip() for fl in child_ff if fl)
                ctype = (issue.get("fields") or {}).get("issuetype", {}).get("name", "").lower()
                if "epic" in ctype:
                    child_epics.append(issue.get("key", ""))

            # Step 2: for each child Epic, fetch its children (Stories/Tasks)
            for epic_key in child_epics:
                if not epic_key:
                    continue
                try:
                    jql2 = _quote(f"parent={epic_key} ORDER BY created ASC")
                    s2 = _jira_req("GET", f"/rest/api/3/search/jql?jql={jql2}&fields={JIRA_FF_FIELD}&maxResults=100")
                    for issue in (s2.get("issues") or []):
                        gchild_ff = ((issue.get("fields") or {}).get(JIRA_FF_FIELD)) or []
                        if isinstance(gchild_ff, list):
                            flags.update(str(fl).strip() for fl in gchild_ff if fl)
                except Exception:
                    pass

        else:
            # Story/Task/Bug (level 0): fetch each subtask's FF field
            subtasks = f.get("subtasks") or []
            for sub in subtasks:
                sub_key = sub.get("key", "")
                if not sub_key:
                    continue
                try:
                    sub_data = _jira_req("GET", f"/rest/api/3/issue/{sub_key}?fields={JIRA_FF_FIELD}")
                    sub_flags = ((sub_data.get("fields") or {}).get(JIRA_FF_FIELD)) or []
                    if isinstance(sub_flags, list):
                        flags.update(str(fl).strip() for fl in sub_flags if fl)
                except Exception:
                    pass

    except Exception as e:
        log.warning(f"[FeatureFlags] Failed to fetch flags from {ticket}: {e}")
    return flags

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE         = os.path.dirname(os.path.abspath(__file__))
HTML         = os.path.join(BASE, "roadmap.html")
TENANTS_DIR  = os.environ.get("FRAZIL_TENANTS_DIR", "/data/tenants")

def team_db_path(team: str) -> str:
    return os.path.join(TENANTS_DIR, team, "roadmap.db")

def valid_team(team: str) -> bool:
    """A team is valid if its directory exists under TENANTS_DIR."""
    if not team or not re.match(r"^[a-z0-9]+$", team):
        return False
    return os.path.isdir(os.path.join(TENANTS_DIR, team))

def resolve_team(x_team: Optional[str]) -> str:
    """Extract and validate the team slug from the X-Team header."""
    team = (x_team or "").strip().lower()
    team = re.sub(r"[^a-z0-9]", "", team)
    if not team or not valid_team(team):
        raise HTTPException(400, f"Unknown team: '{x_team}'. Check your team name.")
    return team

# ── Password hashing ──────────────────────────────────────────────────────────
try:
    import bcrypt as _bcrypt
    def hash_password(plain: str) -> str:
        return _bcrypt.hashpw(plain.encode(), _bcrypt.gensalt(12)).decode()
    def verify_password(plain: str, hashed: str) -> bool:
        try:
            return _bcrypt.checkpw(plain.encode(), hashed.encode())
        except Exception:
            return False
    print("[Auth] Using bcrypt for password hashing")
except ImportError:
    import secrets as _secrets
    def hash_password(plain: str) -> str:
        salt = _secrets.token_hex(16)
        h = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt.encode(), 260000).hex()
        return f"$pbkdf2${salt}${h}"
    def verify_password(plain: str, hashed: str) -> bool:
        try:
            if hashed.startswith("$pbkdf2$"):
                _, _, salt, stored = hashed.split("$")
                h = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt.encode(), 260000).hex()
                return hmac.compare_digest(h, stored)
            return False
        except Exception:
            return False
    print("[Auth] bcrypt not installed — using pbkdf2_hmac")

def is_hashed(pw: str) -> bool:
    return pw.startswith("$2b$") or pw.startswith("$2a$") or pw.startswith("$pbkdf2$")

# ── Token-based authentication ───────────────────────────────────────────────
# Simple HMAC-signed tokens: "team:username:role:expiry:signature"
def _load_token_secret() -> str:
    """Resolve the HMAC signing secret.

    Priority: TOKEN_SECRET env var (the production path) → a persisted file next
    to server.py → a freshly generated one written to that file.

    Why not just generate one in-process: with gunicorn -w 2 each worker would
    generate a *different* secret, so a token signed by worker A fails on worker
    B (random 401s), and every restart would invalidate all live tokens.
    Persisting to a shared file fixes both without requiring any configuration.
    """
    env = os.environ.get("TOKEN_SECRET")
    if env:
        return env
    path = os.path.join(BASE, ".token_secret")
    try:
        if os.path.exists(path):
            with open(path) as f:
                existing = f.read().strip()
            if existing:
                return existing
        generated = secrets.token_hex(32)
        with open(path, "w") as f:
            f.write(generated)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # best-effort (no-op on some platforms)
        log.warning("[Auth] TOKEN_SECRET not set — generated a persistent secret "
                    "at %s. Set TOKEN_SECRET explicitly in production.", path)
        return generated
    except OSError:
        log.warning("[Auth] TOKEN_SECRET not set and secret file unwritable — "
                    "falling back to an ephemeral secret (breaks across workers "
                    "and on restart). Set TOKEN_SECRET in the environment.")
        return secrets.token_hex(32)

_TOKEN_SECRET = _load_token_secret()
_TOKEN_EXPIRY = 86400  # 24 hours

def _sign(payload: str) -> str:
    return hmac.new(_TOKEN_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]

def create_token(team: str, username: str, role: str) -> str:
    expiry = int(time.time()) + _TOKEN_EXPIRY
    payload = f"{team}:{username}:{role}:{expiry}"
    sig = _sign(payload)
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()

def decode_token(token: str) -> dict:
    """Decode and verify a token. Returns {"team", "username", "role"} or raises HTTPException."""
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        parts = raw.rsplit(":", 1)
        if len(parts) != 2:
            raise ValueError("malformed")
        payload, sig = parts
        if not hmac.compare_digest(_sign(payload), sig):
            raise ValueError("bad signature")
        team, username, role, expiry_str = payload.split(":")
        if int(expiry_str) < int(time.time()):
            raise ValueError("expired")
        return {"team": team, "username": username, "role": role}
    except (ValueError, Exception) as e:
        raise HTTPException(401, f"Invalid or expired token: {e}")

def require_auth(authorization: Optional[str] = Header(None),
                 x_team: Optional[str] = Header(None)) -> dict:
    """FastAPI dependency: extract and verify auth token from Authorization header.
    Falls back to X-Team header with limited (viewer) access for backwards compatibility.
    Returns {"team", "username", "role"}."""
    token = None
    if authorization:
        # Strip "Bearer " prefix (case-insensitive) and any whitespace
        raw = authorization.strip()
        if raw.lower().startswith("bearer"):
            raw = raw[6:].strip()  # remove "bearer" (6 chars) and strip
        token = raw if raw else None

    if token:
        try:
            auth = decode_token(token)
            # If X-Team header is also sent, it must match the token's team
            if x_team:
                resolved = re.sub(r"[^a-z0-9]", "", (x_team or "").strip().lower())
                if resolved and resolved != auth["team"]:
                    raise HTTPException(403, "Token team does not match X-Team header")
            return auth
        except HTTPException:
            raise
        except Exception as e:
            log.warning(f"[Auth] Token decode failed: {e}")
            # Fall through to X-Team fallback

    # Fallback: if no valid token but X-Team is present, allow with viewer role
    # This maintains backwards compatibility during migration
    if x_team:
        team = re.sub(r"[^a-z0-9]", "", (x_team or "").strip().lower())
        if team and valid_team(team):
            log.info(f"[Auth] Fallback auth for team '{team}' — no token provided")
            return {"team": team, "username": "_legacy", "role": "viewer"}
    raise HTTPException(401, "Authorization required. Please log in again.")

def require_role(*allowed_roles):
    """Return a dependency that checks the user has one of the allowed roles."""
    def checker(auth: dict = Depends(require_auth)):
        if auth["role"] not in allowed_roles:
            raise HTTPException(403, f"Requires role: {', '.join(allowed_roles)}")
        return auth
    return checker

# ── Database (per-team) ───────────────────────────────────────────────────────
_initialized_teams: set = set()

@contextmanager
def db(team: str):
    path = team_db_path(team)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    # Wait up to 5s for a competing writer instead of erroring out immediately.
    # With gunicorn -w 2, two worker processes can attempt writes concurrently;
    # without this, the loser gets an instant "database is locked" -> HTTP 500.
    con.execute("PRAGMA busy_timeout=5000")
    # Safe + faster under WAL (fsync on checkpoint, not every commit).
    con.execute("PRAGMA synchronous=NORMAL")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def _get_init_password(team: str) -> str:
    """Get the initial admin password for a new team.
    If --new-team wrote a .init_password file, use it; otherwise use the default."""
    pw_file = os.path.join(TENANTS_DIR, team, ".init_password")
    if os.path.exists(pw_file):
        with open(pw_file) as f:
            pw = f.read().strip()
        os.remove(pw_file)
        return pw
    return "frazil123"  # default initial password — admin must change on first login

def init_team_db(team: str):
    if team in _initialized_teams:
        return
    with db(team) as c:
        c.executescript("""
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
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_type       TEXT NOT NULL,
            source              TEXT NOT NULL DEFAULT 'System',
            item_id             INTEGER,
            item_name           TEXT,
            owner               TEXT,
            project             TEXT,
            created_by          TEXT,
            created_ts          TEXT,
            read_by             TEXT,
            read_ts             TEXT,
            resolved_by         TEXT,
            resolved_ts         TEXT,
            action_taken        TEXT,
            previous_value      TEXT,
            new_value           TEXT,
            note                TEXT,
            status              TEXT NOT NULL DEFAULT 'Open',
            message             TEXT
        );
        CREATE TABLE IF NOT EXISTS comments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id     INTEGER NOT NULL,
            author      TEXT NOT NULL,
            body        TEXT NOT NULL,
            created_ts  TEXT NOT NULL,
            edited_ts   TEXT
        );
        CREATE TABLE IF NOT EXISTS capacity_overrides (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            owner           TEXT    NOT NULL,
            date            TEXT    NOT NULL,         -- YYYY-MM-DD
            capacity        REAL    NOT NULL,          -- 0 ≤ capacity ≤ ownerCapacity[owner]
            modified_by     TEXT    NOT NULL DEFAULT '',
            modified_ts     TEXT    NOT NULL DEFAULT '',
            note            TEXT    NOT NULL DEFAULT '',
            UNIQUE(owner, date)
        );
        CREATE TABLE IF NOT EXISTS planning_sessions (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            type            TEXT NOT NULL CHECK(type IN ('Review','Sprint','Release')),
            status          TEXT NOT NULL DEFAULT 'draft'
                                CHECK(status IN ('draft','committed','discarded')),
            created_by      TEXT NOT NULL,
            created_ts      TEXT NOT NULL,
            committed_ts    TEXT,
            release_number  TEXT,
            release_notes   TEXT,
            payload         TEXT NOT NULL DEFAULT '{}'
        );
        """)
        # ── Live migrations: add new columns if they don't exist yet ─────────────
        for _col, _defn in [
            ("locked_by", "TEXT"),
            ("locked_ts", "TEXT"),
            ("snapshot",  "TEXT NOT NULL DEFAULT '{}'"),
        ]:
            try:
                c.execute(f"ALTER TABLE planning_sessions ADD COLUMN {_col} {_defn}")
            except Exception:
                pass  # column already exists — ignore
        # Migrate capacity_overrides: add note column
        try:
            c.execute("ALTER TABLE capacity_overrides ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass  # column already exists
        defaults = {
            "developers":   [],
            "statuses":     ["In Progress","Planned","In Testing","Released","TBD","Backlogged"],
            "delayReasons": ["Testing delays","Partner Delays","Scope Creep",
                             "Priority Shift","Start Delayed","Board Testing",
                             "Resource Constraint","External Dependency",
                             "Requirements Change","Technical Blocker"],
            "changeReasons": ["Scope Change","Resource Constraint","Technical Blocker",
                              "External Dependency","Priority Shift",
                              "Revised Estimate","Partner Delays","Other"],
            "deferReasons":  ["Not Ready","Deprioritised","Waiting on External",
                              "Resource Unavailable","Other"],
            "products":     [{"name":"Fraznet","builtin":True},
                             {"name":"HubSpot","builtin":True}],
            "users":        [{"username":"admin","password":_get_init_password(team),
                              "builtin":True,"role":"admin","mustChangePassword":True}],
            "types":        [{"name":"Feature","color":""},{"name":"Enhancement","color":""},{"name":"Maintenance","color":""}],
        }
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO config(key,value) VALUES(?,?)",
                      (k, json.dumps(v)))
    _migrate_passwords(team)
    _migrate_config_keys(team)
    _initialized_teams.add(team)
    print(f"[DB] Team '{team}' ready: {team_db_path(team)}")

_migrated_teams: set = set()

def _migrate_config_keys(team: str):
    """Backfill any new config keys that didn't exist when the team was created.

    Idempotent and cheap to call, but guarded so it runs at most once per team
    per process: boot() migrates all teams and init_team_db() migrates on
    creation, so the per-request call in GET /api/all was re-reading the whole
    config table (and potentially writing) on every page load for no benefit.
    """
    if team in _migrated_teams:
        return
    new_keys = {
        "changeReasons": ["Scope Change","Resource Constraint","Technical Blocker",
                          "External Dependency","Priority Shift",
                          "Revised Estimate","Partner Delays","Other"],
        "deferReasons":  ["Not Ready","Deprioritised","Waiting on External",
                          "Resource Unavailable","Other"],
        "statusIsDefault":  {},
        "statusIsDeferred": {},
        "jiraSyncConfig":   {"enabled": False, "intervalMinutes": 30},
        "jiraEnabled":      True,
        "statusIsReleased": {},
        "statusIsApproved": {},
        "statusIsTesting":  {},
    }
    # Keys where False/0/empty-string is a valid intentional value — only seed if key is MISSING,
    # never overwrite an existing value even if it's falsy
    presence_only_keys = {"jiraEnabled", "jiraSyncConfig"}

    with db(team) as c:
        existing = {r[0]: json.loads(r[1]) for r in c.execute("SELECT key,value FROM config").fetchall()}
        for k, v in new_keys.items():
            if k in presence_only_keys:
                # Only insert if the key doesn't exist at all — never overwrite
                if k not in existing:
                    c.execute(
                        "INSERT INTO config(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO NOTHING",
                        (k, json.dumps(v))
                    )
                    print(f"[Migration] Seeded config key '{k}' for team '{team}'")
            else:
                # Insert if missing OR if row exists but value is empty/null
                if k not in existing or not existing[k]:
                    c.execute(
                        "INSERT INTO config(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (k, json.dumps(v))
                    )
                    print(f"[Migration] Seeded config key '{k}' for team '{team}'")
    _migrated_teams.add(team)

def _migrate_passwords(team: str):
    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
        if not row: return
        users = json.loads(row["value"])
        changed = False
        for u in users:
            pw = u.get("password", "")
            if pw and not is_hashed(pw):
                u["password"] = hash_password(pw)
                changed = True
        if changed:
            c.execute("UPDATE config SET value=? WHERE key='users'", (json.dumps(users),))

# ── Boot: ensure 'development' team exists, migrate legacy DB if needed ────────
def boot():
    os.makedirs(TENANTS_DIR, exist_ok=True)
    dev_dir = os.path.join(TENANTS_DIR, "development")
    os.makedirs(dev_dir, exist_ok=True)

    # Migrate legacy roadmap.db → development team if new DB doesn't exist yet
    legacy_db = os.path.join(BASE, "roadmap.db")
    new_db    = team_db_path("development")
    if os.path.exists(legacy_db) and not os.path.exists(new_db):
        import shutil
        shutil.copy2(legacy_db, new_db)
        print(f"[Migration] Copied roadmap.db → {new_db}")

    # Also accept existing 'technology' team if it exists (backward compat)
    tech_db = team_db_path("technology")
    if os.path.exists(tech_db):
        tech_dir = os.path.join(TENANTS_DIR, "technology")
        os.makedirs(tech_dir, exist_ok=True)
        init_team_db("technology")

    init_team_db("development")

    # Run migration for ALL existing team DBs (catches teams created before new keys were added)
    import os as _os
    for _entry in _os.listdir(TENANTS_DIR):
        _tpath = _os.path.join(TENANTS_DIR, _entry)
        if _os.path.isdir(_tpath) and _os.path.exists(_os.path.join(_tpath, "roadmap.db")):
            _migrate_config_keys(_entry)

boot()

# ── Rate limiting ─────────────────────────────────────────────────────────────
_rate: dict = {}
_rate_last_prune: float = 0.0
RATE_WINDOW, RATE_MAX = 60, 10

def _check_rate_limit(ip: str):
    global _rate_last_prune
    now = time.time()
    # Prune stale IPs every 5 minutes
    if now - _rate_last_prune > 300:
        stale = [k for k, v in _rate.items() if not v or now - v[-1] > RATE_WINDOW]
        for k in stale:
            del _rate[k]
        _rate_last_prune = now
    attempts = [t for t in _rate.get(ip, []) if now - t < RATE_WINDOW]
    attempts.append(now)
    _rate[ip] = attempts
    if len(attempts) > RATE_MAX:
        raise HTTPException(429, f"Too many login attempts. Try again in {RATE_WINDOW} seconds.")

# ── Audit logging ─────────────────────────────────────────────────────────────
def write_audit(team: str, action: str, username: str = "", project_id=None,
                project_name: str = "", changes: dict = None):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with db(team) as c:
        c.execute(
            "INSERT INTO audit_log(ts,username,action,project_id,project_name,changes)"
            " VALUES(?,?,?,?,?,?)",
            (ts, username or "unknown", action,
             project_id, project_name, json.dumps(changes) if changes else None)
        )

# ── App ───────────────────────────────────────────────────────────────────────
APP_VERSION = "3.2.0"

app = FastAPI(title="Frazil Roadmap", version=APP_VERSION)

# ── Uncaught-exception handler ───────────────────────────────────────────────
import traceback as _traceback
from fastapi.responses import JSONResponse
from fastapi import Request as _Request

# The full traceback is always logged server-side. It is only echoed to the
# client when DEBUG_TRACEBACKS is explicitly enabled — never leak internals
# (file paths, library versions, data) to callers in production.
_DEBUG_TRACEBACKS = os.environ.get("DEBUG_TRACEBACKS", "").strip().lower() in ("1", "true", "yes", "on")

@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: _Request, exc: Exception):
    tb = _traceback.format_exc()
    log.error(f"[500] {request.method} {request.url.path}\n{tb}")
    if _DEBUG_TRACEBACKS:
        return JSONResponse(status_code=500, content={"detail": str(exc), "traceback": tb})
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

@app.get("/api/version")
def get_version():
    return {"server": APP_VERSION, "name": "Frazil Roadmap"}
_allowed_origins = os.environ.get("CORS_ORIGINS", "").split(",")
_allowed_origins = [o.strip() for o in _allowed_origins if o.strip()]
app.add_middleware(CORSMiddleware,
                   allow_origins=_allowed_origins or ["*"],
                   allow_methods=["*"], allow_headers=["*"],
                   allow_credentials=True)

@app.get("/", response_class=HTMLResponse)
def root():
    if not os.path.exists(HTML):
        raise HTTPException(404, "roadmap.html not found next to server.py")
    with open(HTML, encoding="utf-8") as f:
        return f.read()

# ── PWA: manifest, service worker, icons ──────────────────────────────────────
# Served as routes (not static files) so the deploy stays a two-file scp. Icon
# bytes are base64-embedded below; regenerate with `python tools/gen_pwa_icons.py`
# (dev-only — Pillow is NOT a runtime dependency, the bytes are baked in here).
_PWA_ICON_192_B64   = "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"  # GENERATED by tools/gen_pwa_icons.py — do not hand-edit
_PWA_ICON_512_B64   = "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"  # GENERATED by tools/gen_pwa_icons.py — do not hand-edit
_PWA_ICON_APPLE_B64 = "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"  # GENERATED by tools/gen_pwa_icons.py — do not hand-edit

_PWA_MANIFEST = {
    "name": "Frazil Roadmap",
    "short_name": "Roadmap",
    "description": "Team roadmap, Gantt, Kanban & planning",
    "id": "/",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "any",
    "background_color": "#ffffff",
    "theme_color": "#5b4fff",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

# Network-first for the app shell (so a deploy goes live immediately — Caddy
# serves roadmap.html fresh), cache-first for our own static icons/manifest, and
# network-only for /api/* (data stays server-authoritative; no offline writes).
# Cache name is tied to APP_VERSION so a release busts the old shell on activate.
_PWA_SW_TEMPLATE = r"""
const CACHE = 'frazil-shell-__APP_VERSION__';
const SHELL = ['/', '/manifest.webmanifest', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL).catch(() => {})));
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;                 // never intercept writes
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;       // fonts, Jira, etc. pass through
  if (url.pathname.startsWith('/api/')) return;     // data is network-only

  // Navigations / app shell: network-first, fall back to cached shell offline.
  if (req.mode === 'navigate' || url.pathname === '/') {
    e.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put('/', copy)).catch(() => {});
          return res;
        })
        .catch(() => caches.match('/'))
    );
    return;
  }

  // Our own static assets: cache-first, then populate.
  if (SHELL.includes(url.pathname) || url.pathname === '/apple-touch-icon.png') {
    e.respondWith(
      caches.match(req).then((cached) =>
        cached ||
        fetch(req).then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
          return res;
        })
      )
    );
  }
});
"""

@app.get("/manifest.webmanifest")
def pwa_manifest():
    return Response(content=json.dumps(_PWA_MANIFEST),
                    media_type="application/manifest+json")

@app.get("/sw.js")
def pwa_service_worker():
    js = _PWA_SW_TEMPLATE.replace("__APP_VERSION__", APP_VERSION)
    return Response(content=js, media_type="application/javascript",
                    headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

def _pwa_png(b64: str) -> Response:
    if not b64:
        raise HTTPException(503, "PWA icons not generated — run tools/gen_pwa_icons.py")
    return Response(content=base64.b64decode(b64), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=604800"})

@app.get("/icon-192.png")
def pwa_icon_192():
    return _pwa_png(_PWA_ICON_192_B64)

@app.get("/icon-512.png")
def pwa_icon_512():
    return _pwa_png(_PWA_ICON_512_B64)

@app.get("/apple-touch-icon.png")
def pwa_apple_icon():
    return _pwa_png(_PWA_ICON_APPLE_B64)

# ── List teams (for the login dropdown) ──────────────────────────────────────
@app.get("/api/teams")
def list_teams():
    try:
        teams = sorted([
            d for d in os.listdir(TENANTS_DIR)
            if os.path.isdir(os.path.join(TENANTS_DIR, d))
            and re.match(r"^[a-z0-9]+$", d)
        ])
    except FileNotFoundError:
        teams = []
    return {"teams": teams}

# ── Login ─────────────────────────────────────────────────────────────────────
@app.post("/api/login")
def login(body: dict = Body(...), request: FRequest = None, response: Response = None):
    ip = (request.client.host if request else "unknown")
    _check_rate_limit(ip)

    raw_team = body.get("team", "").strip().lower()
    team = re.sub(r"[^a-z0-9]", "", raw_team)
    if not team or not valid_team(team):
        raise HTTPException(400, f"Unknown team: '{raw_team}'")

    # Ensure the team DB is initialized
    init_team_db(team)

    username = body.get("username", "").strip()
    password = body.get("password", "")
    if not username or not password:
        raise HTTPException(400, "Username and password required")

    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
    users = json.loads(row["value"]) if row else []

    user = next((u for u in users if u["username"] == username), None)
    if not user:
        time.sleep(0.3)
        raise HTTPException(401, "Invalid username or password")

    pw = user.get("password", "")
    if not is_hashed(pw):
        # Force-migrate unhashed password on login attempt
        if hmac.compare_digest(pw, password):
            user["password"] = hash_password(password)
            with db(team) as c:
                c.execute("UPDATE config SET value=? WHERE key='users'", (json.dumps(users),))
            log.warning(f"[Auth] Auto-hashed plaintext password for user '{username}' in team '{team}'")
            ok = True
        else:
            ok = False
    else:
        ok = verify_password(password, pw)
    if not ok:
        time.sleep(0.3)
        raise HTTPException(401, "Invalid username or password")

    role = user.get("role", "viewer")
    token = create_token(team, username, role)
    write_audit(team, "login", username)
    # Set httpOnly session cookie so the audit page can verify auth server-side
    if response:
        response.set_cookie(
            key="frazil_session",
            value=token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=60 * 60 * 24 * 7  # 7 days
        )
    return {"username": user["username"], "builtin": user.get("builtin", False),
            "role": role,
            "ownerFilter": user.get("ownerFilter", ""),
            "team": team,
            "token": token,
            "mustChangePassword": user.get("mustChangePassword", False)}

@app.post("/api/users/self/password")
def change_own_password(body: dict = Body(...), auth: dict = Depends(require_auth)):
    """Allow any logged-in user to change their own password. Works for all roles."""
    team     = auth["team"]
    username = auth["username"]
    new_pw   = body.get("password", "")
    if not new_pw or len(new_pw) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
    if not row:
        raise HTTPException(404, "User store not found")
    users = json.loads(row["value"])
    user = next((u for u in users if u["username"] == username), None)
    if not user:
        raise HTTPException(404, "User not found")
    user["password"] = hash_password(new_pw)
    with db(team) as c:
        c.execute("UPDATE config SET value=? WHERE key='users'", (json.dumps(users),))
    write_audit(team, "password:change", username)
    return {"ok": True}

@app.post("/api/users/{target_username}/password")
def admin_change_user_password(
    target_username: str,
    body: dict = Body(...),
    auth: dict = Depends(require_role("admin"))
):
    """Allow the primary (builtin) admin to change any user's password.
    Regular admins can only change non-admin users' passwords."""
    team     = auth["team"]
    caller   = auth["username"]
    new_pw   = body.get("password", "")
    if not new_pw or len(new_pw) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
    if not row:
        raise HTTPException(404, "User store not found")
    users = json.loads(row["value"])
    # Find the caller — check if they are the builtin primary admin
    caller_user  = next((u for u in users if u["username"] == caller), None)
    is_primary   = caller_user and caller_user.get("builtin", False)
    target_user  = next((u for u in users if u["username"] == target_username), None)
    if not target_user:
        raise HTTPException(404, f"User '{target_username}' not found")
    target_is_admin = target_user.get("role", "viewer") == "admin"
    # Non-primary admins cannot change other admins' passwords
    if target_is_admin and not is_primary and caller != target_username:
        raise HTTPException(403, "Only the primary admin can change another admin's password")
    target_user["password"] = hash_password(new_pw)
    with db(team) as c:
        c.execute("UPDATE config SET value=? WHERE key='users'", (json.dumps(users),))
    write_audit(team, "password:admin_change", caller,
                changes={"target": target_username, "by_primary": is_primary})
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    """Clear the session cookie set on login."""
    response.delete_cookie(key="frazil_session", httponly=True, secure=True, samesite="lax")
    return {"ok": True}

@app.post("/api/verify-password")
def verify_pw_endpoint(body: dict = Body(...), request: FRequest = None,
                       auth: dict = Depends(require_auth)):
    ip = (request.client.host if request else "unknown")
    _check_rate_limit(ip)
    team = auth["team"]
    username = body.get("username","")
    password = body.get("password","")
    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
    users = json.loads(row["value"]) if row else []
    user = next((u for u in users if u["username"] == username), None)
    if not user: raise HTTPException(401, "Invalid")
    pw = user.get("password","")
    ok = verify_password(password, pw) if is_hashed(pw) else False
    if not ok: raise HTTPException(401, "Invalid current password")
    return {"ok": True}

@app.post("/api/hash-password")
def hash_pw_endpoint(body: dict = Body(...)):
    plain = body.get("password", "")
    if not plain or len(plain) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    return {"hashed": hash_password(plain)}

@app.post("/api/force-change-password")
def force_change_password(body: dict = Body(...), auth: dict = Depends(require_auth)):
    """Change password and clear mustChangePassword flag. Used on first login."""
    team = auth["team"]
    username = auth["username"]
    new_password = body.get("password", "")
    if not new_password or len(new_password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
    users = json.loads(row["value"]) if row else []
    user = next((u for u in users if u["username"] == username), None)
    if not user:
        raise HTTPException(404, "User not found")
    user["password"] = hash_password(new_password)
    user["mustChangePassword"] = False
    with db(team) as c:
        c.execute("UPDATE config SET value=? WHERE key='users'", (json.dumps(users),))
    write_audit(team, "password:change", username, changes={"forced": True})
    # Issue a fresh token
    role = user.get("role", "viewer")
    token = create_token(team, username, role)
    return {"ok": True, "token": token}

# ── Data ──────────────────────────────────────────────────────────────────────
@app.get("/api/all")
def get_all(auth: dict = Depends(require_auth)):
    team = auth["team"]
    init_team_db(team)
    _migrate_config_keys(team)  # backfill any new keys added since team was created
    with db(team) as c:
        rows = c.execute("SELECT id, data FROM projects ORDER BY id").fetchall()
        # Read all config in a single connection
        config_rows = c.execute("SELECT key, value FROM config").fetchall()
    projects = []
    for r in rows:
        p = json.loads(r["data"]); p["id"] = r["id"]; projects.append(p)
    cfg_map = {r["key"]: json.loads(r["value"]) for r in config_rows}
    def cfg(k):
        return cfg_map.get(k, [])
    users_raw  = cfg("users")
    users_safe = [{"username": u["username"], "builtin": u.get("builtin", False),
                   "role": u.get("role", "viewer"),
                   "ownerFilter": u.get("ownerFilter", ""),
                   "revokedAt": u.get("revokedAt")} for u in users_raw]
    return {"projects": projects, "developers": cfg("developers"),
            "statuses": cfg("statuses"), "delayReasons": cfg("delayReasons"),
            "products": cfg("products"), "users": users_safe,
            "types": cfg("types"),
            "ownerCapacity": cfg("ownerCapacity") or {},
            "statusIgnoreConflicts": cfg("statusIgnoreConflicts") or {},
            "typeIgnoreConflicts": cfg("typeIgnoreConflicts") or {},
            "statusIsActive": cfg("statusIsActive") or {},
            "statusIsTerminal": cfg("statusIsTerminal") or {},
            "statusIsDefault": cfg("statusIsDefault") or {},
            "statusIsDeferred": cfg("statusIsDeferred") or {},
            "statusIsReleased": cfg("statusIsReleased") or {},
            "statusIsApproved": cfg("statusIsApproved") or {},
            "statusIsTesting": cfg("statusIsTesting") or {},
            "changeReasons": cfg("changeReasons") or [],
            "deferReasons": cfg("deferReasons") or [],
            "jiraProjectMapping": cfg("jiraProjectMapping") or {},
            "jiraStatusMapping": cfg("jiraStatusMapping") or {},
            "jiraTypeMapping": cfg("jiraTypeMapping") or {},
            "jiraSyncConfig": cfg("jiraSyncConfig") or {}}


# ── Force-seed config keys (idempotent, for migration/repair) ─────────────────
DEFAULT_CHANGE_REASONS = [
    "Scope Change", "Resource Constraint", "Technical Blocker",
    "External Dependency", "Priority Shift",
    "Revised Estimate", "Partner Delays", "Other"
]
DEFAULT_DEFER_REASONS = [
    "Not Ready", "Deprioritised", "Waiting on External",
    "Resource Unavailable", "Other"
]

@app.post("/api/admin/seed-reasons")
def seed_reasons(auth: dict = Depends(require_role("admin"))):
    """Force-write default changeReasons and deferReasons for this team. Safe to call multiple times."""
    team = auth["team"]
    with db(team) as c:
        for key, val in [("changeReasons", DEFAULT_CHANGE_REASONS),
                         ("deferReasons",  DEFAULT_DEFER_REASONS)]:
            c.execute(
                "INSERT INTO config(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(val))
            )
    return {"ok": True, "changeReasons": DEFAULT_CHANGE_REASONS, "deferReasons": DEFAULT_DEFER_REASONS}

@app.post("/api/admin/reset-password")
def reset_admin_password(body: dict = Body(...)):
    """Reset admin password to frazil123 using a server-side secret key.
    Used when a new team was provisioned with a random password.
    Requires the RESET_SECRET env var to be set on the server."""
    secret = os.environ.get("RESET_SECRET", "")
    if not secret or body.get("secret") != secret:
        raise HTTPException(403, "Invalid reset secret")
    team = re.sub(r"[^a-z0-9]", "", (body.get("team") or "").lower())
    if not team or not valid_team(team):
        raise HTTPException(404, f"Team not found: {team}")
    new_pw = body.get("password", "frazil123")
    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
    if not row:
        raise HTTPException(404, "No users found for team")
    users = json.loads(row["value"])
    admin = next((u for u in users if u.get("role") == "admin"), None)
    if not admin:
        raise HTTPException(404, "No admin user found for team")
    admin["password"] = hash_password(new_pw)
    admin["mustChangePassword"] = True
    with db(team) as c:
        c.execute("UPDATE config SET value=? WHERE key='users'", (json.dumps(users),))
    write_audit(team, "password:reset", "system", changes={"admin": admin["username"]})
    return {"ok": True, "team": team, "username": admin["username"], "note": "Password reset to frazil123, mustChangePassword=True"}

# ── Projects ──────────────────────────────────────────────────────────────────
@app.post("/api/projects")
def create_project(body: dict, auth: dict = Depends(require_role("admin", "editor"))):
    team = auth["team"]
    username = body.pop("_username", auth["username"])
    body.pop("id", None)
    # Server-side rounding of parallelResources — must happen BEFORE the insert
    # so the persisted value matches what we return (not just the response).
    if "parallelResources" in body:
        body["parallelResources"] = round_up_to_quarter(body["parallelResources"])
    with db(team) as c:
        cur = c.execute("INSERT INTO projects(data) VALUES(?)", (json.dumps(body),))
        body["id"] = cur.lastrowid
    write_audit(team, "create", username, body["id"], body.get("name",""))
    return body

@app.put("/api/projects/{pid}")
def update_project(pid: int, body: dict, auth: dict = Depends(require_role("admin", "editor"))):
    team = auth["team"]
    username = body.pop("_username", auth["username"])
    body.pop("id", None)
    # Server-side rounding of parallelResources before save
    if "parallelResources" in body:
        body["parallelResources"] = round_up_to_quarter(body["parallelResources"])

    # Validate: test period must be strictly less than time estimate
    _test_wks = float(body.get("testWeeks") or 0)
    _due_wks  = float(body.get("dueWeeks")  or 0)
    if _test_wks > 0 and _due_wks > 0 and _test_wks >= _due_wks:
        raise HTTPException(422, f"Test period ({_test_wks}w) cannot equal or exceed the time estimate ({_due_wks}w)")

    # Read existing item, validate parallelResources, then write — single DB block
    with db(team) as c:
        row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
        if not row: raise HTTPException(404, "Not found")
        old = json.loads(row["data"])

        # Validate: parallelResources cannot be changed while item is in an active status
        active_row = c.execute("SELECT value FROM config WHERE key='statusIsActive'").fetchone()
        active_map = json.loads(active_row["value"]) if active_row else {}
        current_status = old.get("status", "")
        if active_map.get(current_status):
            old_pr = float(old.get("parallelResources") or 1)
            new_pr = float(body.get("parallelResources") or 1)
            if abs(old_pr - new_pr) > 0.001:
                raise HTTPException(422, f"Parallel Resources cannot be changed while item is active (status: {current_status!r})")

        changes = {k: {"from": old.get(k), "to": v}
                   for k, v in body.items()
                   if old.get(k) != v and k not in {"jiraTickets","description"}}
        c.execute("UPDATE projects SET data=? WHERE id=?", (json.dumps(body), pid))
    write_audit(team, "update", username, pid, body.get("name",""), changes or None)
    body["id"] = pid

    # ── FF pull: when item moves to Released status and has Jira tickets ──────
    # Run in background (best-effort, non-blocking) so PUT response is fast.
    new_status = body.get("status","")
    old_status = old.get("status","")
    tickets    = body.get("jiraTickets") or []
    if new_status != old_status and tickets and jira_configured():
        # Check if the new status is the configured Released status
        try:
            with db(team) as c:
                rel_row = c.execute("SELECT value FROM config WHERE key='statusIsReleased'").fetchone()
            rel_map = json.loads(rel_row["value"]) if rel_row else {}
            if rel_map.get(new_status):
                # Pull FF flags across the full Jira hierarchy for all linked tickets
                all_ff: set = set()
                for ticket in tickets[:10]:
                    all_ff.update(_fetch_jira_feature_flags(ticket))
                if all_ff:
                    body["jiraFeatureFlags"] = sorted(all_ff)
                    # Merge into featureFlags (union of manual + Jira, deduped)
                    existing_manual = set(body.get("featureFlags") or [])
                    body["featureFlags"] = sorted(existing_manual | all_ff)
                    with db(team) as c:
                        c.execute("UPDATE projects SET data=? WHERE id=?",
                                  (json.dumps(body), pid))
                    log.info(f"[FeatureFlags] Pulled {len(all_ff)} flags for item {pid} on release")
        except Exception as e:
            log.warning(f"[FeatureFlags] Release-trigger FF pull failed for item {pid}: {e}")

    return body

@app.delete("/api/projects/{pid}")
def delete_project(pid: int, username: str = "",
                   auth: dict = Depends(require_role("admin"))):
    team = auth["team"]
    username = username or auth["username"]
    with db(team) as c:
        row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
        name = json.loads(row["data"]).get("name","") if row else ""
        # Item 10: Cascade delete orphaned comments and activities
        c.execute("DELETE FROM comments WHERE item_id=?", (pid,))
        c.execute("DELETE FROM activities WHERE item_id=?", (pid,))
        c.execute("DELETE FROM projects WHERE id=?", (pid,))
    write_audit(team, "delete", username, pid, name)
    return {"ok": True}

# ── Config ────────────────────────────────────────────────────────────────────
VALID_KEYS = {"developers","statuses","delayReasons","products","users","types",
              "ownerCapacity","statusIgnoreConflicts","typeIgnoreConflicts",
              "statusIsActive","statusIsTerminal",
              "statusIsDefault","statusIsDeferred",
              "changeReasons","deferReasons",
              "jiraProjectMapping","jiraStatusMapping","jiraTypeMapping",
              "jiraSyncConfig","jiraEnabled","statusIsReleased","statusIsApproved","statusIsTesting"}

@app.put("/api/config/{key}")
def set_config(key: str, body = Body(...), username: str = "",
               auth: dict = Depends(require_role("admin"))):
    team = auth["team"]
    username = username or auth["username"]
    if key not in VALID_KEYS:
        raise HTTPException(400, f"Unknown key: {key}")
    if key == "users":
        with db(team) as c:
            row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
        existing = {u["username"]: u for u in json.loads(row["value"])} if row else {}
        for u in body:
            uname = u.get("username","")
            pw = u.get("password","")
            if not pw:
                if uname in existing and existing[uname].get("password"):
                    u["password"] = existing[uname]["password"]
            elif not is_hashed(pw):
                u["password"] = hash_password(pw)
    with db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(body)))
    write_audit(team, f"config:{key}", username, changes={"updated": key})
    return body

# ── Import ────────────────────────────────────────────────────────────────────
@app.post("/api/import")
def bulk_import(body: dict = Body(...), auth: dict = Depends(require_role("admin"))):
    team = auth["team"]
    username = body.pop("_username", auth["username"])
    with db(team) as c:
        c.execute("DELETE FROM projects")
        for p in body.get("projects", []):
            p.pop("id", None)
            c.execute("INSERT INTO projects(data) VALUES(?)", (json.dumps(p),))
    for key in VALID_KEYS:
        if key in body and body[key]:
            val = body[key]
            if key == "users":
                for u in val:
                    pw = u.get("password","")
                    if pw and not is_hashed(pw):
                        u["password"] = hash_password(pw)
            with db(team) as c:
                c.execute("INSERT INTO config(key,value) VALUES(?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, json.dumps(val)))
    write_audit(team, "import", username, changes={"imported": len(body.get("projects",[]))})
    return {"ok": True, "imported": len(body.get("projects", []))}

# ── Audit log viewer ──────────────────────────────────────────────────────────
def _audit_forbidden_page(team: str) -> str:
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<title>Access Denied — Audit Log</title>
<link href="https://fonts.googleapis.com/css2?family=Lato:wght@400;700;900&display=swap" rel="stylesheet">
<style>*{{box-sizing:border-box;margin:0;padding:0}}body{{font-family:'Lato',sans-serif;background:#f5f5f7;color:#1a1a2e;display:flex;align-items:center;justify-content:center;min-height:100vh}}.card{{background:#fff;border-radius:12px;padding:40px;text-align:center;max-width:400px;box-shadow:0 4px 20px rgba(0,0,0,.1)}}h1{{font-size:20px;font-weight:900;margin-bottom:12px;color:#e8394a}}p{{font-size:13px;color:#7070a0;margin-bottom:20px}}a{{background:#5b4fff;color:#fff;padding:9px 20px;border-radius:7px;text-decoration:none;font-weight:700;font-size:13px}}</style>
</head><body>
<div class="card">
  <h1>⛔ Access Denied</h1>
  <p>The audit log requires admin access. Please log in with an admin account for the <strong>{html.escape(team)}</strong> team.</p>
  <a href="/?team={html.escape(team)}">← Back to Roadmap</a>
</div>
</body></html>"""

@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: FRequest, team: str = "development", search: str = "",  user: str = "",
               action_type: str = "", date_from: str = "", date_to: str = ""):
    team = re.sub(r"[^a-z0-9]", "", team.lower())
    if not valid_team(team):
        raise HTTPException(404, "Team not found")

    # ── Auth check: require valid session cookie or redirect to login ──────────
    session_token = request.cookies.get("frazil_session")
    if not session_token:
        return RedirectResponse(url=f"/?team={team}&next=audit", status_code=302)
    try:
        auth = decode_token(session_token)
        # Token must belong to the requested team, and user must be admin
        if auth["team"] != team:
            return RedirectResponse(url=f"/?team={team}&next=audit", status_code=302)
        if auth["role"] not in ("admin",):
            return HTMLResponse(content=_audit_forbidden_page(team), status_code=403)
    except Exception:
        return RedirectResponse(url=f"/?team={team}&next=audit", status_code=302)
    # ── End auth check ─────────────────────────────────────────────────────────

    # Default to last 7 days when no date filter supplied
    from datetime import timedelta
    _today = datetime.now(timezone.utc).date()
    if not date_from and not date_to:
        date_from = (_today - timedelta(days=7)).isoformat()

    # Build query with filters
    conditions, params = [], []
    if search:
        conditions.append("(project_name LIKE ? OR username LIKE ? OR changes LIKE ?)")
        params += [f"%{search}%", f"%{search}%", f"%{search}%"]
    if user:
        conditions.append("username = ?"); params.append(user)
    if action_type:
        conditions.append("action LIKE ?"); params.append(f"{action_type}%")
    if date_from:
        conditions.append("ts >= ?"); params.append(date_from)
    if date_to:
        conditions.append("ts <= ?"); params.append(date_to + " 23:59:59")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    with db(team) as c:
        rows = c.execute(
            f"SELECT ts, username, action, project_id, project_name, changes "
            f"FROM audit_log {where} ORDER BY id DESC LIMIT 1000",
            params
        ).fetchall()
        # Get distinct users and action prefixes for filter dropdowns
        all_users = [r[0] for r in c.execute("SELECT DISTINCT username FROM audit_log ORDER BY username").fetchall()]
        all_actions = sorted(set(r[0].split(":")[0] for r in c.execute("SELECT DISTINCT action FROM audit_log").fetchall()))

    ACTION_COLOR = {"create":"#22b96e","update":"#5b4fff","delete":"#e8394a",
                    "import":"#f0a500","login":"#0090d4","config":"#e8a000"}

    def row_html(r):
        action = html.escape(r["action"])
        color  = ACTION_COLOR.get(r["action"].split(":")[0], "#888")
        proj   = f'<span style="color:#333">#{r["project_id"]} {html.escape(r["project_name"] or "")}</span>' if r["project_id"] else ""
        changes_html = ""
        if r["changes"]:
            try:
                ch = json.loads(r["changes"])
                lines = []
                for k, v in ch.items():
                    ek = html.escape(str(k))
                    if isinstance(v, dict) and "from" in v and "to" in v:
                        frm = html.escape(str(v["from"])[:80]) if v["from"] is not None else "—"
                        to  = html.escape(str(v["to"])[:80])   if v["to"]   is not None else "—"
                        if frm != to:
                            lines.append(f'<span style="color:#888">{ek}:</span> <span style="color:#c00">{frm}</span> → <span style="color:#060">{to}</span>')
                    else:
                        lines.append(f'<span style="color:#888">{ek}:</span> {html.escape(str(v)[:100])}')
                changes_html = "<br>".join(lines)
            except Exception:
                changes_html = html.escape(str(r["changes"])[:300])
        return f'''<tr>
          <td style="white-space:nowrap;color:#666;font-size:11px">{html.escape(r["ts"])}</td>
          <td><strong>{html.escape(r["username"])}</strong></td>
          <td><span style="background:{color}22;color:{color};padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700">{action}</span></td>
          <td style="font-size:12px">{proj}</td>
          <td style="font-size:11px;color:#444;max-width:360px">{changes_html}</td>
        </tr>'''

    rows_html = "".join(row_html(r) for r in rows) or \
        '<tr><td colspan="5" style="text-align:center;padding:40px;color:#999">No matching audit entries.</td></tr>'

    user_opts  = "".join(f'<option value="{u}"{" selected" if u==user else ""}>{u}</option>' for u in all_users)
    action_opts = "".join(f'<option value="{a}"{" selected" if a==action_type else ""}>{a}</option>' for a in all_actions)

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<title>Frazil Roadmap — Audit Log ({team})</title>
<link href="https://fonts.googleapis.com/css2?family=Lato:wght@400;700;900&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Lato',sans-serif;background:#f5f5f7;color:#1a1a2e;font-size:13px}}
.header{{background:#fff;border-bottom:1px solid #d8d8e0;padding:16px 28px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:10}}
.header h1{{font-size:17px;font-weight:900}}
.sub{{font-size:12px;color:#7070a0}}
.filters{{background:#fff;border-bottom:1px solid #d8d8e0;padding:12px 28px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
.filters label{{font-size:11px;font-weight:700;color:#7070a0;text-transform:uppercase;letter-spacing:.5px;margin-right:2px}}
.filters input,.filters select{{padding:6px 10px;font-size:12px;border:1px solid #d8d8e0;border-radius:6px;font-family:'Lato',sans-serif;outline:none;background:#fff;color:#1a1a2e}}
.filters input:focus,.filters select:focus{{border-color:#5b4fff}}
.search-wrap{{display:flex;align-items:center;gap:8px;flex:1;max-width:320px}}
.search-wrap input{{flex:1}}
.wrap{{padding:24px 28px}}
.results-bar{{font-size:12px;color:#7070a0;margin-bottom:12px}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.07)}}
th{{background:#f5f5f7;padding:10px 14px;text-align:left;font-size:10px;font-weight:700;color:#7070a0;text-transform:uppercase;letter-spacing:.6px;border-bottom:2px solid #d8d8e0}}
td{{padding:10px 14px;border-bottom:1px solid #ebebf0;vertical-align:top}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#fafafe}}
.btn{{border:none;padding:7px 16px;border-radius:6px;font-family:'Lato',sans-serif;font-weight:700;font-size:12px;cursor:pointer}}
.btn-primary{{background:#5b4fff;color:#fff}}
.btn-ghost{{background:transparent;border:1px solid #d8d8e0;color:#7070a0}}
.badge-count{{background:#5b4fff;color:#fff;font-size:10px;font-weight:900;padding:1px 7px;border-radius:8px;margin-left:6px}}
</style>
</head><body>
<div class="header">
  <div>
    <h1>⚙ Audit Log — {team}</h1>
    <div class="sub">{len(rows)} entries shown</div>
    <div style="font-size:11px;color:#888;margin-top:4px">App v{APP_VERSION} · Server v{APP_VERSION}</div>
  </div>
  <button class="btn btn-ghost" onclick="location.reload()" style="margin-left:auto">Refresh</button>
</div>

<form method="get" action="/audit">
  <input type="hidden" name="team" value="{html.escape(team)}">
  <div class="filters">
    <div class="search-wrap">
      <label>Search</label>
      <input type="text" name="search" value="{html.escape(search)}" placeholder="Item name, user, changes…" id="searchInput">
    </div>
    <div style="display:flex;align-items:center;gap:6px">
      <label>User</label>
      <select name="user">
        <option value="">All Users</option>
        {user_opts}
      </select>
    </div>
    <div style="display:flex;align-items:center;gap:6px">
      <label>Action</label>
      <select name="action_type">
        <option value="">All Actions</option>
        {action_opts}
      </select>
    </div>
    <div style="display:flex;align-items:center;gap:6px">
      <label>From</label>
      <input type="date" name="date_from" value="{date_from}">
    </div>
    <div style="display:flex;align-items:center;gap:6px">
      <label>To</label>
      <input type="date" name="date_to" value="{date_to}">
    </div>
    <button type="submit" class="btn btn-primary">Filter</button>
    <a href="/audit?team={team}" class="btn btn-ghost" style="text-decoration:none">All Time</a>
  </div>
  <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
    <span style="font-size:11px;font-weight:700;color:#888;align-self:center">Quick:</span>
    <a href="/audit?team={team}&date_from={_today.isoformat()}" class="btn btn-ghost" style="text-decoration:none;font-size:11px;padding:3px 10px">Today</a>
    <a href="/audit?team={team}&date_from={(_today - timedelta(days=7)).isoformat()}" class="btn btn-ghost" style="text-decoration:none;font-size:11px;padding:3px 10px">Last 7 days</a>
    <a href="/audit?team={team}&date_from={(_today - timedelta(days=30)).isoformat()}" class="btn btn-ghost" style="text-decoration:none;font-size:11px;padding:3px 10px">Last 30 days</a>
    <a href="/audit?team={team}&date_from={_today.replace(day=1).isoformat()}" class="btn btn-ghost" style="text-decoration:none;font-size:11px;padding:3px 10px">This Month</a>
  </div>
</form>

<div class="wrap">
  <div class="results-bar">{len(rows)} result{"s" if len(rows)!=1 else ""}{f" matching <em>{html.escape(search)}</em>" if search else ""} — newest first</div>
  <table>
    <thead><tr>
      <th>Timestamp (UTC)</th><th>User</th><th>Action</th><th>Item</th><th>Changes</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>
<script>
  // Focus search on load
  document.getElementById('searchInput')?.focus();
  // Keyboard shortcut: Enter submits
  document.getElementById('searchInput')?.addEventListener('keydown', e=>{{
    if(e.key==='Enter') e.target.closest('form').submit();
  }});
</script>
</body></html>"""

# ── Jira ──────────────────────────────────────────────────────────────────────
@app.get("/api/jira/status")
def jira_status(auth: dict = Depends(require_auth)):
    team = auth["team"]
    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='jiraEnabled'").fetchone()
    enabled = json.loads(row["value"]) if row else True  # default enabled
    return {"configured": jira_configured(), "baseUrl": JIRA_BASE, "enabled": enabled}

@app.post("/api/jira/tickets")
def get_jira_tickets(body: dict = Body(...), auth: dict = Depends(require_auth)):
    if not jira_configured():
        raise HTTPException(503, "Jira not configured")
    tickets = body.get("tickets", [])
    if not tickets: return {}
    jql = "issueKey in (" + ",".join(tickets[:50]) + ")"
    req = Request(f"{JIRA_BASE}/rest/api/3/search/jql",
        data=json.dumps({"jql":jql,"fields":["summary","status"],"maxResults":50}).encode(),
        headers={"Authorization":_jira_auth_header(),"Accept":"application/json","Content-Type":"application/json"},
        method="POST")
    try:
        with urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
    except HTTPError as e:
        raise HTTPException(e.code, e.read().decode(errors="replace")[:300])
    except URLError as e:
        raise HTTPException(503, str(e.reason))
    result = {}
    for issue in data.get("issues", data.get("values", [])):
        key = issue.get("key") or issue.get("id")
        if not key: continue
        fields = issue.get("fields", {})
        status = fields.get("status", {})
        result[key] = {"summary": fields.get("summary",""),
                       "status": status.get("name",""),
                       "statusCategory": status.get("statusCategory",{}).get("key","")}
    for t in tickets:
        if t not in result:
            result[t] = {"summary":"","status":"Not found","statusCategory":"unknown"}
    return result

# ── Activities ────────────────────────────────────────────────────────────────
@app.get("/api/activities")
def get_activities(auth: dict = Depends(require_auth)):
    team = auth["team"]
    with db(team) as c:
        rows = c.execute("SELECT * FROM activities ORDER BY id DESC LIMIT 500").fetchall()
    return [dict(r) for r in rows]

@app.post("/api/activities")
def create_activity(body: dict = Body(...), x_team: Optional[str] = Header(None),
                    auth: dict = Depends(require_role("admin", "editor"))):
    return _insert_activity(body, auth["team"])

def _insert_activity(body: dict, team: str) -> dict:
    """Internal helper — insert or deduplicate an activity record directly.
    Used both by the HTTP endpoint and by internal callers (e.g. jira_pull_sync)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    activity_type = body.get("activity_type", "")
    item_id = body.get("item_id")

    # Deduplicate: if an Open activity of the same type already exists for this item, update it
    if activity_type and item_id is not None:
        with db(team) as c:
            existing = c.execute(
                "SELECT id FROM activities WHERE activity_type=? AND item_id=? AND status='Open' LIMIT 1",
                (activity_type, item_id)
            ).fetchone()
        if existing:
            if body.get("message"):
                note = body["message"]
            else:
                note = body.get("note", "")
            with db(team) as c:
                c.execute(
                    "UPDATE activities SET note=?, message=?, new_value=?, created_ts=? WHERE id=?",
                    (note, body.get("message", ""), body.get("new_value"), ts, existing["id"])
                )
                row = c.execute("SELECT * FROM activities WHERE id=?", (existing["id"],)).fetchone()
            return dict(row)

    with db(team) as c:
        cur = c.execute(
            "INSERT INTO activities(activity_type,source,item_id,item_name,owner,project,"
            "created_by,created_ts,note,status,message,previous_value,new_value)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (activity_type, body.get("source","System"),
             item_id, body.get("item_name",""),
             body.get("owner",""), body.get("project",""),
             body.get("created_by",""), ts,
             body.get("note",""), body.get("status","Open"),
             body.get("message",""), body.get("previous_value"),
             body.get("new_value"))
        )
        new_id = cur.lastrowid
        row = c.execute("SELECT * FROM activities WHERE id=?", (new_id,)).fetchone()
    return dict(row)

@app.put("/api/activities/{aid}")
def update_activity(aid: int, body: dict = Body(...),
                    auth: dict = Depends(require_role("admin", "editor"))):
    team = auth["team"]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Valid status transitions
    VALID_TRANSITIONS = {
        "Open":         {"Read", "Resolved", "Approved", "Rejected", "Dismissed", "Auto-Cleared"},
        "Read":         {"Resolved", "Approved", "Rejected", "Dismissed", "Auto-Cleared", "Open"},
        "Resolved":     {"Open"},
        "Approved":     {"Open"},
        "Rejected":     {"Open"},
        "Dismissed":    {"Open"},
        "Auto-Cleared": {"Open"},
    }
    TERMINAL_STATUSES = {"Resolved", "Approved", "Rejected", "Dismissed", "Auto-Cleared"}

    with db(team) as c:
        row = c.execute("SELECT * FROM activities WHERE id=?", (aid,)).fetchone()
        if not row: raise HTTPException(404, "Activity not found")

        current_status = row["status"]
        new_status = body.get("status", current_status)

        # Validate transition if status is actually changing
        if new_status != current_status:
            allowed = VALID_TRANSITIONS.get(current_status, set())
            if new_status not in allowed:
                raise HTTPException(
                    400,
                    f"Invalid status transition: '{current_status}' → '{new_status}'. "
                    f"Allowed: {', '.join(sorted(allowed)) if allowed else 'none'}"
                )

        action     = body.get("action_taken", row["action_taken"])
        res_by     = body.get("resolved_by", row["resolved_by"])
        res_ts     = ts if new_status in TERMINAL_STATUSES and not row["resolved_ts"] else row["resolved_ts"]
        read_by    = body.get("read_by", row["read_by"])
        read_ts    = ts if read_by and not row["read_ts"] else row["read_ts"]
        note       = body.get("note", row["note"])
        c.execute("UPDATE activities SET status=?,action_taken=?,resolved_by=?,resolved_ts=?,"
                  "read_by=?,read_ts=?,note=? WHERE id=?",
                  (new_status, action, res_by, res_ts, read_by, read_ts, note, aid))
        updated = c.execute("SELECT * FROM activities WHERE id=?", (aid,)).fetchone()
    return dict(updated)


# ── Jira helpers ───────────────────────────────────────────────────────────────
def _jira_req(method: str, path: str, payload: dict = None, timeout: int = 10):
    """Make a Jira REST API call. Raises HTTPException on failure."""
    url = f"{JIRA_BASE}{path}"
    data = json.dumps(payload).encode() if payload else None
    headers = {"Authorization": _jira_auth_header(), "Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except HTTPError as e:
        msg = e.read().decode(errors="replace")[:400]
        raise HTTPException(e.code, msg)
    except URLError as e:
        raise HTTPException(503, str(e.reason))

def _get_all_jira_tickets(team: str) -> dict:
    """Return {ticket_key: item_id} for all items with jiraTickets."""
    with db(team) as c:
        rows = c.execute("SELECT id, data FROM projects").fetchall()
    mapping = {}
    for r in rows:
        p = json.loads(r["data"])
        for t in (p.get("jiraTickets") or []):
            mapping[t] = r["id"]
    return mapping

# ── Jira Metadata (for Team Settings) ────────────────────────────────────────
@app.get("/api/jira/projects")
def list_jira_projects(auth: dict = Depends(require_role("admin"))):
    if not jira_configured(): raise HTTPException(400, "Jira not configured")
    try:
        data = _jira_req("GET", "/rest/api/3/project/search?maxResults=100")
        return [{"key": p["key"], "name": p["name"]} for p in data.get("values", [])]
    except HTTPException: raise
    except Exception as e: raise HTTPException(502, str(e))

@app.get("/api/jira/statuses")
def list_jira_statuses(auth: dict = Depends(require_role("admin"))):
    if not jira_configured(): raise HTTPException(400, "Jira not configured")
    try:
        data = _jira_req("GET", "/rest/api/3/status")
        seen, result = set(), []
        for s in data:
            name = s.get("name","")
            if name and name not in seen:
                seen.add(name)
                result.append({"id": s.get("id",""), "name": name,
                                "category": s.get("statusCategory",{}).get("name","")})
        return result
    except HTTPException: raise
    except Exception as e: raise HTTPException(502, str(e))

@app.get("/api/jira/issuetypes")
def list_jira_issuetypes(auth: dict = Depends(require_role("admin"))):
    if not jira_configured(): raise HTTPException(400, "Jira not configured")
    try:
        data = _jira_req("GET", "/rest/api/3/issuetype")
        seen, result = set(), []
        for t in data:
            name = t.get("name","")
            if name and name not in seen:
                seen.add(name)
                result.append({"id": t.get("id",""), "name": name})
        return result
    except HTTPException: raise
    except Exception as e: raise HTTPException(502, str(e))

@app.post("/api/jira/test")
def test_jira_connection(auth: dict = Depends(require_role("admin"))):
    if not jira_configured():
        return {"ok": False, "error": "Jira credentials not configured on server"}
    try:
        data = _jira_req("GET", "/rest/api/3/myself")
        return {"ok": True, "displayName": data.get("displayName",""),
                "email": data.get("emailAddress",""), "baseUrl": JIRA_BASE}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ── Jira issue details ────────────────────────────────────────────────────────
@app.get("/api/jira/issue/{key}")
def get_jira_issue(key: str, auth: dict = Depends(require_auth)):
    """Fetch full Jira issue details for Item Page display."""
    if not jira_configured(): raise HTTPException(503, "Jira not configured")
    fields = "summary,status,issuetype,project,assignee,priority,description"
    data = _jira_req("GET", f"/rest/api/3/issue/{key}?fields={fields}")
    f = data.get("fields", {})

    # Extract plain text from ADF description
    def adf_to_text(node):
        if not node: return ""
        if isinstance(node, str): return node
        if isinstance(node, dict):
            t = node.get("type","")
            content = node.get("content",[])
            text = node.get("text","")
            if text: return text
            parts = [adf_to_text(c) for c in content]
            sep = "\n" if t in ("paragraph","bulletList","orderedList","listItem","heading") else ""
            return sep.join(p for p in parts if p) + (sep if sep else "")
        if isinstance(node, list): return "\n".join(adf_to_text(c) for c in node)
        return ""

    desc_adf  = f.get("description")
    desc_text = adf_to_text(desc_adf).strip() if desc_adf else ""

    return {
        "key":         data.get("key",""),
        "summary":     f.get("summary",""),
        "status":      f.get("status",{}).get("name",""),
        "statusCategory": f.get("status",{}).get("statusCategory",{}).get("key",""),
        "issueType":   f.get("issuetype",{}).get("name",""),
        "project":     f.get("project",{}).get("name",""),
        "projectKey":  f.get("project",{}).get("key",""),
        "assignee":    (f.get("assignee") or {}).get("displayName","Unassigned"),
        "priority":    (f.get("priority") or {}).get("name",""),
        "url":         f"{JIRA_BASE}/browse/{data.get('key','')}",
        "descriptionText": desc_text,
    }

# ── Create Jira issue from roadmap item ───────────────────────────────────────
@app.post("/api/jira/create-issue")
def create_jira_issue(body: dict = Body(...),
                      auth: dict = Depends(require_role("admin", "editor"))):
    """Create a Jira issue from a roadmap item and return the new key."""
    team = auth["team"]
    if not jira_configured(): raise HTTPException(503, "Jira not configured")

    item_id      = body.get("item_id")
    item_name    = body.get("item_name","")
    description  = body.get("description","")
    project_key  = body.get("project_key","")
    issue_type   = body.get("issue_type","Task")
    username     = body.get("username","")

    if not project_key: raise HTTPException(400, "project_key required")

    # Build Atlassian Document Format description
    adf_content = [{"type":"paragraph","content":[{"type":"text","text":description or item_name}]}]

    payload = {
        "fields": {
            "project": {"key": project_key},
            "summary": item_name,
            "issuetype": {"name": issue_type},
            "description": {"type":"doc","version":1,"content":adf_content}
        }
    }
    result = _jira_req("POST", "/rest/api/3/issue", payload)
    new_key = result.get("key","")
    if not new_key: raise HTTPException(500, "Jira did not return an issue key")

    # Add remote link back to roadmap item
    if item_id:
        roadmap_url = f"{body.get('roadmap_base_url','')}/index.html?item={item_id}"
        try:
            _jira_req("POST", f"/rest/api/3/issue/{new_key}/remotelink", {
                "object": {"url": roadmap_url, "title": f"Roadmap: {item_name}",
                           "icon": {"url16x16": "https://img.icons8.com/color/16/000000/road.png"}}
            })
        except Exception as e:
            log.warning(f"[Jira] Remote link failed for {new_key}: {e}")

    write_audit(team, "jira:create", username, item_id, item_name,
                changes={"jiraKey": new_key, "action": "created"})
    return {"key": new_key, "url": f"{JIRA_BASE}/browse/{new_key}"}

# ── Link existing Jira issue to roadmap item ──────────────────────────────────
@app.post("/api/jira/link-issue")
def link_jira_issue(body: dict = Body(...),
                    auth: dict = Depends(require_role("admin", "editor"))):
    """Validate a Jira key, check for duplicate association, add remote link back."""
    team     = auth["team"]
    if not jira_configured(): raise HTTPException(503, "Jira not configured")

    ticket   = (body.get("ticket","")).strip().upper()
    item_id  = body.get("item_id")
    item_name= body.get("item_name","")
    username = body.get("username","")
    if not ticket: raise HTTPException(400, "ticket required")

    # Duplicate check — scan all items
    existing_map = _get_all_jira_tickets(team)
    if ticket in existing_map and existing_map[ticket] != item_id:
        raise HTTPException(409, f"{ticket} is already linked to item #{existing_map[ticket]}")

    # Verify ticket exists in Jira
    fields = "summary,status,issuetype,project"
    data = _jira_req("GET", f"/rest/api/3/issue/{ticket}?fields={fields}")
    f = data.get("fields",{})

    # Add remote link back to roadmap
    if item_id:
        roadmap_url = f"{body.get('roadmap_base_url','')}/index.html?item={item_id}"
        try:
            _jira_req("POST", f"/rest/api/3/issue/{ticket}/remotelink", {
                "object": {"url": roadmap_url, "title": f"Roadmap: {item_name}",
                           "icon": {"url16x16": "https://img.icons8.com/color/16/000000/road.png"}}
            })
        except Exception as e:
            log.warning(f"[Jira] Remote link failed for {ticket}: {e}")

    write_audit(team, "jira:link", username, item_id, item_name,
                changes={"jiraKey": ticket, "action": "linked"})
    return {
        "key":      ticket,
        "summary":  f.get("summary",""),
        "status":   f.get("status",{}).get("name",""),
        "issueType":f.get("issuetype",{}).get("name",""),
        "project":  f.get("project",{}).get("name",""),
        "url":      f"{JIRA_BASE}/browse/{ticket}",
    }

# ── Add comment to Jira (roadmap-originated changes only) ─────────────────────
@app.post("/api/jira/comment/{key}")
def add_jira_comment(key: str, body: dict = Body(...),
                     auth: dict = Depends(require_role("admin", "editor"))):
    """Add a roadmap-originated comment to a Jira issue."""
    if not jira_configured(): raise HTTPException(503, "Jira not configured")
    text = body.get("text","")
    if not text: raise HTTPException(400, "text required")
    payload = {"body": {"type":"doc","version":1,"content":[
        {"type":"paragraph","content":[{"type":"text","text":text}]}
    ]}}
    _jira_req("POST", f"/rest/api/3/issue/{key}/comment", payload)
    return {"ok": True}

@app.put("/api/jira/issue/{key}")
def update_jira_issue(key: str, body: dict = Body(...),
                      auth: dict = Depends(require_role("admin", "editor"))):
    """Update fields on a Jira issue (description, summary, etc.)."""
    if not jira_configured(): raise HTTPException(503, "Jira not configured")
    fields = body.get("fields", {})
    if not fields: raise HTTPException(400, "fields required")
    _jira_req("PUT", f"/rest/api/3/issue/{key}", {"fields": fields})
    return {"ok": True}

@app.delete("/api/jira/issue/{key}")
def delete_jira_issue(key: str, auth: dict = Depends(require_role("admin"))):
    """Delete a Jira issue. Use with care — permanent."""
    if not jira_configured(): raise HTTPException(503, "Jira not configured")
    _jira_req("DELETE", f"/rest/api/3/issue/{key}")
    return {"ok": True}

# ── Transition Jira issue status ──────────────────────────────────────────────
@app.post("/api/jira/transition/{key}")
def transition_jira_issue(key: str, body: dict = Body(...),
                          auth: dict = Depends(require_role("admin", "editor"))):
    """Transition a Jira issue to a new status via the transitions API."""
    if not jira_configured(): raise HTTPException(503, "Jira not configured")
    target_status = body.get("status","")
    if not target_status: raise HTTPException(400, "status required")

    # Get available transitions
    trans = _jira_req("GET", f"/rest/api/3/issue/{key}/transitions")
    match = next((t for t in trans.get("transitions",[])
                  if t.get("to",{}).get("name","").lower() == target_status.lower()), None)
    if not match:
        raise HTTPException(404, f"No transition to '{target_status}' available for {key}")

    _jira_req("POST", f"/rest/api/3/issue/{key}/transitions",
              {"transition": {"id": match["id"]}})
    return {"ok": True, "transitionId": match["id"], "toStatus": target_status}

# ── Jira → Roadmap status sync (forward-only, change-gated) ───────────────────
@app.post("/api/jira/pull/{pid}")
def jira_pull_sync(pid: int, body: dict = Body({}), x_team: Optional[str] = Header(None),
                   auth: dict = Depends(require_role("admin", "editor"))):
    """
    Pull-sync: fetch Jira fields, apply forward-only status mapping only when the
    Jira status has CHANGED since the last successful sync. Routing:
      - All sync details → audit log only
      - Jira-driven roadmap status change → item history activity only (not AC)
      - Regressions/skips → audit log only, stop retrying until Jira status changes
    """
    team     = auth["team"]
    username = body.get("username", auth["username"])
    if not jira_configured():
        raise HTTPException(503, "Jira not configured")
    # Check if Jira integration is enabled for this team
    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='jiraEnabled'").fetchone()
    if row and json.loads(row["value"]) is False:
        return {"changed": False, "reason": "Jira integration disabled for this team", "issues": []}

    with db(team) as c:
        row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
    if not row: raise HTTPException(404, "Item not found")
    p = json.loads(row["data"])
    tickets = p.get("jiraTickets") or []
    if not tickets:
        return {"changed": False, "reason": "no tickets linked", "issues": []}

    def cfg(k):
        with db(team) as c:
            r = c.execute("SELECT value FROM config WHERE key=?", (k,)).fetchone()
        return json.loads(r["value"]) if r else {}

    fwd_map      = cfg("jiraStatusMapping")
    rev_map      = {v: k for k, v in fwd_map.items() if v}
    all_statuses = cfg("statuses") or []

    def status_rank(s):
        try: return all_statuses.index(s)
        except ValueError: return 999

    now_ts       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    current_rank = status_rank(p.get("status", ""))
    old_status   = p.get("status", "")
    issues_data  = []
    audit_details = []   # all sync details → audit log only
    best_new_status  = None
    best_rank        = current_rank
    best_jira_status = None

    # jiraLastKnownStatus: { ticket: jira_status } — last Jira status we successfully processed
    last_known = p.get("jiraLastKnownStatus") or {}
    # jiraSyncSkipped: { ticket: jira_status } — status we skipped (regression); don't retry same status
    skipped    = p.get("jiraSyncSkipped") or {}

    fields_param = "summary,status,issuetype,project,assignee,parent,subtasks,updated,customfield_10064"

    for ticket in tickets[:10]:
        try:
            data  = _jira_req("GET", f"/rest/api/3/issue/{ticket}?fields={fields_param}")
            f     = data.get("fields", {})
            jira_status  = f.get("status", {}).get("name", "")
            summary      = f.get("summary", "")
            issue_type   = f.get("issuetype", {}).get("name", "")
            project_key  = f.get("project", {}).get("key", "")
            project_name = f.get("project", {}).get("name", "")
            assignee     = (f.get("assignee") or {}).get("displayName", "")
            updated      = f.get("updated", "")
            parent_key   = (f.get("parent") or {}).get("key", "")
            children     = [s.get("key","") for s in (f.get("subtasks") or [])]

            # Collect feature flags from this ticket (root level)
            root_ff    = f.get(JIRA_FF_FIELD) or []
            issue_ff   = set(str(fl) for fl in root_ff if fl) if isinstance(root_ff, list) else set()

            issue_obj = {
                "key": ticket, "summary": summary, "status": jira_status,
                "statusCategory": f.get("status",{}).get("statusCategory",{}).get("key",""),
                "issueType": issue_type, "project": project_name, "projectKey": project_key,
                "assignee": assignee, "parent": parent_key, "children": children,
                "updated": updated, "url": f"{JIRA_BASE}/browse/{ticket}", "syncedAt": now_ts,
                "featureFlags": sorted(issue_ff),
            }
            issues_data.append(issue_obj)

            prev_known = last_known.get(ticket)

            # Only process status change if Jira status has changed since last sync
            if jira_status == prev_known:
                audit_details.append(f"[{ticket}] status unchanged ({jira_status!r}) — no action")
                # Clear any previous skip flag since Jira matches what we last knew
                skipped.pop(ticket, None)
                continue

            # Jira status changed — log to audit
            if prev_known:
                audit_details.append(f"[{ticket}] Jira status changed: {prev_known!r} → {jira_status!r}")
            else:
                audit_details.append(f"[{ticket}] Jira status first seen: {jira_status!r}")

            # If we already skipped this exact Jira status before, don't retry
            if skipped.get(ticket) == jira_status:
                audit_details.append(f"[{ticket}] {jira_status!r} previously skipped — not retrying")
                continue

            mapped = rev_map.get(jira_status)
            if not mapped:
                audit_details.append(f"[{ticket}] no roadmap mapping for Jira status {jira_status!r}")
                last_known[ticket] = jira_status  # record we saw it, even if unmapped
                continue

            mapped_rank = status_rank(mapped)
            if mapped_rank > best_rank:
                best_rank = mapped_rank
                best_new_status = mapped
                best_jira_status = jira_status
                last_known[ticket] = jira_status
                skipped.pop(ticket, None)  # successful candidate — clear any skip flag
            elif mapped_rank == current_rank:
                audit_details.append(f"[{ticket}] {jira_status!r} → {mapped!r} — already at this status")
                last_known[ticket] = jira_status
            else:
                # Regression — skip and remember so we don't retry same status
                audit_details.append(
                    f"[{ticket}] {jira_status!r} → {mapped!r} skipped "
                    f"(regression from {old_status!r}) — will not retry until Jira status changes"
                )
                skipped[ticket] = jira_status
                last_known[ticket] = jira_status  # record we saw it

        except Exception as e:
            issues_data.append({"key": ticket, "error": str(e), "syncedAt": now_ts})
            audit_details.append(f"[{ticket}] fetch error: {e}")

    any_changed = False
    status_change_activity = None
    if best_new_status and best_new_status != old_status:
        p["status"] = best_new_status
        any_changed = True
        audit_details.append(f"Roadmap status updated: {old_status!r} → {best_new_status!r} (Jira: {best_jira_status!r})")
        # This is the ONE event that goes to item history (not AC — status Auto-Cleared)
        status_change_activity = {
            "activity_type": "Action Taken", "source": "Jira",
            "item_id": pid, "item_name": p.get("name",""),
            "owner": p.get("dev",""), "project": p.get("product",""),
            "created_by": "Jira Sync",
            "message": f"Status updated by Jira sync: {old_status!r} → {best_new_status!r} (Jira: {best_jira_status!r})",
            "previous_value": json.dumps({"status": old_status}),
            "new_value": json.dumps({"status": best_new_status, "jiraStatus": best_jira_status}),
            "status": "Auto-Cleared",
        }

    # Persist updated cache, last-known statuses, skip flags, and sync timestamp
    if "jiraCache" not in p: p["jiraCache"] = {}
    for iss in issues_data:
        if "error" not in iss:
            p["jiraCache"][iss["key"]] = iss

    # ── Fetch Feature Flags from full Jira hierarchy, merge into featureFlags ──
    # jiraFeatureFlags is kept as a separate record for audit/display purposes.
    # featureFlags is the unified field used everywhere (manual + Jira combined).
    ff_changed = False
    try:
        all_ff = set()
        for ticket in tickets[:10]:
            all_ff.update(_fetch_jira_feature_flags(ticket))
        if all_ff:
            old_jira_ff = set(str(x) for x in (p.get("jiraFeatureFlags") or []) if x)
            if all_ff != old_jira_ff:
                ff_changed = True
                audit_details.append(f"Feature flags updated: {sorted(all_ff)}")
            p["jiraFeatureFlags"] = sorted(all_ff)
            existing_manual = set(str(x) for x in (p.get("featureFlags") or []) if x)
            p["featureFlags"] = sorted(existing_manual | all_ff)
        elif "jiraFeatureFlags" in p:
            pass  # keep existing, don't wipe on empty fetch
    except Exception as e:
        log.warning(f"[FeatureFlags] FF fetch failed for item {pid} (non-fatal): {e}")

    p["jiraLastSync"]         = now_ts
    p["jiraLastKnownStatus"]  = last_known
    p["jiraSyncSkipped"]      = skipped

    with db(team) as c:
        c.execute("UPDATE projects SET data=? WHERE id=?", (json.dumps(p), pid))

    # Roadmap status change → item history activity (Auto-Cleared, not visible in AC open tab)
    if status_change_activity:
        try:
            _insert_activity(status_change_activity, auth["team"])
        except Exception as e:
            log.warning(f"[Jira] Failed to log status change activity for item {pid}: {e}")

    # Everything else → audit log only (skip when nothing changed to reduce noise)
    if any_changed or ff_changed:
        write_audit(team, "jira:pull", username, pid, p.get("name",""),
                    changes={
                        "tickets": tickets,
                        "statusChanged": any_changed,
                        "ffChanged": ff_changed,
                        "syncedAt": now_ts,
                        "details": audit_details,
                    })
    return {
        "changed": any_changed, "syncedAt": now_ts, "issues": issues_data,
        "statusChange": {"from": old_status, "to": best_new_status} if any_changed else None,
    }

@app.post("/api/jira/sync-status/{pid}")
def sync_jira_to_roadmap(pid: int, body: dict = Body({}), x_team: Optional[str] = Header(None),
                         auth: dict = Depends(require_role("admin", "editor"))):
    """Legacy alias → full pull sync."""
    return jira_pull_sync(pid, body, x_team, auth)

def _is_terminal(status: str, team: str) -> bool:
    """Check if a status is terminal using the team's statusIsTerminal config."""
    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='statusIsTerminal'").fetchone()
    terminal_map = json.loads(row["value"]) if row else {}
    return bool(terminal_map.get(status))

def _do_sync_children(pid: int, p: dict, team: str, username: str) -> dict:
    """Core child-sync logic: fetch Jira children, create missing roadmap child items.
    Reused by both on-demand endpoint and background sync."""
    from datetime import date

    tickets = p.get("jiraTickets") or []
    if not tickets:
        return {"created": 0, "skipped": 0}

    new_start = p.get("start") or date.today().isoformat()
    new_due   = p.get("due") or new_start

    # Build set of Jira keys already tracked as children
    with db(team) as c:
        all_rows = c.execute("SELECT data FROM projects").fetchall()
    existing_jira_keys = set()
    for r in all_rows:
        try:
            item = json.loads(r["data"])
            if item.get("parent") == pid or item.get("recurrence_parent") == pid:
                for key in (item.get("jiraTickets") or []):
                    existing_jira_keys.add(key)
        except Exception:
            pass

    created_ids, skipped_count = [], 0

    for ticket in tickets[:5]:
        try:
            children = _get_jira_children(ticket)
            for child_issue in children:
                sub_key     = child_issue["key"]
                sub_summary = child_issue["summary"]
                if sub_key in existing_jira_keys:
                    # Jira is source of truth: if this key is already a child of a
                    # DIFFERENT parent, re-assign it to this one
                    with db(team) as c:
                        all_r = c.execute("SELECT * FROM projects").fetchall()
                    for rr in all_r:
                        try:
                            existing = json.loads(rr["data"])
                            if sub_key in (existing.get("jiraTickets") or []):
                                existing_parent = existing.get("parent")
                                if existing_parent is not None and existing_parent != pid:
                                    # Re-parent: move to this recurring item
                                    existing["parent"] = pid
                                    existing["recurrence_parent"] = pid
                                    with db(team) as c2:
                                        c2.execute("UPDATE projects SET data=? WHERE id=?",
                                                   (json.dumps(existing), rr["id"]))
                                    write_audit(team, "update", username, rr["id"],
                                                existing.get("name", ""),
                                                changes={"parent": {"from": existing_parent, "to": pid},
                                                         "reason": "jira_reparent"})
                        except Exception:
                            pass
                    skipped_count += 1
                    continue
                roadmap_status = _jira_status_to_roadmap(child_issue.get("status", ""), team)
                if roadmap_status is None:
                    log.info(f"[SyncChildren] {sub_key} status {child_issue.get('status')!r} not mapped — skipping")
                    existing_jira_keys.add(sub_key)
                    skipped_count += 1
                    continue
                roadmap_type = _jira_type_to_roadmap(child_issue.get("issueType", ""), team) or p.get("type", "")
                child = {
                    "name":              sub_summary,
                    "dev":               p.get("dev", ""),
                    "product":           p.get("product", ""),
                    "type":              roadmap_type,
                    "start":             new_start,
                    "due":               new_due,
                    "dueWeeks":          p.get("dueWeeks", 2),
                    "testWeeks":         p.get("testWeeks", 0),
                    "status":            roadmap_status,
                    "jiraTickets":       [sub_key],
                    "jiraSource":        "syncChildren",
                    "parent":            pid,
                    "recurrence_parent": pid,
                    "hidden":            True,   # hidden by default — review before showing on roadmap
                }
                with db(team) as c:
                    cur = c.execute("INSERT INTO projects(data) VALUES(?)", (json.dumps(child),))
                    child_id = cur.lastrowid
                    c.execute("UPDATE projects SET data=? WHERE id=?",
                              (json.dumps({**child, "id": child_id}), child_id))
                existing_jira_keys.add(sub_key)
                created_ids.append(child_id)
                write_audit(team, "create", username, child_id, sub_summary,
                            changes={"jiraChild": sub_key, "syncedFrom": pid,
                                     "status": roadmap_status, "type": roadmap_type})
        except Exception as e:
            log.warning(f"[SyncChildren] failed for {ticket}: {e}")

    return {"created": len(created_ids), "skipped": skipped_count, "childIds": created_ids}

@app.post("/api/jira/pull-all")
def jira_pull_all(body: dict = Body({}), x_team: Optional[str] = Header(None),
                  auth: dict = Depends(require_role("admin"))):
    """Bulk pull-sync all items with linked tickets.
    Also syncs Jira children for items with syncChildren=True."""
    team = auth["team"]
    if not jira_configured(): raise HTTPException(503, "Jira not configured")

    # Check jiraEnabled for this team
    with db(team) as c:
        row_enabled = c.execute("SELECT value FROM config WHERE key='jiraEnabled'").fetchone()
    if row_enabled and json.loads(row_enabled["value"]) is False:
        return {"synced": 0, "skipped": 0, "errors": 0, "changes": 0, "childrenAdded": 0}

    with db(team) as c:
        rows = c.execute("SELECT id, data FROM projects ORDER BY id").fetchall()

    synced = skipped = errors = changes = children_added = 0

    for r in rows:
        p = json.loads(r["data"])
        if not p.get("jiraTickets"):
            skipped += 1
            continue
        # Skip terminal items — no point syncing released/completed items
        if _is_terminal(p.get("status", ""), team):
            skipped += 1
            continue
        try:
            from datetime import date
            # Pull-sync Jira status changes
            result = jira_pull_sync(r["id"], body, x_team, auth)
            synced += 1
            if result.get("changed"):
                changes += 1

            # Child sync: for items with syncChildren enabled and not terminal
            if p.get("syncChildren") and not _is_terminal(p.get("status",""), team):
                try:
                    child_result = _do_sync_children(r["id"], p, team, auth["username"])
                    if child_result["created"] > 0:
                        children_added += child_result["created"]
                        changes += 1  # trigger frontend reload
                except Exception as ce:
                    log.warning(f"[pull-all] child sync failed for item {r['id']}: {ce}")

        except Exception as e:
            errors += 1

    return {"synced": synced, "skipped": skipped, "errors": errors,
            "changes": changes, "childrenAdded": children_added}


# ── Comments ──────────────────────────────────────────────────────────────────
@app.get("/api/comments/{item_id}")
def get_comments(item_id: int, auth: dict = Depends(require_auth)):
    team = auth["team"]
    with db(team) as c:
        rows = c.execute("SELECT * FROM comments WHERE item_id=? ORDER BY id ASC", (item_id,)).fetchall()
    return [dict(r) for r in rows]

@app.post("/api/comments")
def add_comment(body: dict = Body(...), auth: dict = Depends(require_role("admin", "editor"))):
    team = auth["team"]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with db(team) as c:
        cur = c.execute(
            "INSERT INTO comments(item_id,author,body,created_ts) VALUES(?,?,?,?)",
            (body.get("item_id"), body.get("author", auth["username"]), body.get("body",""), ts)
        )
        row = c.execute("SELECT * FROM comments WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)

@app.delete("/api/comments/{cid}")
def delete_comment(cid: int, auth: dict = Depends(require_role("admin", "editor"))):
    team = auth["team"]
    with db(team) as c:
        c.execute("DELETE FROM comments WHERE id=?", (cid,))
    return {"deleted": cid}

# ── Recurrence: spawn next occurrence ─────────────────────────────────────────
def _get_jira_children(ticket: str) -> list:
    """Fetch child issues of a Jira ticket regardless of issue type.
    - Epics: child issues are found via JQL parent= search
    - Stories/Tasks: children are in the subtasks field
    Returns a list of {key, summary, status} dicts."""
    from urllib.parse import quote
    children = []
    try:
        # First fetch the issue to determine its type
        data = _jira_req("GET", f"/rest/api/3/issue/{ticket}?fields=issuetype,subtasks")
        issue_type = data.get("fields", {}).get("issuetype", {}).get("name", "")
        subtasks   = data.get("fields", {}).get("subtasks") or []
        log.info(f"[Jira] {ticket} issueType={issue_type!r} subtasks={len(subtasks)}")

        if issue_type.lower() == "epic":
            # Epics: use JQL to find child issues (parent = ticket)
            # /rest/api/3/search is deprecated (410) — use /rest/api/3/search/jql
            jql_encoded = quote(f"parent={ticket} ORDER BY created ASC")
            search = _jira_req("GET", f"/rest/api/3/search/jql?jql={jql_encoded}&fields=summary,status,issuetype&maxResults=50")
            issues = search.get("issues") or []
            log.info(f"[Jira] Epic {ticket} JQL search returned {len(issues)} issues")
            for issue in issues:
                children.append({
                    "key":       issue["key"],
                    "summary":   issue.get("fields", {}).get("summary", issue["key"]),
                    "status":    issue.get("fields", {}).get("status", {}).get("name", ""),
                    "issueType": issue.get("fields", {}).get("issuetype", {}).get("name", ""),
                })
        else:
            # Stories, Tasks, Bugs, etc.: use the subtasks field
            log.info(f"[Jira] {ticket} non-epic, subtasks count={len(subtasks)}")
            for sub in subtasks:
                children.append({
                    "key":       sub.get("key", ""),
                    "summary":   sub.get("fields", {}).get("summary", sub.get("key", "")),
                    "status":    sub.get("fields", {}).get("status", {}).get("name", ""),
                    "issueType": sub.get("fields", {}).get("issuetype", {}).get("name", ""),
                })
    except Exception as e:
        log.warning(f"[Jira] _get_jira_children failed for {ticket}: {e}")
    log.info(f"[Jira] _get_jira_children({ticket}) → {len(children)} children")
    return [c for c in children if c.get("key")]

def _jira_type_to_roadmap(jira_type: str, team: str) -> str | None:
    """Map a Jira issue type to a roadmap type using the team's jiraTypeMapping.
    Stored as {roadmapType: jiraType}, so reverse it.
    Returns the roadmap type string, or None if not mapped."""
    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='jiraTypeMapping'").fetchone()
    if not row:
        return None
    fwd_map = json.loads(row["value"])  # {roadmapType: jiraType}
    rev_map = {v.strip(): k for k, v in fwd_map.items() if v}  # strip whitespace from Jira values
    return rev_map.get(jira_type.strip())

def _jira_status_to_roadmap(jira_status: str, team: str) -> str | None:
    """Map a Jira status name to a roadmap status using the team's jiraStatusMapping.
    The stored mapping is {roadmapStatus: jiraStatus}, so we reverse it.
    Returns the roadmap status string, or None if not mapped."""
    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='jiraStatusMapping'").fetchone()
    if not row:
        return None
    fwd_map = json.loads(row["value"])  # {roadmapStatus: jiraStatus}
    # Reverse: {jiraStatus: roadmapStatus}
    rev_map = {v: k for k, v in fwd_map.items() if v}
    return rev_map.get(jira_status)  # None if not found

@app.post("/api/jira/search-raw")
def jira_search_raw(body: dict = Body(...), auth: dict = Depends(require_role("admin"))):
    """Debug: run a raw JQL search against Jira and return the full response."""
    from urllib.parse import quote
    jql     = body.get("jql", "")
    fields  = body.get("fields", "summary,status,issuetype,parent")
    max_res = body.get("maxResults", 20)
    if not jql:
        raise HTTPException(400, "jql required")
    jql_enc = quote(jql)
    result  = _jira_req("GET", f"/rest/api/3/search/jql?jql={jql_enc}&fields={fields}&maxResults={max_res}")
    return result

@app.get("/api/jira/children/{key}")
def get_jira_children_debug(key: str, auth: dict = Depends(require_role("admin"))):
    """Debug: test _get_jira_children for a given Jira key."""
    children = _get_jira_children(key)
    return {"key": key, "count": len(children), "children": children}

@app.post("/api/projects/{pid}/sync-children")
def sync_children_from_jira(pid: int, body: dict = Body({}), auth: dict = Depends(require_role("admin", "editor"))):
    """On-demand: fetch Jira children and create missing roadmap child items."""
    team = auth["team"]
    if not jira_configured():
        raise HTTPException(503, "Jira not configured")
    with db(team) as c:
        row_enabled = c.execute("SELECT value FROM config WHERE key='jiraEnabled'").fetchone()
    if row_enabled and json.loads(row_enabled["value"]) is False:
        raise HTTPException(400, "Jira integration disabled for this team")
    with db(team) as c:
        row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
    if not row: raise HTTPException(404, "Item not found")
    p = json.loads(row["data"])
    if not p.get("jiraTickets"):
        raise HTTPException(400, "Item has no linked Jira tickets")
    result = _do_sync_children(pid, p, team, auth["username"])
    return {"created": result["created"], "skipped": result["skipped"], "childIds": result["childIds"]}

# ══════════════════════════════════════════════════════════════════════════════
# CAPACITY OVERRIDES API  (Phase 1 — per-owner dynamic capacity calendar)
# ══════════════════════════════════════════════════════════════════════════════

def _get_owner_default_capacity(owner: str, team: str) -> float:
    """Return the team-configured default capacity for this owner.
    Falls back to 1 if none is set."""
    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='ownerCapacity'").fetchone()
    cap_map = json.loads(row["value"]) if row else {}
    val = cap_map.get(owner)
    try:
        return float(val) if val is not None else 1.0
    except (TypeError, ValueError):
        return 1.0


def get_effective_capacity(owner: str, date_str: str, team: str) -> float:
    """Core capacity helper used by conflict detection and UI.

    Logic:
      1. If a capacity_override exists for (owner, date) → return override.capacity
      2. Otherwise → return ownerCapacity[owner] from config (default capacity)
    """
    with db(team) as c:
        row = c.execute(
            "SELECT capacity FROM capacity_overrides WHERE owner=? AND date=?",
            (owner, date_str)
        ).fetchone()
    if row is not None:
        return float(row["capacity"])
    return _get_owner_default_capacity(owner, team)


def _validate_override_capacity(owner: str, capacity: float, team: str) -> List[str]:
    """Validate a proposed capacity override value. Returns list of error strings."""
    errors = []
    if capacity < 0:
        errors.append("Capacity cannot be negative")
    default = _get_owner_default_capacity(owner, team)
    if capacity > default:
        errors.append(
            f"Override capacity ({capacity}) cannot exceed the owner's default capacity ({default})"
        )
    return errors


@app.post("/api/capacity-overrides")
def create_or_update_override(body: dict = Body(...),
                               auth: dict = Depends(require_role("admin", "editor"))):
    """Create or update a capacity override for an owner on a specific date.

    Body: { owner, date (YYYY-MM-DD), capacity }
    Uses INSERT OR REPLACE so callers don't need to distinguish create vs update.
    """
    team     = auth["team"]
    owner    = body.get("owner", "").strip()
    date_str = body.get("date", "").strip()
    capacity = body.get("capacity")

    # Validation
    if not owner:
        raise HTTPException(400, "owner is required")
    if not date_str:
        raise HTTPException(400, "date is required (YYYY-MM-DD)")
    try:
        capacity = float(capacity)
    except (TypeError, ValueError):
        raise HTTPException(400, "capacity must be a number")

    errors = _validate_override_capacity(owner, capacity, team)
    if errors:
        raise HTTPException(422, {"errors": errors})

    ts = datetime.now(timezone.utc).isoformat()
    note = body.get("note", "").strip()
    with db(team) as c:
        c.execute(
            """INSERT INTO capacity_overrides(owner, date, capacity, modified_by, modified_ts, note)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner, date) DO UPDATE SET
                   capacity    = excluded.capacity,
                   modified_by = excluded.modified_by,
                   modified_ts = excluded.modified_ts,
                   note        = excluded.note""",
            (owner, date_str, capacity, auth["username"], ts, note)
        )
        row = c.execute(
            "SELECT * FROM capacity_overrides WHERE owner=? AND date=?",
            (owner, date_str)
        ).fetchone()

    write_audit(team, "capacity_override:upsert", auth["username"],
                changes={"owner": owner, "date": date_str, "capacity": capacity})
    return dict(row)


@app.delete("/api/capacity-overrides")
def delete_override(owner: str, date: str,
                    auth: dict = Depends(require_role("admin", "editor"))):
    """Delete a capacity override for owner on date (restores default capacity)."""
    team = auth["team"]
    with db(team) as c:
        result = c.execute(
            "DELETE FROM capacity_overrides WHERE owner=? AND date=?",
            (owner, date)
        )
    if result.rowcount == 0:
        raise HTTPException(404, "No override found for that owner/date")
    write_audit(team, "capacity_override:delete", auth["username"],
                changes={"owner": owner, "date": date})
    return {"deleted": True, "owner": owner, "date": date}


@app.get("/api/capacity-overrides")
def get_overrides(owner: Optional[str] = None,
                  date_from: Optional[str] = None,
                  date_to: Optional[str] = None,
                  auth: dict = Depends(require_auth)):
    """Fetch capacity overrides, optionally filtered by owner and/or date range.

    Query params: owner, date_from (YYYY-MM-DD), date_to (YYYY-MM-DD)
    Returns list of override records sorted by owner, date.
    """
    team = auth["team"]
    clauses, params = [], []
    if owner:
        clauses.append("owner = ?"); params.append(owner)
    if date_from:
        clauses.append("date >= ?"); params.append(date_from)
    if date_to:
        clauses.append("date <= ?"); params.append(date_to)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with db(team) as c:
        rows = c.execute(
            f"SELECT * FROM capacity_overrides {where} ORDER BY owner, date",
            params
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/capacity-overrides/effective")
def get_effective_capacity_endpoint(owner: str, date: str,
                                    auth: dict = Depends(require_auth)):
    """Return the effective capacity for owner on date.
    Checks override table first, then falls back to ownerCapacity default.
    """
    team     = auth["team"]
    capacity = get_effective_capacity(owner, date, team)
    default  = _get_owner_default_capacity(owner, team)
    with db(team) as c:
        override = c.execute(
            "SELECT * FROM capacity_overrides WHERE owner=? AND date=?",
            (owner, date)
        ).fetchone()
    return {
        "owner":          owner,
        "date":           date,
        "capacity":       capacity,
        "default":        default,
        "has_override":   override is not None,
        "override":       dict(override) if override else None
    }


@app.post("/api/capacity-overrides/batch")
def batch_upsert_overrides(body: dict = Body(...),
                            auth: dict = Depends(require_role("admin", "editor"))):
    """Upsert multiple overrides at once — used by the calendar range-edit UI.

    Body: { owner, overrides: [ { date, capacity }, ... ] }
    """
    team    = auth["team"]
    owner   = body.get("owner", "").strip()
    entries = body.get("overrides", [])
    if not owner:
        raise HTTPException(400, "owner is required")
    if not isinstance(entries, list) or not entries:
        raise HTTPException(400, "overrides must be a non-empty list")

    ts = datetime.now(timezone.utc).isoformat()
    note = body.get("note", "").strip()
    saved, errors = [], []
    for entry in entries:
        date_str = entry.get("date", "").strip()
        try:
            cap = float(entry.get("capacity"))
        except (TypeError, ValueError):
            errors.append({"date": date_str, "error": "capacity must be a number"})
            continue
        errs = _validate_override_capacity(owner, cap, team)
        if errs:
            errors.append({"date": date_str, "errors": errs})
            continue
        entry_note = entry.get("note", note).strip()  # per-entry note falls back to body-level note
        with db(team) as c:
            c.execute(
                """INSERT INTO capacity_overrides(owner, date, capacity, modified_by, modified_ts, note)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(owner, date) DO UPDATE SET
                       capacity    = excluded.capacity,
                       modified_by = excluded.modified_by,
                       modified_ts = excluded.modified_ts,
                       note        = excluded.note""",
                (owner, date_str, cap, auth["username"], ts, entry_note)
            )
        saved.append({"date": date_str, "capacity": cap})

    write_audit(team, "capacity_override:batch", auth["username"],
                changes={"owner": owner, "saved": len(saved), "errors": len(errors)})
    return {"saved": len(saved), "errors": errors, "owner": owner}


# ══════════════════════════════════════════════════════════════════════════════
# PLANNING SESSIONS API  (Phase 6)
# ══════════════════════════════════════════════════════════════════════════════

def _validate_session_payload(body: dict, session_type: str) -> List[str]:
    """Validate a planning session payload. Returns list of error strings."""
    errors = []
    if not body.get("name", "").strip():
        errors.append("Session name is required")
    if session_type not in ("Review", "Sprint", "Release"):
        errors.append("Session type must be Review, Sprint, or Release")
    if session_type == "Sprint":
        for item in body.get("sprint_items", []):
            if not item.get("start"):
                errors.append(f"Sprint item '{item.get('name','?')}' is missing a start date")
    if session_type == "Release":
        if not body.get("release_number", "").strip():
            errors.append("Release number is required")
        flags = body.get("feature_flags", [])
        unchecked = [f for f in flags if not f.get("checked")]
        if unchecked:
            errors.append(f"{len(unchecked)} feature flag(s) not checked: {', '.join(f['name'] for f in unchecked)}")
    return errors


@app.post("/api/planning-sessions")
def create_planning_session(body: dict = Body(...),
                            auth: dict = Depends(require_role("admin", "editor"))):
    """Create a new planning session (draft)."""
    team = auth["team"]
    name = body.get("name", "").strip()
    stype = body.get("type", "")
    if not name:
        raise HTTPException(400, "Session name is required")
    if stype not in ("Review", "Sprint", "Release"):
        raise HTTPException(400, "Session type must be Review, Sprint, or Release")
    session_id = body.get("id") or f"ps_{int(time.time()*1000)}"
    ts = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(body.get("payload", {}))
    with db(team) as c:
        c.execute(
            """INSERT OR REPLACE INTO planning_sessions
               (id, name, type, status, created_by, created_ts, payload)
               VALUES (?,?,?,'draft',?,?,?)""",
            (session_id, name, stype, auth["username"], ts, payload)
        )
    write_audit(team, "planning_session:create", auth["username"],
                changes={"session_id": session_id, "name": name, "type": stype})
    return {"id": session_id, "name": name, "type": stype, "status": "draft", "created_ts": ts}


@app.get("/api/planning-sessions")
def list_planning_sessions(status: Optional[str] = None,
                           auth: dict = Depends(require_role("admin", "editor"))):
    """List planning sessions for this team, optionally filtered by status."""
    team = auth["team"]
    with db(team) as c:
        if status:
            rows = c.execute(
                "SELECT * FROM planning_sessions WHERE status=? ORDER BY created_ts DESC",
                (status,)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM planning_sessions ORDER BY created_ts DESC LIMIT 100"
            ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/planning-sessions/active")
def get_active_session(auth: dict = Depends(require_role("admin", "editor"))):
    """Return the most recent draft session for this team, if any.
    Used on page load to restore an interrupted session."""
    team = auth["team"]
    with db(team) as c:
        row = c.execute(
            """SELECT * FROM planning_sessions
               WHERE status='draft'
               ORDER BY created_ts DESC LIMIT 1"""
        ).fetchone()
    if not row:
        return {"session": None}
    return {
        "session": {
            "id":            row["id"],
            "name":          row["name"],
            "type":          row["type"],
            "created_by":    row["created_by"],
            "created_ts":    row["created_ts"],
            "release_number": row["release_number"],
            "payload":       json.loads(row["payload"] or "{}"),
            "snapshot":      json.loads(row["snapshot"] or "{}"),
        }
    }

@app.get("/api/planning-sessions/{session_id}")
def get_planning_session(session_id: str,
                         auth: dict = Depends(require_role("admin", "editor"))):
    """Get a single planning session by ID."""
    team = auth["team"]
    with db(team) as c:
        row = c.execute(
            "SELECT * FROM planning_sessions WHERE id=?", (session_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Planning session not found")
    return dict(row)


@app.put("/api/planning-sessions/{session_id}/draft")
def update_session_draft(session_id: str, body: dict = Body(...),
                         auth: dict = Depends(require_role("admin", "editor"))):
    """Save/update the draft payload of a planning session."""
    team = auth["team"]
    with db(team) as c:
        row = c.execute(
            "SELECT status FROM planning_sessions WHERE id=?", (session_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Planning session not found")
    if row["status"] != "draft":
        raise HTTPException(400, f"Cannot update a {row['status']} session")
    with db(team) as c:
        c.execute(
            "UPDATE planning_sessions SET payload=? WHERE id=?",
            (json.dumps(body.get("payload", {})), session_id)
        )
    return {"id": session_id, "status": "draft", "updated": True}


@app.post("/api/planning-sessions/{session_id}/commit")
def commit_planning_session(session_id: str, body: dict = Body(...),
                            auth: dict = Depends(require_role("admin", "editor"))):
    """Commit a planning session. Validates payload, applies all changes atomically.

    Body schema:
    {
      "name": str,
      "type": "Review"|"Sprint"|"Release",
      "release_number": str?,           # Release only
      "release_notes": str?,            # Release only
      "feature_flags": [{name, checked}], # Release only
      "approved_ids": [int],            # Review only
      "deferred_ids": [int],            # all types
      "sprint_items": [{id, start}],    # Sprint only
      "at_risk_ids": [int],             # Sprint only
      "release_ids": [int],             # Release only
      "blocked_ids": [int],             # Release only
      "priority_changes": [{id, priority}]  # all types
    }
    """
    team = auth["team"]
    stype = body.get("type", "")

    # ── Validation ─────────────────────────────────────────────────────────────
    errors = _validate_session_payload(body, stype)
    if errors:
        raise HTTPException(422, {"errors": errors})

    # ── Load config helpers ────────────────────────────────────────────────────
    with db(team) as c:
        cfg_rows = {r["key"]: json.loads(r["value"])
                    for r in c.execute("SELECT key,value FROM config").fetchall()}
    statuses_list   = cfg_rows.get("statuses", [])
    status_is_active   = cfg_rows.get("statusIsActive", {})
    status_is_deferred = cfg_rows.get("statusIsDeferred", {})
    status_is_approved = cfg_rows.get("statusIsApproved", {})
    status_is_released = cfg_rows.get("statusIsReleased", {})

    def first_active():
        return next((s for s in statuses_list if status_is_active.get(s)), "")

    def get_deferred_status():
        return next((s for s in statuses_list if status_is_deferred.get(s)), "")

    def get_approved_status():
        return next((s for s in statuses_list if status_is_approved.get(s)), "")

    def get_released_status():
        return next((s for s in statuses_list if status_is_released.get(s)), "")

    # ── Load all projects for this team ────────────────────────────────────────
    with db(team) as c:
        proj_rows = {r["id"]: json.loads(r["data"])
                     for r in c.execute("SELECT id, data FROM projects").fetchall()}

    today       = datetime.now(timezone.utc).date().isoformat()
    changes     = []
    to_save     = {}   # {id: updated_project_dict}
    activities  = []   # [{...activity body}]
    session_name = body.get("name", "")
    rel_number   = body.get("release_number", "").strip()
    rel_notes    = body.get("release_notes", "")

    # ── REVIEW: Approved items → Approved status ───────────────────────────────
    if stype == "Review":
        approved_status = get_approved_status()
        for item_id in body.get("approved_ids", []):
            p = proj_rows.get(item_id)
            if not p: continue
            orig_status = p.get("status", "")
            if approved_status:
                p["status"] = approved_status
            updated = {**p, "id": item_id}
            to_save[item_id] = updated
            changes.append({"item_id": item_id, "name": p.get("name",""),
                             "action": "approved", "status_from": orig_status,
                             "status_to": approved_status or orig_status})
            if p.get("dev"):
                activities.append({"activity_type": "Priority Change", "source": "User",
                    "item_id": item_id, "item_name": p.get("name",""),
                    "owner": p["dev"], "project": p.get("product",""),
                    "created_by": auth["username"],
                    "message": f"[{session_name}] Approved for work → {approved_status}",
                    "status": "Open"})

    # ── SPRINT: Sprint items → first Active status + start date ───────────────
    if stype == "Sprint":
        active_status = first_active()
        for item in body.get("sprint_items", []):
            item_id = item.get("id")
            start   = item.get("start", "")
            if not item_id or not start:
                raise HTTPException(422, {"errors": [f"Sprint item {item_id} missing start date"]})
            p = proj_rows.get(item_id)
            if not p: continue
            orig_status = p.get("status", "")
            if active_status: p["status"] = active_status
            p["start"] = start
            to_save[item_id] = {**p, "id": item_id}
            changes.append({"item_id": item_id, "name": p.get("name",""),
                             "action": "sprint", "status_from": orig_status,
                             "status_to": active_status, "start": start})
            if p.get("dev"):
                activities.append({"activity_type": "Priority Change", "source": "User",
                    "item_id": item_id, "item_name": p.get("name",""),
                    "owner": p["dev"], "project": p.get("product",""),
                    "created_by": auth["username"],
                    "message": f"[{session_name}] Added to sprint → {active_status} · start: {start}",
                    "status": "Open"})
        for item_id in body.get("at_risk_ids", []):
            p = proj_rows.get(item_id)
            if not p: continue
            to_save.setdefault(item_id, {**p, "id": item_id})
            changes.append({"item_id": item_id, "name": p.get("name",""), "action": "at_risk"})
            activities.append({"activity_type": "At Risk", "source": "User",
                "item_id": item_id, "item_name": p.get("name",""),
                "owner": p.get("dev",""), "project": p.get("product",""),
                "created_by": auth["username"],
                "message": f"[{session_name}] Flagged At Risk in Sprint planning",
                "status": "Open"})

    # ── RELEASE: Release items → Released status + release metadata ───────────
    if stype == "Release":
        released_status = get_released_status()
        for item_id in body.get("release_ids", []):
            p = proj_rows.get(item_id)
            if not p: continue
            orig_status = p.get("status","")
            if released_status: p["status"] = released_status
            p["releaseDate"]   = today
            p["releaseNumber"] = rel_number
            if rel_notes: p["releaseNotes"] = rel_notes
            to_save[item_id] = {**p, "id": item_id}
            changes.append({"item_id": item_id, "name": p.get("name",""),
                             "action": "released", "status_from": orig_status,
                             "status_to": released_status, "release_date": today,
                             "release_number": rel_number})
            if p.get("dev"):
                activities.append({"activity_type": "Priority Change", "source": "User",
                    "item_id": item_id, "item_name": p.get("name",""),
                    "owner": p["dev"], "project": p.get("product",""),
                    "created_by": auth["username"],
                    "message": f"[{session_name}] Released {rel_number} → {released_status} · {today}",
                    "status": "Open"})
        for item_id in body.get("blocked_ids", []):
            p = proj_rows.get(item_id)
            if not p: continue
            to_save.setdefault(item_id, {**p, "id": item_id})
            changes.append({"item_id": item_id, "name": p.get("name",""), "action": "blocked"})
            activities.append({"activity_type": "Blocked", "source": "User",
                "item_id": item_id, "item_name": p.get("name",""),
                "owner": p.get("dev",""), "project": p.get("product",""),
                "created_by": auth["username"],
                "message": f"[{session_name}] Blocked from release {rel_number}",
                "status": "Open"})

    # ── ALL TYPES: Deferred items ──────────────────────────────────────────────
    deferred_status = get_deferred_status()
    for item in body.get("deferred_items", []):
        item_id = item.get("id")
        p = proj_rows.get(item_id)
        if not p: continue
        if deferred_status: p["status"] = deferred_status
        p["deferred"]     = True
        p["deferReason"]  = item.get("reason", "")
        p["deferNote"]    = item.get("note", "")
        p["deferRevisit"] = item.get("revisit", "")
        p["start"] = p["revised"] = p["expected"] = p["releaseDate"] = p["delay"] = ""
        to_save[item_id] = {**p, "id": item_id}
        changes.append({"item_id": item_id, "name": p.get("name",""),
                         "action": "deferred", "reason": item.get("reason","")})
        if p.get("dev"):
            activities.append({"activity_type": "Priority Change", "source": "User",
                "item_id": item_id, "item_name": p.get("name",""),
                "owner": p["dev"], "project": p.get("product",""),
                "created_by": auth["username"],
                "message": f"[{session_name}] Deferred: {item.get('reason','')}",
                "status": "Open"})

    # ── ALL TYPES: Priority changes ────────────────────────────────────────────
    for pc in body.get("priority_changes", []):
        item_id  = pc.get("id")
        new_prio = pc.get("priority")
        p = proj_rows.get(item_id)
        if not p or new_prio is None: continue
        old_prio = p.get("priority")
        p["priority"] = new_prio
        to_save.setdefault(item_id, {**p, "id": item_id})
        to_save[item_id]["priority"] = new_prio
        if old_prio != new_prio:
            changes.append({"item_id": item_id, "name": p.get("name",""),
                             "action": "priority", "from": old_prio, "to": new_prio})

    # ── Atomic DB write: save all changed projects + activities + session ──────
    committed_ts = datetime.now(timezone.utc).isoformat()
    with db(team) as c:
        for item_id, updated in to_save.items():
            c.execute("UPDATE projects SET data=? WHERE id=?",
                      (json.dumps(updated), item_id))
        for act in activities:
            act.setdefault("created_ts", committed_ts)
            c.execute("""INSERT INTO activities
                (activity_type,source,item_id,item_name,owner,project,
                 created_by,created_ts,status,message)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (act["activity_type"], act.get("source","User"),
                 act.get("item_id"), act.get("item_name",""),
                 act.get("owner",""), act.get("project",""),
                 act.get("created_by",""), act.get("created_ts", committed_ts),
                 act.get("status","Open"), act.get("message","")))
        # Upsert session record as committed
        c.execute("""INSERT OR REPLACE INTO planning_sessions
            (id, name, type, status, created_by, created_ts, committed_ts,
             release_number, release_notes, payload)
            VALUES (?,?,?,'committed',?,?,?,?,?,?)""",
            (session_id, session_name, stype, auth["username"],
             committed_ts, committed_ts,
             rel_number or None, rel_notes or None,
             json.dumps({"changes": changes})))

    write_audit(team, "planning_session:commit", auth["username"],
                changes={"session_id": session_id, "type": stype,
                         "items_changed": len(to_save), "release_number": rel_number})

    return {
        "session_id": session_id, "type": stype, "status": "committed",
        "committed_ts": committed_ts, "changes": changes,
        "items_changed": len(to_save), "activities_posted": len(activities)
    }


@app.delete("/api/planning-sessions/{session_id}")
def discard_planning_session(session_id: str,
                             auth: dict = Depends(require_role("admin", "editor"))):
    """Mark a planning session as discarded."""
    team = auth["team"]
    with db(team) as c:
        row = c.execute(
            "SELECT status FROM planning_sessions WHERE id=?", (session_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Session not found")
    if row["status"] == "committed":
        raise HTTPException(400, "Cannot discard a committed session")
    with db(team) as c:
        c.execute(
            "UPDATE planning_sessions SET status='discarded' WHERE id=?",
            (session_id,)
        )
    write_audit(team, "planning_session:discard", auth["username"],
                changes={"session_id": session_id})
    return {"id": session_id, "status": "discarded"}


@app.post("/api/planning-sessions/{session_id}/release-metadata")
def save_release_metadata(session_id: str, body: dict = Body(...),
                          auth: dict = Depends(require_role("admin", "editor"))):
    """Save release number and notes to a draft session."""
    team = auth["team"]
    rel_number = body.get("release_number", "").strip()
    rel_notes  = body.get("release_notes", "")
    if not rel_number:
        raise HTTPException(400, "Release number is required")
    with db(team) as c:
        row = c.execute(
            "SELECT status FROM planning_sessions WHERE id=?", (session_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Session not found")
    if row["status"] != "draft":
        raise HTTPException(400, f"Cannot update a {row['status']} session")
    with db(team) as c:
        c.execute(
            """UPDATE planning_sessions
               SET release_number=?, release_notes=? WHERE id=?""",
            (rel_number, rel_notes, session_id)
        )
    return {"id": session_id, "release_number": rel_number, "saved": True}


@app.post("/api/planning-sessions/{session_id}/validate")
def validate_session(session_id: str, body: dict = Body(...),
                     auth: dict = Depends(require_role("admin", "editor"))):
    """Pre-validate a session payload without committing. Returns errors list."""
    stype  = body.get("type", "")
    errors = _validate_session_payload(body, stype)
    return {"valid": len(errors) == 0, "errors": errors}


@app.post("/api/planning-sessions/{session_id}/check-conflicts")
def check_session_conflicts(session_id: str, body: dict = Body(...),
                            auth: dict = Depends(require_role("admin", "editor"))):
    """Compare item statuses in the client's snapshot against current DB state.
    Returns list of items where status has changed since the session was started."""
    team     = auth["team"]
    snapshot = body.get("snapshot", {})   # { "itemId": { "status": "...", ... } }
    if not snapshot:
        return {"conflicts": [], "conflict_count": 0}

    with db(team) as c:
        rows = c.execute("SELECT id, data FROM projects").fetchall()

    conflicts = []
    for row in rows:
        item_id = str(row["id"])
        if item_id not in snapshot:
            continue
        current = json.loads(row["data"])
        snap    = snapshot[item_id]
        if current.get("status") != snap.get("status"):
            conflicts.append({
                "item_id":        row["id"],
                "item_name":      current.get("name", ""),
                "status_at_start": snap.get("status", ""),
                "status_now":     current.get("status", ""),
            })
    return {"conflicts": conflicts, "conflict_count": len(conflicts)}


@app.post("/api/planning-sessions/{session_id}/acquire-lock")
def acquire_session_lock(session_id: str, body: dict = Body({}),
                         auth: dict = Depends(require_role("admin"))):
    """Attempt to acquire a commit lock for this session.
    Returns {acquired: bool, locked_by: str|null}.
    Lock expires after 5 minutes of inactivity."""
    team = auth["team"]
    username = auth["username"]
    now_ts = datetime.now(timezone.utc).isoformat()
    lock_expiry_minutes = 5

    with db(team) as c:
        row = c.execute(
            "SELECT locked_by, locked_ts FROM planning_sessions WHERE id=?",
            (session_id,)
        ).fetchone()

    if not row:
        raise HTTPException(404, "Session not found")

    locked_by = row["locked_by"]
    locked_ts = row["locked_ts"]

    # Check if existing lock is still valid (not expired and not ours)
    lock_valid = False
    if locked_by and locked_by != username and locked_ts:
        try:
            from datetime import timedelta
            lock_time = datetime.fromisoformat(locked_ts.replace("Z", "+00:00"))
            lock_valid = (datetime.now(timezone.utc) - lock_time).total_seconds() < lock_expiry_minutes * 60
        except Exception:
            lock_valid = False

    if lock_valid:
        return {"acquired": False, "locked_by": locked_by, "locked_ts": locked_ts}

    # Acquire the lock
    with db(team) as c:
        c.execute(
            "UPDATE planning_sessions SET locked_by=?, locked_ts=? WHERE id=?",
            (username, now_ts, session_id)
        )
    return {"acquired": True, "locked_by": username, "locked_ts": now_ts}


@app.post("/api/planning-sessions/{session_id}/release-lock")
def release_session_lock(session_id: str,
                         auth: dict = Depends(require_role("admin"))):
    """Release a commit lock held by the current user."""
    team = auth["team"]
    username = auth["username"]
    with db(team) as c:
        row = c.execute(
            "SELECT locked_by FROM planning_sessions WHERE id=?", (session_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Session not found")
    if row["locked_by"] and row["locked_by"] != username:
        raise HTTPException(403, "Lock held by another user")
    with db(team) as c:
        c.execute(
            "UPDATE planning_sessions SET locked_by=NULL, locked_ts=NULL WHERE id=?",
            (session_id,)
        )
    return {"released": True}


@app.put("/api/planning-sessions/{session_id}/snapshot")
def save_session_snapshot(session_id: str, body: dict = Body(...),
                          auth: dict = Depends(require_role("admin", "editor"))):
    """Persist the client's item snapshot to the server for conflict detection."""
    team = auth["team"]
    snapshot = body.get("snapshot", {})
    with db(team) as c:
        c.execute(
            "UPDATE planning_sessions SET snapshot=? WHERE id=?",
            (json.dumps(snapshot), session_id)
        )
    return {"saved": True}




def _sync_recurrence_child_statuses(parent_id: int, team: str, username: str) -> dict:
    """For all hidden children of a recurring parent item:
    1. Re-fetch their Jira status.
    2. If Jira says Done/Released → mark the roadmap item as the Released status.
    3. Stamp releaseDate if not already set.
    Returns {updated: count, errors: []}."""
    from datetime import date
    if not jira_configured():
        return {"updated": 0, "errors": []}

    # Get the released status for this team
    with db(team) as c:
        rel_row = c.execute("SELECT value FROM config WHERE key='statusIsReleased'").fetchone()
        all_rows = c.execute("SELECT * FROM projects").fetchall()
    rel_map = json.loads(rel_row["value"]) if rel_row else {}
    released_status = next((s for s, v in rel_map.items() if v), None)
    if not released_status:
        return {"updated": 0, "errors": ["No released status configured"]}

    # Collect children of this parent (including hidden)
    children = []
    for r in all_rows:
        try:
            item = json.loads(r["data"])
            if item.get("parent") == parent_id or item.get("recurrence_parent") == parent_id:
                item["_row_id"] = r["id"]
                children.append(item)
        except Exception:
            pass

    updated, errors = 0, []
    today_str = date.today().isoformat()

    for child in children:
        tickets = child.get("jiraTickets") or []
        if not tickets:
            continue
        # Skip if already in a terminal status
        with db(team) as c:
            term_row = c.execute("SELECT value FROM config WHERE key='statusIsTerminal'").fetchone()
        term_map = json.loads(term_row["value"]) if term_row else {}
        if term_map.get(child.get("status", "")):
            continue
        for ticket in tickets[:1]:  # primary ticket only
            try:
                data = _jira_req("GET", f"/rest/api/3/issue/{ticket}?fields=status,resolutiondate")
                jira_status = data.get("fields", {}).get("status", {}).get("name", "")
                roadmap_status = _jira_status_to_roadmap(jira_status, team)
                if roadmap_status and rel_map.get(roadmap_status):
                    # Jira says this child is done — mark it released
                    child_updated = dict(child)
                    child_updated["status"] = released_status
                    if not child_updated.get("releaseDate"):
                        # Use Jira resolution date if available, else today
                        res_date = data.get("fields", {}).get("resolutiondate", "")
                        child_updated["releaseDate"] = res_date[:10] if res_date else today_str
                    row_id = child.get("_row_id") or child.get("id")
                    child_updated.pop("_row_id", None)
                    with db(team) as c:
                        c.execute("UPDATE projects SET data=? WHERE id=?",
                                  (json.dumps(child_updated), row_id))
                    write_audit(team, "update", username, row_id,
                                child.get("name", ""),
                                changes={"status": {"from": child.get("status"), "to": released_status},
                                         "source": "jira_recurrence_sync"})
                    updated += 1
            except Exception as e:
                errors.append(f"{ticket}: {e}")

    return {"updated": updated, "errors": errors}


@app.post("/api/projects/{pid}/recur")
def spawn_recurrence(pid: int, body: dict = Body({}), auth: dict = Depends(require_role("admin", "editor"))):
    """Called when a recurring item becomes terminal. Creates the next recurrence with
    a start date advanced by the recurrence period, and optionally syncs Jira subtasks
    as child items if syncChildren is enabled."""
    team = auth["team"]
    with db(team) as c:
        row = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not row: raise HTTPException(404, "Item not found")
    p = json.loads(row["data"])
    recurrence = p.get("recurrence", "none")
    if recurrence == "none": raise HTTPException(400, "Item is not recurring")

    from datetime import date, timedelta

    # Period map: advance start by the recurrence interval
    PERIOD_DAYS = {"weekly": 7, "biweekly": 14, "monthly": 30}
    period_days = PERIOD_DAYS.get(recurrence, 7)

    # Bug fix: new start = previous start + period (not today)
    prev_start_str = p.get("start") or date.today().isoformat()
    try:
        prev_start = date.fromisoformat(prev_start_str)
    except ValueError:
        prev_start = date.today()
    new_start = prev_start + timedelta(days=period_days)
    new_start_str = new_start.isoformat()

    # Compute new due from dueWeeks
    dueWeeks = p.get("dueWeeks", 2)
    new_due = (new_start + timedelta(weeks=dueWeeks)).isoformat()

    # Build new item — strip per-cycle fields, carry forward config fields
    skip_keys = {"id","status","delay","revised","expected","releaseDate",
                 "jiraTickets","jiraCache","jiraLastSync","jiraLastKnownStatus",
                 "jiraSyncSkipped","revokedAt"}
    new_item = {k: v for k, v in p.items() if k not in skip_keys}
    new_item["start"]              = new_start_str
    new_item["due"]                = new_due
    # Use the team's configured default status (e.g. "New") not hardcoded "Planned"
    with db(team) as c:
        def_row = c.execute("SELECT value FROM config WHERE key='statusIsDefault'").fetchone()
    def_map = json.loads(def_row["value"]) if def_row else {}
    default_status = next((s for s, v in def_map.items() if v), "New")
    new_item["status"]             = default_status
    new_item["recurrence"]         = recurrence
    new_item["syncChildren"]       = p.get("syncChildren", False)
    new_item["recurrence_parent"]  = pid

    with db(team) as c:
        cur = c.execute("INSERT INTO projects(data) VALUES(?)", (json.dumps(new_item),))
        new_id = cur.lastrowid
        c.execute("UPDATE projects SET data=? WHERE id=?",
                  (json.dumps({**new_item, "id": new_id}), new_id))
        new_row = c.execute("SELECT * FROM projects WHERE id=?", (new_id,)).fetchone()
    result = json.loads(new_row["data"])
    result["id"] = new_id

    write_audit(team, "create", auth["username"], new_id, new_item.get("name",""),
                changes={"recurrenceOf": pid, "newStart": new_start_str})

    # ── Children are NOT copied from the preceding item ─────────────────────────
    # The new recurring item starts with no children. Once a Jira ticket is linked
    # to the new item and syncChildren is enabled, the regular Jira sync will
    # fetch children from that ticket's Jira hierarchy.

    # Sync existing children's Jira statuses on the OLD parent (mark Done ones as Released)
    sync_result = _sync_recurrence_child_statuses(pid, team, auth["username"])
    log.info(f"[Recur] Child status sync for old parent {pid}: {sync_result}")

    result["_childIds"]     = []
    result["_childSynced"]  = sync_result.get("updated", 0)
    return result

@app.post("/api/projects/{pid}/sync-children-status")
def sync_children_status_endpoint(pid: int, body: dict = Body({}),
                                   auth: dict = Depends(require_role("admin", "editor"))):
    """On-demand: re-check Jira status for all children of a recurring item
    and mark released ones as Released in the roadmap."""
    team = auth["team"]
    with db(team) as c:
        row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
    if not row: raise HTTPException(404, "Item not found")
    result = _sync_recurrence_child_statuses(pid, team, auth["username"])
    return result


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
