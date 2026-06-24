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

# ── Email (Amazon SES via boto3 + EC2 instance role) ─────────────────────────
# Matches the rest of the Frazil fleet (dashboard, sharebox): authenticate to
# SES through the instance IAM role — no SMTP creds, no stored AWS keys. boto3
# is imported lazily so server.py still loads in dev/test environments without it.
MAIL_FROM    = os.environ.get("MAIL_FROM", "notifications@frazil.app")
AWS_REGION   = os.environ.get("AWS_REGION", "us-west-2")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://roadmap.frazil.app").rstrip("/")

def mail_configured() -> bool:
    """True when sending is possible: boto3 importable + a From address set.
    (SES authorization comes from the instance role and is checked at send time.)"""
    if not MAIL_FROM:
        return False
    try:
        import boto3  # noqa: F401
        return True
    except ImportError:
        return False

def send_email(to_addr: str, subject: str, text_body: str, html_body: str = None):
    """Send one email via the SES API using the instance role. Raises on failure."""
    try:
        import boto3
    except ImportError as e:
        raise RuntimeError(f"boto3 not installed: {e}")
    body = {"Text": {"Data": text_body, "Charset": "UTF-8"}}
    if html_body:
        body["Html"] = {"Data": html_body, "Charset": "UTF-8"}
    client = boto3.client("ses", region_name=AWS_REGION)
    client.send_email(
        Source=MAIL_FROM,
        Destination={"ToAddresses": [to_addr]},
        Message={"Subject": {"Data": subject, "Charset": "UTF-8"}, "Body": body},
    )

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

# ── Password-link tokens (reset / invite) ────────────────────────────────────
# Signed like auth tokens, but bound to the user's CURRENT password hash so a
# link is single-use: once the password is set/changed, the bind no longer
# matches and any earlier link stops validating. No DB table needed.
_RESET_TTL  = 3600          # forgot-password links: 1 hour
_INVITE_TTL = 7 * 86400     # new-user setup links: 7 days

def _pw_token_bind(pw_hash: str) -> str:
    return hashlib.sha256((pw_hash or "").encode()).hexdigest()[:16]

def make_password_token(team: str, username: str, purpose: str, pw_hash: str, ttl: int) -> str:
    expiry  = int(time.time()) + ttl
    payload = f"{purpose}:{team}:{username}:{expiry}:{_pw_token_bind(pw_hash)}"
    sig     = _sign(payload)
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()

def decode_password_token(token: str) -> dict:
    """Verify signature + expiry. Returns {purpose, team, username, bind}.
    Raises HTTP 400 on any problem (single generic message — no detail leak)."""
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        payload, sig = raw.rsplit(":", 1)
        if not hmac.compare_digest(_sign(payload), sig):
            raise ValueError("bad signature")
        purpose, team, username, expiry_str, bind = payload.split(":")
        if int(expiry_str) < int(time.time()):
            raise ValueError("expired")
        return {"purpose": purpose, "team": team, "username": username, "bind": bind}
    except Exception:
        raise HTTPException(400, "This link is invalid or has expired.")

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
_FTS_ENABLED = True   # FTS5 full-text search; flipped off if the fts5 module is absent

# ── Full-text search (FTS5) helpers — defined early so init_team_db (run at boot) can use them ──
def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "")

def _ensure_fts(c):
    """Create the FTS5 index table; flip _FTS_ENABLED off if fts5 isn't available."""
    global _FTS_ENABLED
    if not _FTS_ENABLED:
        return
    try:
        c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS projects_fts "
                  "USING fts5(item_key, name, description)")
    except Exception as e:
        _FTS_ENABLED = False
        log.warning(f"[Search] FTS5 unavailable — falling back to LIKE search: {e}")

def _fts_sync(c, pid: int, data: dict):
    """Keep the FTS row (rowid = item id) in step with the item's text fields."""
    if not _FTS_ENABLED:
        return
    try:
        c.execute("DELETE FROM projects_fts WHERE rowid=?", (pid,))
        c.execute("INSERT INTO projects_fts(rowid, item_key, name, description) VALUES(?,?,?,?)",
                  (pid, data.get("itemKey") or "", data.get("name") or "",
                   _strip_tags(data.get("description") or "")))
    except Exception:
        pass  # never let search-index upkeep block a write

def _fts_delete(c, pid: int):
    if not _FTS_ENABLED:
        return
    try:
        c.execute("DELETE FROM projects_fts WHERE rowid=?", (pid,))
    except Exception:
        pass

def _fts_match(q: str):
    """Build a safe FTS5 MATCH string: alnum terms only, prefix-matched, AND-ed."""
    terms = [re.sub(r"[^0-9A-Za-z]", "", t) for t in (q or "").split()]
    terms = [t for t in terms if t]
    return " ".join(f"{t}*" for t in terms) or None

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
        CREATE TABLE IF NOT EXISTS key_counters (
            prefix          TEXT PRIMARY KEY,
            seq             INTEGER NOT NULL DEFAULT 0
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
        -- Stage 3b: per-user notification inbox (private; internal/in-app only)
        CREATE TABLE IF NOT EXISTS notifications (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT NOT NULL,                 -- recipient
            type        TEXT NOT NULL,                 -- mention|watch_status|watch_comment|assigned
            item_id     INTEGER,
            item_name   TEXT,
            message     TEXT NOT NULL DEFAULT '',
            actor       TEXT,
            created_ts  TEXT NOT NULL,
            read        INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(username, read);
        -- Stage 3b: item watchers (kept OUT of the item JSON blob, which update_project
        -- fully replaces from the client body — a blob field would be wiped on next save)
        CREATE TABLE IF NOT EXISTS watchers (
            item_id     INTEGER NOT NULL,
            username    TEXT NOT NULL,
            PRIMARY KEY(item_id, username)
        );
        -- Stage 6: per-user "viewed" trail (feeds the beta My Home → Recent merge).
        -- One row per (user, item); the timestamp is upserted on each open.
        CREATE TABLE IF NOT EXISTS recent_views (
            username    TEXT NOT NULL,
            item_id     INTEGER NOT NULL,
            viewed_ts   TEXT NOT NULL,
            PRIMARY KEY(username, item_id)
        );
        CREATE INDEX IF NOT EXISTS idx_recent_user ON recent_views(username, viewed_ts);
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
        # Stage 4: single-level comment threads. parent_id NULL = top-level; a reply
        # carries its (root) parent's id. Existing comments stay top-level.
        try:
            c.execute("ALTER TABLE comments ADD COLUMN parent_id INTEGER")
        except Exception:
            pass  # column already exists
        # ── Phase 1 (JIRA-REPLACEMENT.md): indexed columns mirrored from item JSON ──
        for _col, _defn in [
            ("item_key", "TEXT"), ("type", "TEXT"), ("status", "TEXT"),
            ("parent_id", "INTEGER"), ("product", "TEXT"), ("owner", "TEXT"),
            ("assignee", "TEXT"), ("reporter", "TEXT"), ("priority", "TEXT"),
            ("rank", "TEXT"), ("story_points", "REAL"), ("sprint_id", "TEXT"),
            ("archived", "INTEGER NOT NULL DEFAULT 0"), ("updated_ts", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE projects ADD COLUMN {_col} {_defn}")
            except Exception:
                pass  # column already exists
        for _idx, _expr in [
            ("idx_projects_parent",   "parent_id"), ("idx_projects_status",  "status"),
            ("idx_projects_product",  "product"),   ("idx_projects_owner",   "owner"),
            ("idx_projects_sprint",   "sprint_id"), ("idx_projects_archived","archived"),
        ]:
            c.execute(f"CREATE INDEX IF NOT EXISTS {_idx} ON projects({_expr})")
        # Partial unique index: keys are unique when present, many NULLs allowed.
        try:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_key "
                      "ON projects(item_key) WHERE item_key IS NOT NULL")
        except Exception:
            pass
        _ensure_fts(c)   # FTS5 search index (no-op if fts5 module unavailable)
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
            "departments":  [],
            "products":     [{"name":"Fraznet","builtin":True},
                             {"name":"HubSpot","builtin":True}],
            "users":        [{"username":"admin","password":_get_init_password(team),
                              "builtin":True,"role":"admin","mustChangePassword":True}],
            "types":        [{"name":"Feature","color":""},{"name":"Enhancement","color":""},{"name":"Maintenance","color":""}],
            # Per-type "Scheduled" flag (appears on Gantt + consumes capacity). Default
            # every type ON so behavior is unchanged until an admin unchecks one.
            "typeScheduled": {"Feature":True,"Enhancement":True,"Maintenance":True},
            # Status-flow anchor for the default set: drag-order is the rank; "Released" is
            # the terminal (done) status. No readiness-floor seed — the default set has no
            # "ready" gate; admins flag one in Admin → Statuses if wanted. Admin-editable.
            "statusIsTerminal": {"Released": True},
            # /beta rich-text editor (Tiptap) master switch. Default ON for the beta
            # surface; flipping to False reverts Description+Comments to the classic
            # lightweight editor with no redeploy. Classic root never reads this.
            "richTextEditor": True,
        }
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO config(key,value) VALUES(?,?)",
                      (k, json.dumps(v)))
    _migrate_passwords(team)
    _migrate_config_keys(team)
    # Backfill indexed columns in its OWN transaction (after the schema migration
    # has committed the new columns). Keeping it separate means a rollback in the
    # schema block — or a concurrent worker boot — can't lose the backfill. It is
    # self-limiting (only rows with updated_ts IS NULL) and idempotent.
    try:
        with db(team) as c:
            _unindexed = c.execute("SELECT id, data FROM projects WHERE updated_ts IS NULL").fetchall()
            for _r in _unindexed:
                try:
                    _reindex_project(c, _r["id"], json.loads(_r["data"]))
                except Exception:
                    pass
            # (Re)build the FTS index if it's out of sync (first create / drift).
            if _FTS_ENABLED:
                try:
                    fts_n  = c.execute("SELECT count(*) FROM projects_fts").fetchone()[0]
                    proj_n = c.execute("SELECT count(*) FROM projects").fetchone()[0]
                    if fts_n != proj_n:
                        c.execute("DELETE FROM projects_fts")
                        for _r in c.execute("SELECT id, data FROM projects").fetchall():
                            _fts_sync(c, _r["id"], json.loads(_r["data"]))
                        print(f"[Search] Built FTS index ({proj_n} items) for team '{team}'")
                except Exception as e:
                    log.warning(f"[Search] FTS backfill failed for '{team}': {e}")
        if _unindexed:
            print(f"[Migration] Indexed {len(_unindexed)} item(s) for team '{team}'")
    except Exception as e:
        log.warning(f"[Migration] index backfill failed for '{team}': {e}")
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
        "departments":   [],
        "statusIsDefault":  {},
        "statusIsDeferred": {},
        "jiraSyncConfig":   {"enabled": False, "intervalMinutes": 30},
        "jiraEnabled":      True,
        "statusIsReleased": {},
        "statusIsApproved": {},
        "statusIsTesting":  {},
        "statusIsBlocked":  {},
        "richTextEditor":   True,
    }
    # Keys where False/0/empty-string is a valid intentional value — only seed if key is MISSING,
    # never overwrite an existing value even if it's falsy
    presence_only_keys = {"jiraEnabled", "jiraSyncConfig", "richTextEditor"}

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
        # typeScheduled: per-type "appears on Gantt + consumes capacity" flag. Seed
        # to {every existing type: True} so behavior is unchanged until an admin
        # unchecks one. Seed only when MISSING — never overwrite admin choices.
        # (Types absent from the map still read as scheduled via isScheduledType,
        # so types added later default to scheduled too.)
        if "typeScheduled" not in existing:
            _types = existing.get("types") or []
            seeded = {}
            for _t in _types:
                _nm = _t.get("name") if isinstance(_t, dict) else _t
                if _nm:
                    seeded[_nm] = True
            c.execute(
                "INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING",
                ("typeScheduled", json.dumps(seeded))
            )
            print(f"[Migration] Seeded config key 'typeScheduled' for team '{team}'")
        # Status-flow anchors: the /beta flow rank reads the drag-ordered `statuses`, but
        # terminal (done) + readiness-floor (Approved) come from these flags. Seed sensible
        # defaults ONLY when unset AND the matching default-named status exists — never
        # clobber a team that already configured (or uses custom names). Teams with custom
        # names set these via Admin → Statuses; the beta helper falls back meanwhile.
        _sts = existing.get("statuses") or []
        for _flag, _name in (("statusIsTerminal", "Released"), ("statusIsApproved", "Approved")):
            if _name in _sts and not existing.get(_flag):
                c.execute(
                    "INSERT INTO config(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (_flag, json.dumps({_name: True}))
                )
                print(f"[Migration] Seeded status-flow flag '{_flag}' for team '{team}'")
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
    # NOTE: item-key backfill runs in a separate pass (see _backfill_all_teams_keys
    # below) — boot() executes at import, too early to reference that helper here.

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
APP_VERSION = "4.9.1"

app = FastAPI(title="Frazil Flow", version=APP_VERSION)

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
    return {"server": APP_VERSION, "name": "Frazil Flow"}
def _configure_cors(app, allowed_origins):
    """Enable CORS only when an explicit allowlist is configured.

    Deliberately does NOT fall back to '*': with allow_credentials=True,
    Starlette would reflect any request Origin and return
    Allow-Credentials: true — effectively opening the API to every site. With
    no allowlist we add no CORS grants at all (same-origin requests, which is
    how the app actually runs, are unaffected). Credentials are only enabled
    alongside a concrete allowlist.
    """
    if allowed_origins:
        app.add_middleware(CORSMiddleware,
                           allow_origins=allowed_origins,
                           allow_methods=["*"], allow_headers=["*"],
                           allow_credentials=True)
        log.info("[CORS] Restricted to: %s", ", ".join(allowed_origins))
    else:
        log.info("[CORS] No CORS_ORIGINS set — cross-origin disabled (same-origin only)")

_allowed_origins = os.environ.get("CORS_ORIGINS", "").split(",")
_allowed_origins = [o.strip() for o in _allowed_origins if o.strip()]
_configure_cors(app, _allowed_origins)

@app.get("/", response_class=HTMLResponse)
def root():
    if not os.path.exists(HTML):
        raise HTTPException(404, "roadmap.html not found next to server.py")
    with open(HTML, encoding="utf-8") as f:
        return f.read()

# ── Beta shell (/beta) ────────────────────────────────────────────────────────
# Additive: serves the SAME roadmap.html so the /beta left-rail shell can run
# alongside production. The shell is a route-gated module inside roadmap.html that
# is a no-op unless location.pathname starts with "/beta". Production "/" above is
# untouched. The catch-all carries /beta/gantt, /beta/list, /beta/item/123, etc.
@app.get("/beta")
@app.get("/beta/{subpath:path}")
def beta_redirect(request: FRequest, subpath: str = ""):
    # Phase 3: Flow lives at root now. Legacy /beta/* → 301 (permanent) to the same path
    # at root, query preserved. Was 302 during rollout; flipped to 301 once proven (root
    # is the permanent home) so clients/crawlers cache the redirect and canonicalize to root.
    target = "/" + subpath
    q = request.url.query
    if q:
        target += "?" + q
    return RedirectResponse(url=target, status_code=301)

# ── PWA: manifest, service worker, icons ──────────────────────────────────────
# Served as routes (not static files) so the deploy stays a two-file scp. Icon
# bytes are base64-embedded below; regenerate with `python tools/gen_pwa_icons.py`
# (dev-only — Pillow is NOT a runtime dependency, the bytes are baked in here).
# Favicon: the Frazil "f" mark (64x64 PNG), base64-embedded like the PWA icons
# above so the deploy stays a two-file scp. Source: f-logo.png.
_FAVICON_B64        = "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"
_PWA_ICON_192_B64   = "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"  # GENERATED by tools/gen_pwa_icons.py — do not hand-edit
_PWA_ICON_512_B64   = "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"  # GENERATED by tools/gen_pwa_icons.py — do not hand-edit
_PWA_ICON_APPLE_B64 = "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"  # GENERATED by tools/gen_pwa_icons.py — do not hand-edit

_PWA_MANIFEST = {
    "name": "Frazil Flow",
    "short_name": "Flow",
    "description": "Team planning — Gantt, Kanban, list & sprints",
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

@app.get("/favicon.png")
def favicon_png():
    return _pwa_png(_FAVICON_B64)

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

    # Match by username (exact) or email (case-insensitive). Email is unique per
    # team (enforced on save), so the email match is unambiguous.
    _ident_l = username.lower()
    user = next((u for u in users
                 if u["username"] == username
                 or ((u.get("email") or "").strip().lower() == _ident_l)), None)
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


# ── Self-service password reset / invite (Amazon SES) ─────────────────────────
@app.post("/api/forgot-password")
def forgot_password(body: dict = Body(...), request: FRequest = None):
    """Public. Emails a reset link if the (team, email) matches an active user.
    Always returns the same response — never reveals whether an account exists."""
    ip = (request.client.host if request else "unknown")
    _check_rate_limit(ip)
    resp = {"ok": True, "message": "If an account with that email exists, a reset link has been sent."}

    team  = re.sub(r"[^a-z0-9]", "", (body.get("team") or "").strip().lower())
    email = (body.get("email") or "").strip().lower()
    if not team or not valid_team(team) or not email:
        return resp

    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
    users = json.loads(row["value"]) if row else []
    user = next((u for u in users
                 if (u.get("email") or "").strip().lower() == email and not u.get("revokedAt")), None)

    if user and mail_configured():
        try:
            token = make_password_token(team, user["username"], "reset", user.get("password", ""), _RESET_TTL)
            link  = f"{APP_BASE_URL}/?pwtoken={token}"
            send_email(
                user["email"],
                "Reset your Frazil Flow password",
                f"Hi {user['username']},\n\nReset your Frazil Flow password using the link below "
                f"(valid for 1 hour):\n\n{link}\n\nIf you didn't request this, you can ignore this email.",
                f"<p>Hi {html.escape(user['username'])},</p>"
                f"<p><a href=\"{link}\">Reset your Frazil Flow password</a> (valid for 1 hour).</p>"
                f"<p>If you didn't request this, you can ignore this email.</p>",
            )
            write_audit(team, "password:forgot", user["username"])
        except Exception as e:
            log.warning(f"[Email] forgot-password send failed: {e}")
    return resp


@app.post("/api/reset-password")
def reset_password_with_token(body: dict = Body(...)):
    """Public. Set a new password using a signed reset/invite link token."""
    token  = body.get("token", "")
    new_pw = body.get("password", "")
    if not new_pw or len(new_pw) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    decoded = decode_password_token(token)
    team, uname = decoded["team"], decoded["username"]
    if not valid_team(team):
        raise HTTPException(400, "This link is invalid or has expired.")

    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
        users = json.loads(row["value"]) if row else []
        user = next((u for u in users if u["username"] == uname), None)
        if not user:
            raise HTTPException(400, "This link is invalid or has expired.")
        # Single-use: the token's bind must match the CURRENT password hash.
        if _pw_token_bind(user.get("password", "")) != decoded["bind"]:
            raise HTTPException(400, "This link has already been used or is no longer valid.")
        user["password"] = hash_password(new_pw)
        user["mustChangePassword"] = False
        c.execute("UPDATE config SET value=? WHERE key='users'", (json.dumps(users),))
    write_audit(team, "password:reset_complete", uname)
    return {"ok": True, "team": team, "username": uname}


@app.post("/api/users/{target_username}/send-invite")
def send_user_invite(target_username: str, auth: dict = Depends(require_role("admin"))):
    """Admin. Email a new user a 'set your password' link (valid 7 days)."""
    team = auth["team"]
    if not mail_configured():
        raise HTTPException(503, "Email is not configured on the server (set the SES_SMTP_* env vars).")
    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
    users = json.loads(row["value"]) if row else []
    user = next((u for u in users if u["username"] == target_username), None)
    if not user:
        raise HTTPException(404, f"User '{target_username}' not found")
    email = (user.get("email") or "").strip()
    if not email:
        raise HTTPException(400, f"User '{target_username}' has no email address set")

    token = make_password_token(team, target_username, "invite", user.get("password", ""), _INVITE_TTL)
    link  = f"{APP_BASE_URL}/?pwtoken={token}"
    try:
        send_email(
            email,
            "Set up your Frazil Flow account",
            f"Hi {target_username},\n\nAn account has been created for you on Frazil Flow (team: {team}). "
            f"Set your password using the link below (valid for 7 days):\n\n{link}",
            f"<p>Hi {html.escape(target_username)},</p>"
            f"<p>An account has been created for you on Frazil Flow (team: <b>{html.escape(team)}</b>). "
            f"<a href=\"{link}\">Set your password</a> (valid for 7 days).</p>",
        )
    except Exception as e:
        log.warning(f"[Email] invite send failed for {target_username}: {e}")
        raise HTTPException(502, f"Could not send email: {e}")
    write_audit(team, "user:invite", auth["username"], changes={"target": target_username})
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
                   "email": u.get("email", ""),
                   "revokedAt": u.get("revokedAt")} for u in users_raw]
    return {"projects": projects, "developers": cfg("developers"),
            "statuses": cfg("statuses"), "delayReasons": cfg("delayReasons"),
            "products": cfg("products"), "users": users_safe,
            "types": cfg("types"),
            "ownerCapacity": cfg("ownerCapacity") or {},
            "statusIgnoreConflicts": cfg("statusIgnoreConflicts") or {},
            "typeIgnoreConflicts": cfg("typeIgnoreConflicts") or {},
            "typeScheduled": cfg("typeScheduled") or {},
            "productIgnoreConflicts": cfg("productIgnoreConflicts") or {},
            "statusIsActive": cfg("statusIsActive") or {},
            "statusIsTerminal": cfg("statusIsTerminal") or {},
            "statusIsDefault": cfg("statusIsDefault") or {},
            "statusIsDeferred": cfg("statusIsDeferred") or {},
            "statusIsReleased": cfg("statusIsReleased") or {},
            "statusIsApproved": cfg("statusIsApproved") or {},
            "statusIsTesting": cfg("statusIsTesting") or {},
            "statusIsBlocked": cfg("statusIsBlocked") or {},
            "changeReasons": cfg("changeReasons") or [],
            "deferReasons": cfg("deferReasons") or [],
            "departments": cfg("departments") or [],
            "jiraProjectMapping": cfg("jiraProjectMapping") or {},
            "jiraStatusMapping": cfg("jiraStatusMapping") or {},
            "jiraTypeMapping": cfg("jiraTypeMapping") or {},
            "jiraSyncConfig": cfg("jiraSyncConfig") or {},
            # /beta rich-text editor master switch — boolean preserved (default ON when
            # absent) so an admin's explicit False reaches the client and reverts the editor.
            "richTextEditor": cfg_map.get("richTextEditor", True)}


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
# Phase 1 (JIRA-REPLACEMENT.md): items carry mirrored, indexed columns alongside
# the JSON blob so we can query/filter/paginate without loading the whole team
# into memory. The blob stays the source of truth; these helpers keep the columns
# in sync on every write. NB: the blob's owner field is historically named `dev`.
def _project_index_cols(data: dict) -> dict:
    def _s(v):     return str(v) if v not in (None, "") else None
    def _int(v):
        try:    return int(v) if v not in (None, "", False) else None
        except (TypeError, ValueError): return None
    def _float(v):
        try:    return float(v) if v not in (None, "") else None
        except (TypeError, ValueError): return None
    return {
        "item_key":     _s(data.get("itemKey")),
        "type":         _s(data.get("type")),
        "status":       _s(data.get("status")),
        "parent_id":    _int(data.get("parent")),
        "product":      _s(data.get("product")),
        "owner":        _s(data.get("dev")),          # blob field is `dev`
        "assignee":     _s(data.get("assignee")),     # new in later phases
        "reporter":     _s(data.get("reporter")),     # new in later phases
        "priority":     _s(data.get("priority")),
        "rank":         _s(data.get("rank")),          # new in later phases
        "story_points": _float(data.get("storyPoints")),
        "sprint_id":    _s(data.get("sprintId")),
        "archived":     1 if data.get("archived") else 0,
    }

def _reindex_project(c, pid: int, data: dict, ts: str = None):
    """Mirror an item's blob fields into its indexed columns (+ updated_ts) and FTS."""
    cols = _project_index_cols(data)
    cols["updated_ts"] = ts or datetime.now(timezone.utc).isoformat()
    assignments = ", ".join(f"{k}=?" for k in cols)
    c.execute(f"UPDATE projects SET {assignments} WHERE id=?", (*cols.values(), pid))
    _fts_sync(c, pid, data)

def _insert_project(c, data: dict, ts: str = None) -> int:
    """INSERT an item and populate its indexed columns. Returns the new id."""
    cur = c.execute("INSERT INTO projects(data) VALUES(?)", (json.dumps(data),))
    pid = cur.lastrowid
    _reindex_project(c, pid, data, ts)
    return pid

def _save_project(c, pid: int, data: dict, ts: str = None):
    """UPDATE an item's blob AND its indexed columns together (no drift)."""
    c.execute("UPDATE projects SET data=? WHERE id=?", (json.dumps(data), pid))
    _reindex_project(c, pid, data, ts)

# ── Human-readable item keys (Phase 1b, JIRA-REPLACEMENT.md §4) ───────────────
# Key = {PREFIX}-{n}, prefix configured per product in Admin → Projects. Counter
# is per-prefix and atomic within the transaction. Keys are immutable once set.
def _next_key_seq(c, prefix: str) -> int:
    c.execute("INSERT INTO key_counters(prefix, seq) VALUES(?, 0) ON CONFLICT(prefix) DO NOTHING", (prefix,))
    c.execute("UPDATE key_counters SET seq = seq + 1 WHERE prefix=?", (prefix,))
    return c.execute("SELECT seq FROM key_counters WHERE prefix=?", (prefix,)).fetchone()[0]

def _product_prefix(c, product_name: str):
    if not product_name:
        return None
    row = c.execute("SELECT value FROM config WHERE key='products'").fetchone()
    for p in (json.loads(row["value"]) if row else []):
        if isinstance(p, dict) and p.get("name") == product_name:
            return (p.get("keyPrefix") or "").strip() or None
    return None

# Items whose product has no keyPrefix (or no product) still get a key under
# this default prefix, so every item is addressable. Shares a per-prefix counter.
DEFAULT_KEY_PREFIX = "FRAZ"

def _assign_item_key(c, data: dict):
    """Give the item a {PREFIX}-{n} key if it has none yet — the product's prefix
    when set, else DEFAULT_KEY_PREFIX. Immutable once set."""
    if data.get("itemKey"):
        return
    prefix = _product_prefix(c, data.get("product")) or DEFAULT_KEY_PREFIX
    data["itemKey"] = f"{prefix}-{_next_key_seq(c, prefix)}"

def _backfill_item_keys(team: str) -> int:
    """Assign a key to EVERY keyless item — its product's prefix when set, else
    DEFAULT_KEY_PREFIX. Idempotent; run on boot and after a products config save."""
    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='products'").fetchone()
        prefixes = {p["name"]: (p.get("keyPrefix") or "").strip()
                    for p in (json.loads(row["value"]) if row else [])
                    if isinstance(p, dict) and (p.get("keyPrefix") or "").strip()}
        rows = c.execute(
            "SELECT id, data FROM projects WHERE item_key IS NULL OR item_key='' ORDER BY id"
        ).fetchall()
        assigned = 0
        for r in rows:
            data = json.loads(r["data"])
            if data.get("itemKey"):
                continue
            prefix = prefixes.get(data.get("product")) or DEFAULT_KEY_PREFIX
            data["itemKey"] = f"{prefix}-{_next_key_seq(c, prefix)}"
            _save_project(c, r["id"], data)
            assigned += 1
    return assigned

# One-time boot backfill for ALL existing teams — runs HERE (at import, after the
# helper is defined; boot() above runs too early). Idempotent: only keyless items
# get a key (product prefix when set, else DEFAULT_KEY_PREFIX).
def _backfill_all_teams_keys():
    import os as _os
    if not os.path.isdir(TENANTS_DIR):
        return
    for _entry in _os.listdir(TENANTS_DIR):
        _tpath = _os.path.join(TENANTS_DIR, _entry)
        if _os.path.isdir(_tpath) and _os.path.exists(_os.path.join(_tpath, "roadmap.db")):
            try:
                _backfill_item_keys(_entry)
            except Exception as e:
                print(f"[ItemKeys] boot backfill failed for {_entry}: {e}")
_backfill_all_teams_keys()

# ── Departments (multi-value free-text field + shared master list) ────────────
def _normalize_departments(arr):
    """Trim, drop empties, case-insensitive dedup preserving first-seen casing."""
    out, seen = [], set()
    for d in (arr or []):
        if not isinstance(d, str):
            continue
        d = d.strip()
        if not d:
            continue
        k = d.casefold()
        if k in seen:
            continue
        seen.add(k); out.append(d)
    return out

def _union_departments(team, item_depts):
    """Union an item's departments into the shared `departments` config list
    (case-insensitive, first-seen casing). Lets editors create departments by
    typing them on a ticket — same key the admin config path would write."""
    item_depts = _normalize_departments(item_depts)
    if not item_depts:
        return
    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='departments'").fetchone()
        cur = json.loads(row["value"]) if row else []
        if not isinstance(cur, list):
            cur = []
        seen = {str(d).casefold() for d in cur if isinstance(d, str)}
        changed = False
        for d in item_depts:
            if d.casefold() not in seen:
                cur.append(d); seen.add(d.casefold()); changed = True
        if changed:
            c.execute("INSERT INTO config(key,value) VALUES('departments',?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(cur),))

@app.post("/api/projects")
def create_project(body: dict, auth: dict = Depends(require_role("admin", "editor"))):
    team = auth["team"]
    username = body.pop("_username", auth["username"])
    body.pop("id", None)
    body.setdefault("reporter", username)   # who created the item (immutable-ish)
    # Server-side rounding of parallelResources — must happen BEFORE the insert
    # so the persisted value matches what we return (not just the response).
    if "parallelResources" in body:
        body["parallelResources"] = round_up_to_quarter(body["parallelResources"])
    if "departments" in body:
        body["departments"] = _normalize_departments(body.get("departments"))
    with db(team) as c:
        _assign_item_key(c, body)
        body["id"] = _insert_project(c, body)
    write_audit(team, "create", username, body["id"], body.get("name",""))
    try:
        _union_departments(team, body.get("departments"))   # persist any new departments (best-effort)
    except Exception as e:
        log.warning(f"[Departments] union after create failed for item {body['id']}: {e}")
    # Stage 3b: notifications (post-commit, best-effort — never fail the create)
    try:
        _add_watchers(team, body["id"], [username, body.get("assignee")])
        if body.get("assignee") and body.get("assignee") != username:
            _notify(team, [body["assignee"]], "assigned", body["id"], body.get("name",""),
                    f"{username} assigned you {body.get('name','an item')}", username)
    except Exception as e:
        log.warning(f"[Notify] create hook failed for item {body['id']}: {e}")
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

    # One connection: read the item, read the status-flag config we need, validate,
    # then write. (Previously statusIsReleased was read in a second connection.)
    with db(team) as c:
        row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
        if not row: raise HTTPException(404, "Not found")
        old = json.loads(row["data"])

        cfg = {r["key"]: json.loads(r["value"]) for r in c.execute(
            "SELECT key, value FROM config WHERE key IN ('statusIsActive','statusIsReleased','statusIsBlocked')"
        ).fetchall()}
        active_map   = cfg.get("statusIsActive", {})
        released_map = cfg.get("statusIsReleased", {})
        blocked_map  = cfg.get("statusIsBlocked", {})
        blocked_status = next((k for k, v in (blocked_map or {}).items() if v), "")

        # Validate: parallelResources cannot be changed while item is in an active status
        current_status = old.get("status", "")
        if active_map.get(current_status):
            old_pr = float(old.get("parallelResources") or 1)
            new_pr = float(body.get("parallelResources") or 1)
            if abs(old_pr - new_pr) > 0.001:
                raise HTTPException(422, f"Parallel Resources cannot be changed while item is active (status: {current_status!r})")

        changes = {k: {"from": old.get(k), "to": v}
                   for k, v in body.items()
                   if old.get(k) != v and k not in {"jiraTickets","description"}}
        # Attachments live in the blob but are managed ONLY by the attachment
        # endpoints — a wholesale item PUT carries the client's (often stale)
        # copy, so force the server-authoritative value through (mirror of the
        # watchers table rationale). Prevents a full PUT from wiping an
        # attachment that was just added in the same session.
        old_atts = old.get("attachments")
        if old_atts is not None:
            body["attachments"] = old_atts
        else:
            body.pop("attachments", None)
        if "departments" in body:
            body["departments"] = _normalize_departments(body.get("departments"))
        # Blocked binding (additive): leaving the Blocked status clears the stashed
        # pre-block status here; the open Blocked flag is auto-cleared post-commit below.
        if blocked_status and body.get("status", "") != blocked_status:
            body.pop("preBlockStatus", None)
        _save_project(c, pid, body)
    write_audit(team, "update", username, pid, body.get("name",""), changes or None)
    try:
        _union_departments(team, body.get("departments"))   # persist any new departments (best-effort)
    except Exception as e:
        log.warning(f"[Departments] union after update failed for item {pid}: {e}")
    body["id"] = pid
    # Stage 3b: notifications (post-commit, best-effort — never fail/roll back the update)
    try:
        _notify_item_update(team, pid, old, body, username)
    except Exception as e:
        log.warning(f"[Notify] update hook failed for item {pid}: {e}")

    # ── FF pull: when item moves to the Released status and has Jira tickets ──────
    # Best-effort and isolated from the main update. The Jira hierarchy walk is a
    # network call, so it runs OUTSIDE any transaction; the resulting write then
    # re-reads the current row and merges ONLY the feature-flag fields, so a
    # concurrent edit landing during the Jira call isn't clobbered.
    new_status = body.get("status", "")
    old_status = old.get("status", "")
    # ── Blocked binding (post-commit, best-effort): when an item moves to ANY
    # non-Blocked status, auto-clear its open Blocked flag so status ⇔ flag stay
    # consistent. No-op unless a Blocked status is configured. Never fails the update. ──
    if blocked_status and new_status != blocked_status:
        try:
            _ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            with db(team) as c:
                c.execute(
                    "UPDATE activities SET status='Auto-Cleared', resolved_by=?, resolved_ts=?, "
                    "action_taken=? WHERE item_id=? AND activity_type='Blocked' AND status IN ('Open','Read')",
                    (username or "System", _ts, "Auto-cleared: item moved out of the Blocked status", pid)
                )
        except Exception as e:
            log.warning(f"[Blocked] flag-clear after update failed for item {pid}: {e}")
    tickets    = body.get("jiraTickets") or []
    if new_status != old_status and released_map.get(new_status) and tickets and jira_configured():
        try:
            all_ff: set = set()
            for ticket in tickets[:10]:
                all_ff.update(_fetch_jira_feature_flags(ticket))
            if all_ff:
                with db(team) as c:
                    cur = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
                    if cur:
                        current = json.loads(cur["data"])
                        current["jiraFeatureFlags"] = sorted(all_ff)
                        # Merge into featureFlags (union of existing + Jira, deduped)
                        current["featureFlags"] = sorted(set(current.get("featureFlags") or []) | all_ff)
                        _save_project(c, pid, current)
                        body["jiraFeatureFlags"] = current["jiraFeatureFlags"]
                        body["featureFlags"]     = current["featureFlags"]
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
        _fts_delete(c, pid)
    write_audit(team, "delete", username, pid, name)
    return {"ok": True}

# Sortable columns for /api/items (whitelist — never interpolate raw user input).
_ITEMS_SORTABLE = {"updated_ts", "item_key", "status", "type", "product",
                   "owner", "assignee", "priority", "story_points", "sprint_id", "id"}

@app.get("/api/items")
def list_items(
    auth: dict = Depends(require_auth),
    product: Optional[str] = None,
    type: Optional[str] = None,
    status: Optional[str] = None,
    owner: Optional[str] = None,
    assignee: Optional[str] = None,
    sprint: Optional[str] = None,
    parent_id: Optional[str] = None,
    archived: Optional[str] = None,
    q: Optional[str] = None,
    sort: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    counts: Optional[str] = None,
):
    """Server-side query/search/paginate over the indexed item columns
    (JIRA-REPLACEMENT.md §3.2). Returns a page of full item blobs + total count.
    With counts=1, each item gets a `_childCount` (non-archived children) so the
    tree view knows which rows are expandable. Read-only; any role may call it."""
    team = auth["team"]
    init_team_db(team)

    where, params = [], []
    def eq(col, val):
        # A value may be a single term or a comma-separated list (multi-select
        # filters from the top bar). Single → `col=?`; multiple → `col IN (...)`.
        if val is not None and val != "":
            vals = [v for v in val.split(",") if v != ""]
            if len(vals) == 1:
                where.append(f"{col}=?"); params.append(vals[0])
            elif len(vals) > 1:
                where.append(f"{col} IN ({','.join('?' * len(vals))})")
                params.extend(vals)
    eq("product", product)
    eq("type", type)
    eq("status", status)
    eq("owner", owner)
    eq("assignee", assignee)
    eq("sprint_id", sprint)

    # parent_id: an integer, or 'none'/'null' for top-level items.
    if parent_id not in (None, ""):
        if parent_id.lower() in ("none", "null"):
            where.append("parent_id IS NULL")
        else:
            try:
                params.append(int(parent_id)); where.append("parent_id=?")
            except ValueError:
                raise HTTPException(400, "parent_id must be an integer or 'none'")

    # archived: default hides archived; 'all' = both; '1'/'true' = archived only.
    a = (archived or "").lower()
    if a in ("1", "true", "yes"):
        where.append("archived=1")
    elif a != "all":
        where.append("archived=0")

    # Free-text search: FTS5 (relevance-ranked) when available, else scoped LIKE.
    use_fts   = bool(q) and _FTS_ENABLED
    fts_match = _fts_match(q) if use_fts else None
    if use_fts and not fts_match:
        use_fts = False
    if q and not use_fts:
        # Fallback: scoped, multi-term LIKE over key/name/description (AND-ed).
        for term in q.split():
            where.append("(item_key LIKE ? OR json_extract(data,'$.name') LIKE ? "
                         "OR json_extract(data,'$.description') LIKE ?)")
            t = f"%{term}%"
            params += [t, t, t]

    sort_col, _, sort_dir = (sort or "updated_ts:desc").partition(":")
    sort_dir = "ASC" if sort_dir.lower() == "asc" else "DESC"
    # `name` lives in the JSON blob (no indexed column) — sort it via json_extract,
    # case-insensitively. All other sorts must be whitelisted indexed columns.
    if sort_col == "name":
        sort_expr = "json_extract(projects.data,'$.name') COLLATE NOCASE"
    else:
        if sort_col not in _ITEMS_SORTABLE:
            sort_col = "updated_ts"
        sort_expr = f"projects.{sort_col}"

    page = max(1, page)
    page_size = max(1, min(500, page_size))
    offset = (page - 1) * page_size

    join  = ""
    if sort_col == "priority":
        # One priority order everywhere: Urgent(1) → High(2) → Medium(3) → Low(4),
        # with blank/none (NULL or <1) ALWAYS last regardless of direction.
        order = f"(projects.priority IS NULL OR projects.priority < 1) ASC, projects.priority {sort_dir}, projects.id DESC"
    elif sort_col == "item_key":
        # Natural "{PREFIX}-{N}" order, computed over the FULL set (before LIMIT/OFFSET so
        # it's correct across pagination): blanks (NULL/'') ALWAYS last in both directions;
        # then the text prefix case-insensitively; then the trailing integer NUMERICALLY.
        # rtrim(key,'0..9') strips trailing digits to isolate the prefix; CAST of the
        # remaining digits → 0 for keys that don't end in digits (graceful fallback, never
        # crashes). All literal SQL — sort_col is whitelisted, sort_dir is ASC/DESC.
        _blank  = "(projects.item_key IS NULL OR projects.item_key = '')"
        _prefix = "rtrim(projects.item_key, '0123456789')"
        _num    = f"CAST(substr(projects.item_key, length({_prefix}) + 1) AS INTEGER)"
        order = f"{_blank} ASC, {_prefix} COLLATE NOCASE {sort_dir}, {_num} {sort_dir}, projects.id DESC"
    else:
        order = f"{sort_expr} {sort_dir}, projects.id DESC"
    if use_fts:
        join = "JOIN projects_fts ON projects_fts.rowid = projects.id"
        where.append("projects_fts MATCH ?")
        params.append(fts_match)
        if not sort:                       # default → rank by relevance
            order = "bm25(projects_fts), projects.id DESC"
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with db(team) as c:
        total = c.execute(f"SELECT COUNT(*) FROM projects {join} {where_sql}", params).fetchone()[0]
        rows = c.execute(
            f"SELECT projects.id AS id, projects.data AS data FROM projects {join} {where_sql} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, page_size, offset)
        ).fetchall()
        items = []
        for r in rows:
            p = json.loads(r["data"]); p["id"] = r["id"]; items.append(p)
        # Optional child counts for the tree view (one grouped query for the page).
        if counts and items:
            ids = [it["id"] for it in items]
            qmarks = ",".join("?" * len(ids))
            cc = {row["parent_id"]: row["n"] for row in c.execute(
                f"SELECT parent_id, COUNT(*) AS n FROM projects "
                f"WHERE parent_id IN ({qmarks}) AND archived=0 GROUP BY parent_id", ids).fetchall()}
            for it in items:
                it["_childCount"] = cc.get(it["id"], 0)
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "pages": (total + page_size - 1) // page_size if total else 0}

# Fields safe to set in bulk. Deliberately excludes scheduling/capacity fields
# (dueWeeks/testWeeks/parallelResources/start/due) — those have per-item rules.
_BULK_FIELDS = {"status", "dev", "assignee", "sprintId", "priority", "archived"}

@app.post("/api/items/bulk")
def bulk_update_items(body: dict = Body(...),
                      auth: dict = Depends(require_role("admin", "editor"))):
    """Apply a small patch to many items at once, in one transaction. Only the
    whitelisted fields above may be set."""
    team  = auth["team"]
    ids   = body.get("ids") or []
    patch = body.get("patch") or {}
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "ids must be a non-empty list")
    if not isinstance(patch, dict) or not patch:
        raise HTTPException(400, "patch must be a non-empty object")
    bad = set(patch) - _BULK_FIELDS
    if bad:
        raise HTTPException(400, f"Fields not allowed in bulk edit: {', '.join(sorted(bad))}")
    if "archived" in patch:
        patch["archived"] = bool(patch["archived"])

    updated = 0
    with db(team) as c:
        for raw in ids:
            try:
                pid = int(raw)
            except (TypeError, ValueError):
                continue
            row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
            if not row:
                continue
            data = json.loads(row["data"])
            data.update(patch)
            _save_project(c, pid, data)
            updated += 1
    write_audit(team, "bulk_update", auth["username"],
                changes={"count": updated, "fields": list(patch.keys())})
    return {"updated": updated, "patch": patch}

# ── Config ────────────────────────────────────────────────────────────────────
VALID_KEYS = {"developers","statuses","delayReasons","products","users","types",
              "ownerCapacity","statusIgnoreConflicts","typeIgnoreConflicts","productIgnoreConflicts",
              "typeScheduled",
              "statusIsActive","statusIsTerminal",
              "statusIsDefault","statusIsDeferred",
              "changeReasons","deferReasons","departments",
              "jiraProjectMapping","jiraStatusMapping","jiraTypeMapping",
              "jiraSyncConfig","jiraEnabled","statusIsReleased","statusIsApproved","statusIsTesting","statusIsBlocked",
              "richTextEditor"}

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
        seen_emails = {}
        for u in body:
            uname = u.get("username","")
            pw = u.get("password","")
            if not pw:
                if uname in existing and existing[uname].get("password"):
                    u["password"] = existing[uname]["password"]
            elif not is_hashed(pw):
                u["password"] = hash_password(pw)
            # Normalize + validate email; emails must be unique per team because
            # users can log in by email.
            email = (u.get("email") or "").strip()
            u["email"] = email
            if email:
                if "@" not in email or " " in email:
                    raise HTTPException(422, f"Invalid email address: '{email}'")
                key_l = email.lower()
                if key_l in seen_emails:
                    raise HTTPException(422, f"Duplicate email '{email}' — emails must be unique per team")
                seen_emails[key_l] = uname
    with db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(body)))
    write_audit(team, f"config:{key}", username, changes={"updated": key})
    # Defining/changing a product's keyPrefix backfills keys for its existing items.
    if key == "products":
        try:
            _backfill_item_keys(team)
        except Exception as e:
            log.warning(f"[ItemKeys] backfill after products save failed: {e}")
    return body

# ── Beta: Kanban Boards (shared, global per team) ─────────────────────────────
# Additive endpoints used only by the /beta shell. Boards are a shared column
# configuration for Kanban, stored in the config table under key 'boards'. Per
# spec, create/edit/delete is available to any logged-in user (require_auth).
# Production routes/behaviour are untouched — nothing in production reads these.
def _read_boards(team: str):
    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='boards'").fetchone()
    try:
        return json.loads(row["value"]) if row else []
    except Exception:
        return []

@app.get("/api/boards")
def get_boards(auth: dict = Depends(require_auth)):
    return {"boards": _read_boards(auth["team"])}

@app.put("/api/boards")
def put_boards(body = Body(...), auth: dict = Depends(require_auth)):
    team = auth["team"]
    boards = body.get("boards") if isinstance(body, dict) else body
    if not isinstance(boards, list):
        raise HTTPException(422, "boards must be a list")
    # Light validation: each board needs an id, a name, and ≥1 column with ≥1 status.
    for b in boards:
        if not isinstance(b, dict) or not b.get("id") or not (b.get("name") or "").strip():
            raise HTTPException(422, "each board needs an id and a name")
        cols = b.get("columns")
        if not isinstance(cols, list) or not cols:
            raise HTTPException(422, f"board '{b.get('name')}' needs at least one column")
        seen = set()
        for col in cols:
            sts = col.get("statuses") if isinstance(col, dict) else None
            if not isinstance(sts, list) or not sts:
                raise HTTPException(422, "each column needs at least one status")
            for s in sts:
                if s in seen:
                    raise HTTPException(422, f"status '{s}' is assigned to more than one column")
                seen.add(s)
            if col.get("dropStatus") not in sts:
                raise HTTPException(422, "a column's drop status must be one of its statuses")
    with db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES('boards',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (json.dumps(boards),))
    write_audit(team, "beta:boards", auth["username"], changes={"count": len(boards)})
    return {"boards": boards}

# ── Beta: Sprints (shared, global per team; single Active) ────────────────────
# Additive, /beta-only. Sprints are stored in the config table under key 'sprints'.
# Items reference a sprint via their existing sprintId field. Any logged-in user
# may manage sprints for now (require_auth). Production is untouched.
_SPRINT_STATES = {"Planned", "Active", "Completed"}

def _read_sprints(team: str):
    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='sprints'").fetchone()
    try:
        return json.loads(row["value"]) if row else []
    except Exception:
        return []

@app.get("/api/sprints")
def get_sprints(auth: dict = Depends(require_auth)):
    return {"sprints": _read_sprints(auth["team"])}

@app.put("/api/sprints")
def put_sprints(body = Body(...), auth: dict = Depends(require_auth)):
    team = auth["team"]
    sprints = body.get("sprints") if isinstance(body, dict) else body
    if not isinstance(sprints, list):
        raise HTTPException(422, "sprints must be a list")
    active = 0
    for s in sprints:
        if not isinstance(s, dict) or not s.get("id") or not (s.get("name") or "").strip():
            raise HTTPException(422, "each sprint needs an id and a name")
        if s.get("state") not in _SPRINT_STATES:
            raise HTTPException(422, f"invalid sprint state {s.get('state')!r}")
        if s.get("state") == "Active":
            active += 1
    if active > 1:
        raise HTTPException(422, "only one sprint may be Active at a time")
    with db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES('sprints',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (json.dumps(sprints),))
    write_audit(team, "beta:sprints", auth["username"], changes={"count": len(sprints)})
    return {"sprints": sprints}

# ── Beta: Releases (shared, global per team) ──────────────────────────────────
# Additive, /beta-only. Releases are stored in the config table under key
# 'releases'. Items reference a release via an optional `release` field. Any
# logged-in user may manage releases for now (require_auth). Production untouched.
_RELEASE_STATES = {"Unreleased", "Released"}

def _read_releases(team: str):
    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='releases'").fetchone()
    try:
        return json.loads(row["value"]) if row else []
    except Exception:
        return []

@app.get("/api/releases")
def get_releases(auth: dict = Depends(require_auth)):
    return {"releases": _read_releases(auth["team"])}

@app.put("/api/releases")
def put_releases(body = Body(...), auth: dict = Depends(require_auth)):
    team = auth["team"]
    releases = body.get("releases") if isinstance(body, dict) else body
    if not isinstance(releases, list):
        raise HTTPException(422, "releases must be a list")
    for r in releases:
        if not isinstance(r, dict) or not r.get("id") or not (r.get("name") or "").strip():
            raise HTTPException(422, "each release needs an id and a name")
        if r.get("state") not in _RELEASE_STATES:
            raise HTTPException(422, f"invalid release state {r.get('state')!r}")
    with db(team) as c:
        c.execute("INSERT INTO config(key,value) VALUES('releases',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (json.dumps(releases),))
    write_audit(team, "beta:releases", auth["username"], changes={"count": len(releases)})
    return {"releases": releases}

# ── Attachments (S3, /beta — Stage 2a) ────────────────────────────────────────
# Files live in a private S3 bucket. The browser uploads DIRECTLY to S3 with a
# short-lived presigned PUT (file never proxies through the backend); downloads
# use presigned GET. Deletes are server-side only. Credentials come from the
# EC2 instance role (same pattern as SES) — never hardcoded, never in client
# code, never logged. Attachment metadata is stored on the item JSON blob
# (p.attachments) so there is no schema migration.
import uuid as _uuid

MAX_ATTACH_BYTES = 50 * 1024 * 1024  # 50 MB, enforced server-side (refuse to sign) + client-side
ATTACH_BUCKET    = os.environ.get("ATTACH_BUCKET", "frazil-flow-attachments")
# Optional SSE-KMS key (ARN / alias / key-id). When set, presigned PUTs are
# signed to encrypt the object with this CMK and the browser replays the
# matching x-amz-server-side-encryption* headers (see presign_attachment).
# Leave unset when the bucket applies default encryption on its own.
ATTACH_KMS_KEY_ID = os.environ.get("ATTACH_KMS_KEY_ID") or None

def _s3_client():
    import boto3  # lazy — server.py still loads without boto3 in dev/test
    from botocore.config import Config
    # Force SigV4 + virtual-hosted regional addressing so presigned URLs point at
    # the bucket's real regional host (…s3.us-west-2.amazonaws.com) and carry an
    # X-Amz-Signature. The default global endpoint + SigV2 triggers a region
    # redirect that surfaces as a (misleading) CORS error in the browser.
    return boto3.client(
        "s3",
        region_name=AWS_REGION,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )

def _sanitize_filename(name: str) -> str:
    """Reduce to a safe basename of [A-Za-z0-9._-]; never empty."""
    base = (name or "file").strip().replace("\\", "/").split("/")[-1]
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    base = base.lstrip(".") or "file"   # avoid hidden/empty names
    return base[:200]

def _attachment_key(pid: int, u: str, name: str) -> str:
    """items/{itemId}/{uuid}/{sanitized-filename} — uuid is its own path segment."""
    return f"items/{pid}/{u}/{_sanitize_filename(name)}"

def _get_item_blob(c, pid: int) -> dict:
    row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
    if not row:
        raise HTTPException(404, "Item not found")
    return json.loads(row["data"])

@app.post("/api/items/{pid}/attachments/presign")
def presign_attachment(pid: int, body: dict = Body(...),
                       auth: dict = Depends(require_role("admin", "editor"))):
    """Validate + return a short-lived presigned PUT URL. Records nothing yet."""
    team = auth["team"]
    filename = body.get("filename") or "file"
    content_type = body.get("contentType") or "application/octet-stream"
    try:
        size = int(body.get("size") or 0)
    except (TypeError, ValueError):
        raise HTTPException(422, "size must be an integer")
    # Server-side size guard — refuse to sign an oversized declared upload.
    if size > MAX_ATTACH_BYTES:
        raise HTTPException(413, f"File exceeds the 50 MB limit ({size} bytes).")
    with db(team) as c:
        _get_item_blob(c, pid)  # 404 if the item doesn't exist
    att_id = _uuid.uuid4().hex
    key = _attachment_key(pid, att_id, filename)
    params = {"Bucket": ATTACH_BUCKET, "Key": key, "ContentType": content_type}
    # Headers the browser MUST replay on its PUT — they are part of the SigV4
    # signature, so the upload 403s if they're missing or don't match.
    put_headers = {"Content-Type": content_type}
    if ATTACH_KMS_KEY_ID:
        params["ServerSideEncryption"] = "aws:kms"
        params["SSEKMSKeyId"] = ATTACH_KMS_KEY_ID
        put_headers["x-amz-server-side-encryption"] = "aws:kms"
        put_headers["x-amz-server-side-encryption-aws-kms-key-id"] = ATTACH_KMS_KEY_ID
    try:
        url = _s3_client().generate_presigned_url(
            "put_object", Params=params, ExpiresIn=300,
        )
    except Exception as e:
        log.warning("[Attach] presign PUT failed for item %s: %s", pid, e)
        raise HTTPException(502, "Could not presign the upload (storage unavailable).")
    return {"attId": att_id, "key": key, "url": url,
            "name": _sanitize_filename(filename), "headers": put_headers}

@app.post("/api/items/{pid}/attachments")
def add_attachment(pid: int, body: dict = Body(...),
                   auth: dict = Depends(require_role("admin", "editor"))):
    """Record an attachment on the item blob after the browser's direct S3 PUT."""
    team = auth["team"]
    username = body.get("_username") or auth["username"]
    att_id = body.get("attId"); key = body.get("key")
    if not att_id or not key:
        raise HTTPException(422, "attId and key are required")
    rec = {
        "id": att_id, "key": key,
        "name": (body.get("name") or "file"),  # TRUE original filename
        "contentType": body.get("contentType") or "application/octet-stream",
        "size": int(body.get("size") or 0),
        "by": username,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    with db(team) as c:
        p = _get_item_blob(c, pid)
        atts = p.get("attachments") or []
        atts.append(rec)
        p["attachments"] = atts
        _save_project(c, pid, p)
    write_audit(team, "attachment:add", username, pid, p.get("name", ""), changes={"file": rec["name"]})
    return rec

@app.get("/api/items/{pid}/attachments")
def list_attachments(pid: int, auth: dict = Depends(require_auth)):
    """List attachments, each with a short-lived presigned GET URL (orig name + type)."""
    team = auth["team"]
    with db(team) as c:
        p = _get_item_blob(c, pid)
    atts = p.get("attachments") or []
    out = []
    cli = None
    for a in atts:
        url = None
        try:
            if cli is None:
                cli = _s3_client()
            url = cli.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": ATTACH_BUCKET, "Key": a["key"],
                    "ResponseContentDisposition": f'inline; filename="{a.get("name", "file")}"',
                    "ResponseContentType": a.get("contentType", "application/octet-stream"),
                },
                ExpiresIn=300,
            )
        except Exception as e:
            log.warning("[Attach] presign GET failed for %s: %s", a.get("key"), e)
        out.append({**a, "url": url})
    return {"attachments": out}

@app.delete("/api/items/{pid}/attachments/{att_id}")
def delete_attachment(pid: int, att_id: str,
                      auth: dict = Depends(require_role("admin", "editor"))):
    """Remove the object from S3 (server-side) and the metadata from the blob."""
    team = auth["team"]; username = auth["username"]
    with db(team) as c:
        p = _get_item_blob(c, pid)
        atts = p.get("attachments") or []
        target = next((a for a in atts if a.get("id") == att_id), None)
        if not target:
            raise HTTPException(404, "Attachment not found")
        p["attachments"] = [a for a in atts if a.get("id") != att_id]
        _save_project(c, pid, p)
    try:
        _s3_client().delete_object(Bucket=ATTACH_BUCKET, Key=target["key"])
    except Exception as e:
        log.warning("[Attach] S3 delete failed for %s: %s", target.get("key"), e)
    write_audit(team, "attachment:delete", username, pid, p.get("name", ""), changes={"file": target.get("name")})
    return {"deleted": att_id, "name": target.get("name")}

# ── Notifications & watchers (Stage 3b) ──────────────────────────────────────
# Server-side generation hooked into the real write paths (item update/assign,
# comment, planning commit, jira sync). Every hook runs AFTER the primary write
# has committed and is wrapped best-effort so a notification failure can never
# fail or roll back the underlying mutation. Self-notifications are suppressed.
_MENTION_TEXT_RE = re.compile(r'@([A-Za-z0-9._-]+)')
_MENTION_HTML_RE = re.compile(r'data-u="([^"]+)"')

def _team_usernames(team: str) -> set:
    with db(team) as c:
        row = c.execute("SELECT value FROM config WHERE key='users'").fetchone()
    try:
        return {u.get("username") for u in (json.loads(row["value"]) if row else []) if u.get("username")}
    except Exception:
        return set()

def _notify(team, recipients, ntype, item_id, item_name, message, actor):
    """Insert one notification per recipient, de-duped and excluding the actor."""
    recips = [r for r in dict.fromkeys(recipients or []) if r and r != actor]
    if not recips:
        return
    ts = datetime.now(timezone.utc).isoformat()
    with db(team) as c:
        for r in recips:
            c.execute("INSERT INTO notifications(username,type,item_id,item_name,message,actor,created_ts,read)"
                      " VALUES(?,?,?,?,?,?,?,0)", (r, ntype, item_id, item_name or "", message, actor or "", ts))

def _get_watchers(team, item_id) -> set:
    with db(team) as c:
        rows = c.execute("SELECT username FROM watchers WHERE item_id=?", (item_id,)).fetchall()
    return {r["username"] for r in rows}

def _add_watchers(team, item_id, usernames):
    us = [u for u in dict.fromkeys(usernames or []) if u]
    if not us:
        return
    with db(team) as c:
        for u in us:
            c.execute("INSERT OR IGNORE INTO watchers(item_id,username) VALUES(?,?)", (item_id, u))

def _parse_mentions_text(text, valid):
    return {m for m in _MENTION_TEXT_RE.findall(text or "") if m in valid}

def _parse_mentions_html(html, valid):
    return {m for m in _MENTION_HTML_RE.findall(html or "") if m in valid}

def _item_name(team, item_id):
    with db(team) as c:
        row = c.execute("SELECT data FROM projects WHERE id=?", (item_id,)).fetchone()
    try:
        return json.loads(row["data"]).get("name", "") if row else ""
    except Exception:
        return ""

def _notify_item_update(team, pid, old, new, actor):
    """Status change → watchers; assignee change → new assignee; new description mention → mentioned."""
    name = new.get("name", "") or old.get("name", "")
    os_, ns_ = old.get("status", ""), new.get("status", "")
    if ns_ and ns_ != os_:
        _notify(team, _get_watchers(team, pid), "watch_status", pid, name,
                f"{actor} changed status to {ns_}", actor)
    oa, na = old.get("assignee", ""), new.get("assignee", "")
    if na and na != oa:
        _add_watchers(team, pid, [na])
        _notify(team, [na], "assigned", pid, name, f"{actor} assigned you {name or 'an item'}", actor)
    od, nd = old.get("description", "") or "", new.get("description", "") or ""
    if nd != od:
        valid = _team_usernames(team)
        new_m = _parse_mentions_html(nd, valid) - _parse_mentions_html(od, valid)
        if new_m:
            _add_watchers(team, pid, list(new_m))
            _notify(team, list(new_m), "mention", pid, name, f"{actor} mentioned you in {name or 'an item'}", actor)

def _notify_on_comment(team, item_id, author, text, parent_id=None):
    if not item_id:
        return
    valid = _team_usernames(team)
    mentioned = _parse_mentions_text(text, valid)
    name = _item_name(team, item_id)
    _add_watchers(team, item_id, [author] + list(mentioned))   # commenter + mentioned auto-watch
    if mentioned:
        _notify(team, list(mentioned), "mention", item_id, name, f"{author} mentioned you in a comment", author)
    # Stage 4: a reply notifies the parent comment's author (unless self / already mentioned).
    notified = set(mentioned)
    if parent_id is not None:
        with db(team) as c:
            prow = c.execute("SELECT author FROM comments WHERE id=?", (parent_id,)).fetchone()
        pa = prow["author"] if prow else None
        if pa and pa != author and pa not in notified:
            _notify(team, [pa], "reply", item_id, name, f"{author} replied to your comment on {name or 'an item'}", author)
            notified.add(pa)
    watchers = _get_watchers(team, item_id) - notified          # avoid double-notifying the mentioned / replied-to
    _notify(team, watchers, "watch_comment", item_id, name, f"{author} commented on {name or 'an item'}", author)

@app.get("/api/notifications")
def list_notifications(auth: dict = Depends(require_auth)):
    team = auth["team"]; me = auth["username"]
    with db(team) as c:
        rows = c.execute("SELECT * FROM notifications WHERE username=? ORDER BY id DESC LIMIT 100", (me,)).fetchall()
        unread = c.execute("SELECT COUNT(*) FROM notifications WHERE username=? AND read=0", (me,)).fetchone()[0]
    return {"notifications": [dict(r) for r in rows], "unread": unread}

@app.post("/api/notifications/read")
def mark_notifications_read(body: dict = Body({}), auth: dict = Depends(require_auth)):
    team = auth["team"]; me = auth["username"]
    with db(team) as c:
        if body.get("all"):
            c.execute("UPDATE notifications SET read=1 WHERE username=? AND read=0", (me,))
        elif body.get("id") is not None:
            c.execute("UPDATE notifications SET read=1 WHERE id=? AND username=?", (body.get("id"), me))
        unread = c.execute("SELECT COUNT(*) FROM notifications WHERE username=? AND read=0", (me,)).fetchone()[0]
    return {"unread": unread}

@app.get("/api/items/{pid}/watchers")
def get_item_watchers(pid: int, auth: dict = Depends(require_auth)):
    w = _get_watchers(auth["team"], pid)
    return {"watchers": sorted(w), "watching": auth["username"] in w}

@app.post("/api/items/{pid}/watch")
def watch_item(pid: int, auth: dict = Depends(require_auth)):
    _add_watchers(auth["team"], pid, [auth["username"]])
    return {"watching": True}

@app.post("/api/items/{pid}/unwatch")
def unwatch_item(pid: int, auth: dict = Depends(require_auth)):
    with db(auth["team"]) as c:
        c.execute("DELETE FROM watchers WHERE item_id=? AND username=?", (pid, auth["username"]))
    return {"watching": False}

@app.post("/api/items/{pid}/view")
def record_view(pid: int, auth: dict = Depends(require_auth)):
    """Record that the caller viewed an item (beta shell → My Home Recent trail).
    Upserts the timestamp, then prunes to the newest ~100 per user. No
    self-suppression: it's my own trail, so my views count. Best-effort."""
    team = auth["team"]; me = auth["username"]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")  # match audit_log.ts so the merge sorts cleanly
    with db(team) as c:
        c.execute("INSERT INTO recent_views(username,item_id,viewed_ts) VALUES(?,?,?) "
                  "ON CONFLICT(username,item_id) DO UPDATE SET viewed_ts=excluded.viewed_ts",
                  (me, pid, ts))
        c.execute("DELETE FROM recent_views WHERE username=? AND item_id NOT IN "
                  "(SELECT item_id FROM recent_views WHERE username=? ORDER BY viewed_ts DESC LIMIT 100)",
                  (me, me))
    return {"ok": True}

# ── My Home (beta) read endpoints — additive, read-only, any authed user ──────
# These back the /beta "My Home" landing's Watching + Recent tabs. The data lives
# server-side (watchers table / audit_log), so the classic client never needs them.
@app.get("/api/my/watching")
def my_watching(auth: dict = Depends(require_auth)):
    """Item ids the current user watches. Archived/deleted items are excluded via
    the join to live, non-archived projects."""
    team = auth["team"]; me = auth["username"]
    with db(team) as c:
        rows = c.execute(
            "SELECT w.item_id FROM watchers w JOIN projects p ON p.id=w.item_id "
            "WHERE w.username=? AND p.archived=0", (me,)).fetchall()
    return {"items": [r["item_id"] for r in rows]}

@app.get("/api/my/recent")
def my_recent(auth: dict = Depends(require_auth)):
    """Merged 'recently touched by me' trail, deduped by item (newest wins),
    newest-first. Accepts MULTIPLE sources: 'worked' (my mutations in the audit
    log) and 'viewed' (items I opened in the beta shell). Archived/deleted items
    excluded via the join to live projects."""
    team = auth["team"]; me = auth["username"]
    merged = {}   # item_id -> {item_id, lastTouched, source}
    def _merge(item_id, ts, source):
        if item_id is None:
            return
        cur = merged.get(item_id)
        if cur is None or (ts or "") > (cur["lastTouched"] or ""):
            merged[item_id] = {"item_id": item_id, "lastTouched": ts, "source": source}
    with db(team) as c:
        worked = c.execute(
            "SELECT a.project_id AS item_id, MAX(a.ts) AS ts "
            "FROM audit_log a JOIN projects p ON p.id=a.project_id "
            "WHERE a.username=? AND a.project_id IS NOT NULL AND p.archived=0 "
            "GROUP BY a.project_id", (me,)).fetchall()
        viewed = c.execute(
            "SELECT v.item_id AS item_id, v.viewed_ts AS ts "
            "FROM recent_views v JOIN projects p ON p.id=v.item_id "
            "WHERE v.username=? AND p.archived=0", (me,)).fetchall()
    for r in worked:
        _merge(r["item_id"], r["ts"], "worked")
    for r in viewed:
        _merge(r["item_id"], r["ts"], "viewed")
    items = sorted(merged.values(), key=lambda x: x["lastTouched"] or "", reverse=True)[:50]
    return {"items": items}

# ── Import ────────────────────────────────────────────────────────────────────
@app.post("/api/import")
def bulk_import(body: dict = Body(...), auth: dict = Depends(require_role("admin"))):
    team = auth["team"]
    username = body.pop("_username", auth["username"])
    with db(team) as c:
        c.execute("DELETE FROM projects")
        for p in body.get("projects", []):
            p.pop("id", None)
            _insert_project(c, p)
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
  <a href="/?team={html.escape(team)}">← Back to Flow</a>
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

    user_opts  = "".join(f'<option value="{html.escape(u)}"{" selected" if u==user else ""}>{html.escape(u)}</option>' for u in all_users)
    action_opts = "".join(f'<option value="{html.escape(a)}"{" selected" if a==action_type else ""}>{html.escape(a)}</option>' for a in all_actions)

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<title>Frazil Flow — Audit Log ({team})</title>
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
                "object": {"url": roadmap_url, "title": f"Flow: {item_name}",
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
                "object": {"url": roadmap_url, "title": f"Flow: {item_name}",
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
        _save_project(c, pid, p)

    # Roadmap status change → item history activity (Auto-Cleared, not visible in AC open tab)
    if status_change_activity:
        try:
            _insert_activity(status_change_activity, auth["team"])
        except Exception as e:
            log.warning(f"[Jira] Failed to log status change activity for item {pid}: {e}")

    # Stage 3b: notify watchers of a Jira-driven status change (post-commit, best-effort).
    # Suppressed for bulk/initial syncs (jira_pull_all sets _suppressNotify) to avoid a
    # post-deploy notification burst from accumulated Jira changes.
    if any_changed and best_new_status and best_new_status != old_status and not (body or {}).get("_suppressNotify"):
        try:
            _notify(team, _get_watchers(team, pid), "watch_status", pid, p.get("name", ""),
                    f"Jira sync changed status to {best_new_status}", "Jira Sync")
        except Exception as e:
            log.warning(f"[Notify] jira status hook failed for item {pid}: {e}")

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
                                        _save_project(c2, rr["id"], existing)
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
                    child_id = _insert_project(c, child)
                    _save_project(c, child_id, {**child, "id": child_id})
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
    # Stage 3b: bulk sync = the initial/backfill path → suppress per-watcher notifications
    # so a post-deploy run can't emit a burst for accumulated Jira status changes.
    body = {**(body or {}), "_suppressNotify": True}

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
    item_id = body.get("item_id")
    author  = body.get("author", auth["username"])
    # Stage 4: single-level threads. Normalize parent_id to the thread ROOT — if the
    # target is itself a reply, attach to its parent; ignore a parent on another item or
    # a missing one (→ top-level). Guarantees at most one level regardless of client.
    parent_id = body.get("parent_id")
    with db(team) as c:
        if parent_id is not None:
            prow = c.execute("SELECT id,item_id,parent_id FROM comments WHERE id=?", (parent_id,)).fetchone()
            if not prow or prow["item_id"] != item_id:
                parent_id = None                      # missing / cross-item → top-level
            elif prow["parent_id"] is not None:
                parent_id = prow["parent_id"]         # reply-to-reply → root
        cur = c.execute(
            "INSERT INTO comments(item_id,author,body,created_ts,parent_id) VALUES(?,?,?,?,?)",
            (item_id, author, body.get("body",""), ts, parent_id)
        )
        row = c.execute("SELECT * FROM comments WHERE id=?", (cur.lastrowid,)).fetchone()
    # Stage 3b/4: mention + watcher (+ reply-to parent author) notifications (post-commit, best-effort)
    try:
        _notify_on_comment(team, item_id, author, body.get("body", ""), parent_id)
    except Exception as e:
        log.warning(f"[Notify] comment hook failed: {e}")
    return dict(row)

@app.delete("/api/comments/{cid}")
def delete_comment(cid: int, auth: dict = Depends(require_role("admin", "editor"))):
    team = auth["team"]
    with db(team) as c:
        # Stage 4: cascade — deleting a top-level comment removes its replies too (no orphans).
        c.execute("DELETE FROM comments WHERE id=? OR parent_id=?", (cid, cid))
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
            _save_project(c, item_id, updated)
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

    # Stage 3b: notify watchers of planning-driven status changes (post-commit, best-effort)
    try:
        for ch in changes:
            sf, st_ = ch.get("status_from"), ch.get("status_to")
            if st_ and sf != st_:
                _notify(team, _get_watchers(team, ch["item_id"]), "watch_status",
                        ch["item_id"], ch.get("name", ""),
                        f"{auth['username']} changed status to {st_} (planning)", auth["username"])
    except Exception as e:
        log.warning(f"[Notify] planning-commit hook failed: {e}")

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
                        _save_project(c, row_id, child_updated)
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
        new_id = _insert_project(c, new_item)
        _save_project(c, new_id, {**new_item, "id": new_id})
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


# ── SPA catch-all (Phase 3: Flow lives at root) ───────────────────────────────
# MUST be the LAST route. FastAPI matches in declaration order, so every route
# defined above wins first; only genuine root SPA paths (/list, /item/5,
# /planning/sprints, …) fall through to here and get roadmap.html (like root()).
# API/audit are guarded so a stray GET to an undefined /api/* or /audit path still
# 404s instead of returning HTML.
@app.get("/{full_path:path}", response_class=HTMLResponse)
def spa_catch_all(full_path: str):
    if full_path.startswith("api/") or full_path.startswith("audit"):
        raise HTTPException(404)
    if not os.path.exists(HTML):
        raise HTTPException(404, "roadmap.html not found next to server.py")
    with open(HTML, encoding="utf-8") as f:
        return f.read()


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
