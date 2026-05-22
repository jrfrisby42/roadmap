# CLAUDE.md

This file gives Claude Code the context it needs to be useful immediately. Read it before touching code. See `ARCHITECTURE.md` for deep references and `DEPLOYMENT.md` for production operations.

---

## What this project is

**Frazil Roadmap** is an internal, multi-tenant team roadmap / Gantt / Kanban / planning tool. It is live in production at `https://roadmap.frazil.app` (running on EC2). Current version is **3.1.0** (set in both `server.py` `APP_VERSION` and `roadmap.html` `const APP_VERSION`).

The whole app is **two files**:

- `server.py` — FastAPI + SQLite backend (~3,100 lines)
- `roadmap.html` — entire frontend: HTML, CSS, and JS in one file (~13,200 lines)

No build step. No bundler. No npm. The HTML file is served directly by FastAPI at `/`. This is intentional — it keeps deploy to a single `scp` of two files.

---

## How to run locally

```bash
python server.py
# then open http://localhost:8000
```

Optional `.env` next to `server.py` (loaded on startup):

```
JIRA_BASE_URL=https://freezingpointllc.atlassian.net
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=...
TOKEN_SECRET=<long random hex>     # signs auth tokens; auto-generated if absent
CORS_ORIGINS=https://roadmap.frazil.app
```

Add a team: `python server.py --new-team acme` (prints an initial admin password and forces a change on first login).

---

## Tech stack & dependencies

- **Backend:** FastAPI, uvicorn, gunicorn, sqlite3 (stdlib), `urllib.request` for Jira HTTP calls. No `requests`, no SQLAlchemy, no ORM — keep it that way unless we have a real reason to change.
- **Frontend:** Vanilla JS + CSS in one HTML file. No frameworks, no React, no build step. SVG icons inline.
- **DB:** SQLite, one file per team at `/data/tenants/{team}/roadmap.db`. WAL mode.
- **Auth:** Custom HMAC-signed tokens (not JWT). See `ARCHITECTURE.md`.
- **Jira:** Atlassian Cloud REST API v3, hand-rolled HTTP via `urllib`.

---

## Repository layout

```
.
├── server.py            # FastAPI backend, all routes, DB, auth, Jira
├── roadmap.html         # Entire frontend (HTML/CSS/JS)
├── .env                 # local secrets, NOT committed
├── CLAUDE.md            # this file
├── ARCHITECTURE.md      # deep reference
├── DEPLOYMENT.md        # EC2/Caddy/systemd operations
└── /data/tenants/<team>/roadmap.db    # per-team SQLite (created on demand)
```

Both files in this repo are also mirrored at `/mnt/project/` when working in this environment.

---

## Working conventions (READ THIS BEFORE EDITING)

These have been hard-won across many sessions. Following them saves churn.

### 1. The HTML file is monolithic — accept it, don't fight it

`roadmap.html` is one ~13k-line file with `<style>`, then HTML body, then a single `<script>` block. We've discussed splitting it; the consensus is **don't** unless we're doing a full SPA rewrite. Splitting buys ergonomics only, costs the deploy simplicity, and risks breaking working code with no test suite.

When editing, navigate by searching for function names (`function renderKanban`) or DOM ids (`id="capCalModalBg"`).

### 2. Edit with `str_replace`, not by rewriting the whole file

Both files are large. Use `str_replace` with enough surrounding context to make the match unique. Never overwrite the whole file unless you really need to (e.g., a major refactor).

When patching the frontend programmatically (e.g. a Python `str_replace` script inside `bash_tool`), always run a syntax check after:

```bash
# Python
python3 -c "import ast; ast.parse(open('server.py').read()); print('server.py OK')"

# JS inside the HTML
python3 -c "
import re
c = open('roadmap.html').read()
s = re.findall(r'<script>(.*?)</script>', c, re.DOTALL)[0]
open('/tmp/chk.js','w').write(s)
" && node --check /tmp/chk.js && echo 'JS OK'
```

The HTML has exactly one `<script>` block (no `src=`); the regex above grabs it. If a future change adds more script tags, adapt accordingly.

### 3. Status semantics are config-driven — never hardcode status names

Status names are per-team configurable. Behavior is driven by boolean flag maps stored in the `config` table:

- `statusIsActive` — counts toward capacity, locks parallelResources edits
- `statusIsTerminal` — completed; excludes from active counts
- `statusIsDefault` — exactly one true; default for new items
- `statusIsDeferred` — exactly one true; assigned when an item is deferred in a planning session
- `statusIsReleased` — exactly one true; triggers FF pull from Jira hierarchy
- `statusIsApproved` — exactly one true; set by Review planning sessions
- `statusIsTesting` — exactly one true; the "in testing" phase

Never write `if status == "In Progress"`. Always resolve through the flag maps. Helpers exist on both server (inside `commit_planning_session`) and client (`getDefaultStatus()`, `getDeferredStatus()`, `isActiveStatus()`, etc.).

### 4. Items (`projects` table) are JSON blobs

The `projects` table is literally `id INTEGER PRIMARY KEY, data TEXT`. The full item is JSON-serialized into `data`. This means:

- You cannot SQL-index individual fields.
- `get_all` (`GET /api/all`) returns everything for the team in one shot; the frontend keeps it in memory in `projects[]`.
- "Items" and "projects" are the same thing in this codebase — historical naming. The actual project/product (Fraznet, HubSpot, etc.) is a separate config key called `products`.

### 5. The Jira sync is forward-only and change-gated

Jira → roadmap status sync **never regresses status**. It only advances. It tracks two per-item dicts to avoid re-processing:

- `jiraLastKnownStatus[ticket]` — the last Jira status we successfully consumed
- `jiraSyncSkipped[ticket]` — a Jira status we declined (because it would regress); we won't reprocess that same status again

If a Jira sync seems to not be picking up a change, those two dicts are the place to look.

Feature flags (`customfield_10064` in Jira, a label-type field) are walked across the full ticket hierarchy: ticket → epic → stories → subtasks. Result is deduped into `jiraFeatureFlags`. Manual flags live in `featureFlags`. The union is what gets shown.

### 6. Planning sessions are atomic, server-validated, server-applied

A planning session goes draft → committed (or discarded). Three types: **Review**, **Sprint**, **Release**. The frontend builds a `payload`; the server validates and applies all changes in a single DB transaction via `POST /api/planning-sessions/{id}/commit`. Validation rules live in `_validate_session_payload()`. Don't write per-item PUT loops for things a session covers — use the commit endpoint.

### 7. Capacity model

- `parallelResources` (per item) — how many resource units the item consumes during dev work.
- Phases: **Dev work** (`dueWeeks − testWeeks`) consumes `parallelResources`. **Testing** (`testWeeks`) consumes `1`. **Delay** (`revised − due`) consumes `1`.
- `parallelResources` is always rounded **up** to the nearest 0.25 (min 1.0). Done on the server in `round_up_to_quarter()` in `create_project` and `update_project`.
- `parallelResources` cannot be changed while the item is in an active status — server returns 422.
- Per-owner per-day overrides live in the `capacity_overrides` table. Defaults come from the `ownerCapacity` config key.

### 8. Test period cannot equal or exceed time estimate

`testWeeks >= dueWeeks` returns HTTP 422 on PUT. Enforce on the client too; don't bypass on the server.

### 9. Parent/child vs requires/dependent are different things

- **Parent / child** = organizational containment (an epic and its work items). No date relationship. A parent is a label, not a scheduling constraint.
- **Requires / dependent** = scheduling. Item B requires item A — A must happen before B (or alongside, if `parallel` is set on the requirement).

An item can only have one of these two relationships — enforced in the UI via mutual exclusion when picking "Parent Item" or "Depends On" in the item modal.

### 10. Roles

- `viewer` — read-only. Cannot see Planning view. Kanban hidden from this role. Clicking a Gantt bar opens the read-only Item Page instead of the edit modal.
- `editor` — can create/update projects, capacity overrides, planning sessions. Time/date fields are read-only for editors when the item is in an active status. Editors can be **owner-scoped** via the per-user `ownerFilter` field (see below).
- `admin` — everything, plus config edits, user management, deletes.

Routes are gated with `Depends(require_role("admin", "editor"))` or similar. Never rely on the frontend to enforce a role — every state-changing endpoint must have a server-side `require_role`.

**Owner-scoped editors:** a user record can carry `ownerFilter: "<owner name>"`. When set on an editor, the UI scopes their view to items they own, locks the owner field in the edit modal, and shows a 📌 badge. This is UI-only scoping — server enforcement is by role, not by ownerFilter.

**The "primary admin" rule:** only the builtin admin user (the one created at team init) can change other admin users' passwords via `POST /api/users/{username}/password`. This is checked inside the endpoint, not in `require_role`.

### 11. XSS — always escape user content

The server-rendered audit log at `/audit` uses `html.escape()` on every dynamic value. Past XSS issues lived there. In the frontend, use the `esc()` helper before injecting into `innerHTML`.

### 12. Don't break the API surface

The frontend assumes:
- `GET /api/all` returns `{projects, config, activities, ...}` in one call
- `PUT /api/config/{key}` for any of the keys in `VALID_KEYS` (server.py line ~986)
- Token comes back from `POST /api/login`; client stores it as `frazil_token` in `localStorage` and sends `Authorization: Bearer <token>` and `X-Team: <team>` on every request

If you're tempted to "improve" the API shape, check the frontend first — a lot depends on these contracts.

### 13. The Item Page is a separate UX surface from the modal

There are two ways to view an item:

- **Edit Modal** — opened by clicking a Gantt bar / Kanban card as admin or editor. Lightweight, focused on edits. JIRA quick-add and a compact notes field are here.
- **Item Page** — opened as `/?item={id}` (bookmarkable, shareable) or by clicking from a viewer's session. Full-screen overlay with Schedule card, JIRA tickets with live status, Dependencies, Activity history, Comments thread, Admin Controls. Pushes into `history.pushState`, restores on back button via a `popstate` handler.

Same data, same permission rules. If a field is read-only in the modal for a given role, it must be read-only on the Item Page too. Comments live on the Item Page only — not the modal. The "external link" icon next to the item name in the modal opens the Item Page in a new tab.

`checkItemPageParam()` runs on boot to handle deep links.

### 14. Recurrence is a single string field, not multiple flags

Item field `recurrence` is a string: `"none"` | `"weekly"` | `"biweekly"` | `"monthly"`. There is no separate `recurring` boolean. `recurrence === "none"` means non-recurring; anything else means recurring with that cadence.

Related fields:
- `syncChildren: bool` — for recurring items with a linked Jira ticket, pull Jira sub-issues into hidden roadmap children.
- `recurrence_parent: <pid>` — back-reference on a spawned occurrence to its predecessor in the chain.

When a recurring item becomes terminal, `POST /api/projects/{pid}/recur` spawns the next occurrence. New start = previous start + period (NOT today). The new item gets the team's `statusIsDefault` status, NOT a hardcoded "Planned". Children are NOT carried forward — they're pulled fresh from Jira after a new ticket is linked.

### 15. CSV import (HubSpot) lives in Admin → Data

The Admin Panel has a **Data** tab. It contains JSON export, JSON import, and CSV import (HubSpot Projects format). The CSV importer maps HubSpot fields to roadmap fields:

| HubSpot field | Roadmap field |
|---|---|
| `Name` | name |
| `Record ID` | `hubspotId` (used for duplicate detection) |
| `Pipeline Stage` | status (mapped through a status table) |
| `Owner` | owner (matched to existing developers; falls back to default) |
| `Start date` | start |
| `Target due date` | due (also computes dueWeeks) |
| `Close date` | releaseDate (if completed) |

The importer is a 3-step modal: Upload → Preview (shows new vs duplicate in green/amber) → Result. Duplicates are detected by name (case-insensitive) AND `hubspotId`; matches are skipped. The pipeline-stage mapping is hardcoded in the frontend — extend it there to support other CSV sources.

### 16. Saved Views, dark mode, and view persistence

**Saved Views** — accessible from the hamburger menu. Two system views (`My Work`, `At Risk`) plus user-created custom views. Stored in `localStorage` under key `frazil_saved_views_<username>` (per-user, not server-side). A view captures all dropdown filters, date range, view mode, group-by, and search term. Default view applies automatically on login.

**Dark mode** — toggle in the hamburger menu. Stored in `localStorage.frazil_dark_mode` (`'1'` or `'0'`). Default is light; falls back to OS `prefers-color-scheme` if unset. Implemented as `body.dark-mode` class with CSS variable overrides — `--bg`, `--surface`, `--surface2`, `--border`, `--row-sep`, `--text`, `--muted`, `--accent`, `--accent2`, `--today-line`. All custom components must reference the variables, not hard-code colors, or they'll break in dark mode.

**View mode** (Gantt / Kanban / Planning / Dashboard) is in `sessionStorage.frazil_view`. The select order in the UI is fixed: Gantt → Kanban → Planning → Dashboard.

### 17. Frontend storage keys — full inventory

`localStorage` (persistent):
- `frazil_token` — bearer token
- `frazil_rm_session` — `'1'` if logged in
- `frazil_rm_user` — username
- `frazil_rm_role` — `'viewer'`/`'editor'`/`'admin'`
- `frazil_rm_team` — current team slug
- `frazil_rm_login_ts` — login timestamp (ms)
- `frazil_dark_mode` — `'1'`/`'0'`
- `frazil_saved_views_<username>` — JSON array of saved views
- `frazil_default_view_<username>` — id of the default view

`sessionStorage` (per-tab):
- `frazil_view` — current view mode

The `SK` constant at the top of the script enumerates the older session keys: `const SK = { session:'frazil_rm_session', team:'frazil_rm_team' };`. New code should follow the `frazil_*` naming convention.

### 18. Jira description sync uses ADF

Jira REST API v3 requires Atlassian Document Format (ADF — a JSON node tree) for description updates, not plain text or HTML. The frontend `notesToADF(html)` helper converts the rich-text notes (HTML) to ADF: strip tags, decode entities, convert `<br>`, `<p>`, `<li>` to newlines/bullets, wrap each paragraph in an ADF paragraph node.

Pushing notes to Jira goes through `PUT /api/jira/issue/{key}` with `{ fields: { description: <ADF> } }`. The server passes this through to Jira's PUT endpoint. ADF parsing (Jira → roadmap, for display) is in `adf_to_text()` in `get_jira_issue`.

---

## Common tasks

### Add a new endpoint

1. Decide the role gate (`admin` only? `admin` + `editor`?).
2. Add the route in `server.py` near related routes (Jira routes are clustered, planning routes are clustered, etc.).
3. Use `auth: dict = Depends(require_role(...))` and read `auth["team"]`, `auth["username"]`.
4. Wrap DB access in `with db(team) as c:` — the helper handles commit/rollback.
5. Call `write_audit(team, "action", username, pid, name, changes=...)` for any mutation worth tracking.
6. Frontend: add a method to the `API` object near `const API = {` (~line 1990 in roadmap.html) — it handles the auth header and X-Team automatically.

### Add a new config key

1. Add the key name to `VALID_KEYS` in `server.py` (the `PUT /api/config/{key}` allowlist).
2. Add the default in the `defaults` dict inside `init_team_db()`.
3. Also add the key in `_migrate_config_keys()` so existing team DBs get backfilled on next boot.
4. Frontend: declare the variable at module top (near `let ownerCapacity = {}`), load it in `boot()` from `data.config`, and persist with `API.putConfig(key, value)`.

### Add a new status flag (e.g. `statusIsX`)

1. Add to `VALID_KEYS` and `init_team_db` defaults `{}` and `_migrate_config_keys`.
2. Add a `let statusIsX = {}` at the top of the script in `roadmap.html`.
3. Load it from config in `boot()`.
4. If the rule is "exactly one true" (like Default/Deferred/Released), enforce it in the admin status editor and on save.
5. Use the flag map everywhere, never the literal status name.

### Modify items (server-side)

Items are JSON blobs in `projects.data`. The pattern is always:

```python
with db(team) as c:
    row = c.execute("SELECT data FROM projects WHERE id=?", (pid,)).fetchone()
    if not row: raise HTTPException(404, "Item not found")
    p = json.loads(row["data"])
    # ... modify p ...
    c.execute("UPDATE projects SET data=? WHERE id=?", (json.dumps(p), pid))
```

### Run a Jira sync

- Per-item pull: `POST /api/jira/pull/{pid}`
- Per-item push status: `POST /api/jira/sync-status/{pid}`
- Bulk pull all: `POST /api/jira/pull-all` (called from the "Sync Jira" button)
- Child sync for recurring items: `POST /api/projects/{pid}/sync-children` (driven by `syncChildren: true` on the item)

---

## What I should NOT do unprompted

- Don't migrate to PostgreSQL.
- Don't split the HTML file into separate JS modules.
- Don't add a build step (no Vite, no esbuild, no webpack).
- Don't introduce a frontend framework (no React, Vue, Svelte, HTMX).
- Don't add an ORM (no SQLAlchemy, no SQLModel).
- Don't change the `X-Team` header convention.
- Don't change the `Authorization: Bearer <token>` shape.
- Don't write tests scaffolding (pytest, playwright) unless asked — there are currently none, and adding them is its own decision.

These are real choices we've made, not oversights. If something looks like it would benefit from one of these, flag it and ask first.

---

## Style notes

- Comments use `# ── Heading ─────...` boxes for major sections in `server.py`. Match the style for new sections.
- Server code is generally 4-space indent, type hints where convenient but not exhaustive.
- Frontend uses 2-space indent. Function names are camelCase.
- Error messages should be specific: `"Test period (3w) cannot equal or exceed the time estimate (3w)"` not `"Invalid"`.

---

## Deployment cheat sheet

Full details in `DEPLOYMENT.md`. The fast version:

- Production host: `ubuntu@52.35.224.183` (Elastic IP), EC2 `t4g.small`, Ubuntu 24.04 ARM64
- App path: `/opt/roadmap/`
- DB path: `/data/tenants/{team}/roadmap.db` (separate EBS volume at `/data`)
- Reverse proxy: Caddy at `/etc/caddy/Caddyfile`, auto-SSL via Let's Encrypt
- Service: systemd unit `roadmap.service` running `gunicorn server:app -w 2 -k uvicorn.workers.UvicornWorker --bind 127.0.0.1:8000`
- Deploy: `scp server.py roadmap.html ubuntu@52.35.224.183:/opt/roadmap/`
- HTML changes need NO restart (Caddy reads it fresh). `server.py` changes need `sudo systemctl restart roadmap`.
- Logs: `/var/log/roadmap-access.log`, `/var/log/roadmap-error.log`, plus `journalctl -u roadmap`
