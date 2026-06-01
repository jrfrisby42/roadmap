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
_TOKEN_SECRET = os.environ.get("TOKEN_SECRET", secrets.token_hex(32))
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

def _migrate_config_keys(team: str):
    """Backfill any new config keys that didn't exist when the team was created."""
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
APP_VERSION = "3.1.0"

app = FastAPI(title="Frazil Roadmap", version=APP_VERSION)

# ── Debug: expose Python traceback in 500 responses so we can diagnose ───────
import traceback as _traceback
from fastapi.responses import JSONResponse
from fastapi import Request as _Request

@app.exception_handler(Exception)
async def _debug_exception_handler(request: _Request, exc: Exception):
    tb = _traceback.format_exc()
    log.error(f"[500] {request.method} {request.url.path}\n{tb}")
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc), "traceback": tb}
    )

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
