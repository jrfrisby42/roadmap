# ARCHITECTURE.md

Deep reference for the Frazil Roadmap codebase. Read `CLAUDE.md` first for conventions; this doc is for *how things work*.

---

## High-level shape

```
Browser  ──HTTPS──▶  Caddy  ──HTTP──▶  gunicorn(uvicorn) ──▶ server.py (FastAPI)
                                                                │
                                                                ▼
                                              /data/tenants/<team>/roadmap.db (SQLite, WAL)
                                                                │
                                                                ▼
                                              (outbound only) ──▶ Jira Cloud REST v3
```

- One FastAPI process, two gunicorn workers.
- Per-team SQLite database; `X-Team` header from the client selects which DB to open per request.
- Frontend is one HTML page loaded once; everything after is JSON over `fetch`.

---

## Authentication

Custom HMAC-signed tokens. Not JWT.

- `TOKEN_SECRET` (env var, auto-generated random hex if absent) signs each token.
- Token shape: base64(`username|team|role|expires_at`)`.`base64(hmac_sha256(payload, secret)).
- `require_auth` dependency parses `Authorization: Bearer <token>`, verifies signature, checks expiry, returns `{username, team, role}`.
- `require_role("admin")` / `require_role("admin", "editor")` wraps `require_auth` and adds the role check.
- Token lifetime: tokens carry an expiry; the client also checks it before sending. On 401, the client clears its session and re-shows the login modal.

**Roles:**

| Role | Can do |
|---|---|
| `viewer` | Read everything except Planning view (hidden). No state-changing endpoints. |
| `editor` | Create/update items, planning sessions, capacity overrides, Jira sync. |
| `admin` | Everything: config edits, user management, deletes, audit log. |

**Important:** every state-changing endpoint MUST have a `require_role` dependency. Don't rely on the frontend to gate behavior.

**Password storage:** salted SHA-256 hashes. Legacy plaintext passwords are auto-upgraded on next successful login (`_migrate_passwords`).

**Forced password change:** new accounts and reset accounts get `mustChangePassword: true`. The frontend blocks all UI behind a Force Change Password modal until satisfied.

---

## Multi-tenancy

- Team slug is `[a-z0-9]` only.
- DB path: `/data/tenants/{team}/roadmap.db`.
- `db(team)` context manager opens the right DB, sets WAL mode, handles commit/rollback.
- `init_team_db(team)` runs once per team (cached in `_initialized_teams`); creates tables, seeds defaults, runs migrations.
- The `development` team is auto-created on boot. Other teams via `python server.py --new-team <slug>`.
- There is also a legacy single-DB layout (one `roadmap.db` at the repo root); `init_team_db` will migrate it under `development/` if found.

---

## Database schema (per team)

All tables live in `/data/tenants/{team}/roadmap.db`.

```sql
projects (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL DEFAULT '{}'      -- full JSON blob of the item
)

config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '[]'     -- JSON: array, object, or scalar
)

audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,                -- 'create' | 'update' | 'delete' | 'planning_session:create' | ...
    project_id INTEGER,
    project_name TEXT,
    changes TEXT                          -- JSON: { field: {from, to} } or arbitrary
)

activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_type TEXT NOT NULL,         -- 'At Risk' | 'Blocked' | 'Due Date Change' | 'Capacity Conflict' | ...
    source TEXT NOT NULL DEFAULT 'System',
    item_id INTEGER, item_name TEXT, owner TEXT, project TEXT,
    created_by TEXT, created_ts TEXT,
    read_by TEXT, read_ts TEXT,
    resolved_by TEXT, resolved_ts TEXT,
    action_taken TEXT,
    previous_value TEXT, new_value TEXT,
    note TEXT,
    message TEXT
)

comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    ts TEXT NOT NULL,
    body TEXT NOT NULL
)

planning_sessions (
    id TEXT PRIMARY KEY,                 -- e.g. 'ps_1714502400000'
    name TEXT NOT NULL,
    type TEXT NOT NULL,                  -- 'Review' | 'Sprint' | 'Release'
    status TEXT NOT NULL,                -- 'draft' | 'committed' | 'discarded'
    created_by TEXT, created_ts TEXT, committed_ts TEXT,
    release_number TEXT, release_notes TEXT,
    payload TEXT NOT NULL DEFAULT '{}',  -- the full session state
    locked_by TEXT, locked_ts TEXT,      -- single-writer lock for commit
    snapshot TEXT NOT NULL DEFAULT '{}'  -- items snapshot at session start, for conflict detection
)

capacity_overrides (
    owner TEXT, date TEXT, value INTEGER,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (owner, date)
)
```

**Cascade rules:** deleting a project cascades to its `comments` and `activities` rows (in `delete_project`). `audit_log` rows are kept by design — they're the history of the deletion.

**Migrations:** all migrations are idempotent `ALTER TABLE … ADD COLUMN` calls wrapped in `try/except` inside `init_team_db()`. There is no migration framework; just add the column and a `try/except pass` block.

---

## Item (project) JSON schema

These are the keys you'll find inside `projects.data`. Not all are always present; default to falsy/empty.

```jsonc
{
  "id": 123,
  "name": "MRT 2.0",
  "product": "Fraznet",                 // matches a config.products[].name
  "owner": "Tyler / Gerald",            // matches a config.developers[]
  "status": "In Progress",              // matches one of config.statuses
  "type": "Feature",                    // matches a config.types[].name
  "priority": 2,                        // 1=Urgent, 2=High, 3=Medium, 4=Low
  "start": "2026-04-01",                // YYYY-MM-DD (locked planning view)
  "due":   "2026-05-15",                // YYYY-MM-DD
  "revised": null,                      // delay end date if delayed
  "dueWeeks": 6.0,                      // total time estimate including testing
  "testWeeks": 1.5,                     // testing portion (must be < dueWeeks)
  "parallelResources": 1.5,             // capacity units during dev; UP to nearest 0.25
  "description": "...",                 // free text

  // Relationships (mutually exclusive)
  "parent":   456,                      // parent item id (organizational)
  "requires": 789,                      // dependency item id (scheduling)
  "parallel": false,                    // when requires is set: dependent can run alongside?

  // Jira linkage
  "jiraTickets": ["FRAZ-10535"],        // up to 10 linked tickets
  "jiraCache":    { "FRAZ-10535": { /* cached summary, status, etc */ } },
  "jiraLastSync": "2026-04-29 15:30:00 UTC",
  "jiraLastKnownStatus": { "FRAZ-10535": "In Review" },
  "jiraSyncSkipped":     { "FRAZ-10535": "Backlog" },
  "jiraFeatureFlags": ["mrt_v2_fraz_10570"],   // Jira-sourced (customfield_10064)
  "featureFlags":     ["mrt_v2_fraz_10570", "manual_flag"],  // union of manual + Jira

  // Recurrence (recurring items)
  "recurrence": "weekly",                // "none" | "weekly" | "biweekly" | "monthly"
  "syncChildren": true,                  // pull child items from Jira sub-issues
  "recurrence_parent": 122,              // id of the previous occurrence in the chain
  "hidden": false,                       // child items spawned from Jira are typically hidden

  // Release
  "releaseDate":   "2026-04-22",
  "releaseNumber": "v2.7.1",
  "releaseNotes":  "...",

  // Activity flags
  "atRisk": false,
  "blocked": false,
  "deferred": false,                    // set when an item is deferred via planning session
  "deferReason": "Resource Unavailable",
  "deferNote": "Waiting on vendor",
  "deferRevisit": "2026-09-01",         // optional date to reconsider

  // Timeline change tracking
  "delayReason": "Partner Delays",
  "changeReason": "Scope Change",

  // Source attribution
  "jiraSource": "FRAZ-10570",           // set when item was spawned from Jira
  "hubspotId":  "abc123"                // set when item was imported from a HubSpot CSV (used to dedupe)
}
```

---

## Status flag system

Each `config.statusIs*` key is a `{ statusName: bool }` map. Resolve behavior through these, never through literal status strings.

| Flag | Cardinality | Meaning |
|---|---|---|
| `statusIsActive` | many | Counts toward owner capacity. While active, `parallelResources` is locked. |
| `statusIsTerminal` | many | Item is complete; excluded from active counts. |
| `statusIsDefault` | exactly 1 | Status applied to new items. |
| `statusIsDeferred` | exactly 1 | Status applied when an item is deferred in a planning session. |
| `statusIsApproved` | exactly 1 | Status set by Review-type planning session commits. |
| `statusIsReleased` | exactly 1 | Triggers Jira feature-flag hierarchy pull on PUT. |
| `statusIsTesting` | exactly 1 | "In testing" phase; drives At-Risk auto-notification. |

Cardinality of "exactly 1" is enforced in the admin status editor. If somehow zero or many end up true, the helpers (`getDefaultStatus()`, `getDeferredStatus()`, etc.) return the first match or empty string — be defensive.

---

## API endpoint inventory

Grouped by area. All require `Authorization` + `X-Team` headers unless noted.

### Auth & users
| Method | Path | Role |
|---|---|---|
| `GET` | `/api/version` | public |
| `GET` | `/api/teams` | public (returns list of team slugs from `/data/tenants/*`) |
| `POST` | `/api/login` | public |
| `POST` | `/api/logout` | auth |
| `POST` | `/api/verify-password` | auth |
| `POST` | `/api/hash-password` | auth |
| `POST` | `/api/users/self/password` | auth |
| `POST` | `/api/users/{username}/password` | admin (primary admin only for other admins) |
| `POST` | `/api/force-change-password` | auth (when `mustChangePassword: true`) |
| `POST` | `/api/admin/reset-password` | admin |

### Data
| Method | Path | Role |
|---|---|---|
| `GET` | `/` | public — serves `roadmap.html` |
| `GET` | `/api/all` | auth — bulk fetch: projects + config + activities |
| `POST` | `/api/admin/seed-reasons` | admin |

### Projects (items)
| Method | Path | Role |
|---|---|---|
| `POST` | `/api/projects` | admin/editor |
| `PUT` | `/api/projects/{pid}` | admin/editor |
| `DELETE` | `/api/projects/{pid}` | admin |
| `POST` | `/api/projects/{pid}/recur` | admin/editor — spawn next recurrence |
| `POST` | `/api/projects/{pid}/sync-children` | admin/editor — pull child items from Jira |
| `POST` | `/api/projects/{pid}/sync-children-status` | admin/editor — pull child statuses |

### Config
| Method | Path | Role |
|---|---|---|
| `PUT` | `/api/config/{key}` | admin — key must be in `VALID_KEYS` |
| `POST` | `/api/import` | admin |

### Audit
| Method | Path | Role |
|---|---|---|
| `GET` | `/audit` | admin (HTML page, server-rendered, all dynamic values `html.escape`d) |

### Activities (notifications)
| Method | Path | Role |
|---|---|---|
| `GET` | `/api/activities` | auth |
| `POST` | `/api/activities` | admin/editor |
| `PUT` | `/api/activities/{aid}` | admin/editor — mark read/resolved |

### Comments
| Method | Path | Role |
|---|---|---|
| `GET` | `/api/comments/{item_id}` | auth |
| `POST` | `/api/comments` | admin/editor |
| `DELETE` | `/api/comments/{cid}` | admin/editor |

### Jira
| Method | Path | Role |
|---|---|---|
| `GET` | `/api/jira/status` | auth |
| `POST` | `/api/jira/test` | admin |
| `GET` | `/api/jira/projects` | auth — Jira project list |
| `GET` | `/api/jira/statuses` | auth — Jira status list |
| `GET` | `/api/jira/issuetypes` | auth |
| `POST` | `/api/jira/tickets` | auth — search/list tickets |
| `GET` | `/api/jira/issue/{key}` | auth |
| `POST` | `/api/jira/create-issue` | admin/editor — create Jira ticket from roadmap item |
| `POST` | `/api/jira/link-issue` | admin/editor |
| `POST` | `/api/jira/comment/{key}` | admin/editor |
| `PUT` | `/api/jira/issue/{key}` | admin/editor |
| `DELETE` | `/api/jira/issue/{key}` | admin/editor — unlinks (does NOT delete from Jira) |
| `POST` | `/api/jira/transition/{key}` | admin/editor |
| `POST` | `/api/jira/pull/{pid}` | admin/editor — per-item pull sync |
| `POST` | `/api/jira/sync-status/{pid}` | admin/editor — per-item push status |
| `POST` | `/api/jira/pull-all` | admin/editor — bulk pull all items |
| `POST` | `/api/jira/search-raw` | admin/editor — raw JQL passthrough |
| `GET` | `/api/jira/children/{key}` | auth |

### Capacity
| Method | Path | Role |
|---|---|---|
| `GET` | `/api/capacity-overrides` | auth |
| `POST` | `/api/capacity-overrides` | admin/editor |
| `DELETE` | `/api/capacity-overrides` | admin/editor |
| `POST` | `/api/capacity-overrides/batch` | admin/editor |
| `GET` | `/api/capacity-overrides/effective` | auth — owner-day effective capacity |

### Planning sessions
| Method | Path | Role |
|---|---|---|
| `POST` | `/api/planning-sessions` | admin/editor — create draft |
| `GET` | `/api/planning-sessions` | admin/editor — list (optional `?status=`) |
| `GET` | `/api/planning-sessions/active` | admin/editor — must be defined BEFORE `/{session_id}` |
| `GET` | `/api/planning-sessions/{session_id}` | admin/editor |
| `PUT` | `/api/planning-sessions/{session_id}/draft` | admin/editor — autosave |
| `PUT` | `/api/planning-sessions/{session_id}/snapshot` | admin/editor |
| `POST` | `/api/planning-sessions/{session_id}/release-metadata` | admin/editor |
| `POST` | `/api/planning-sessions/{session_id}/validate` | admin/editor |
| `POST` | `/api/planning-sessions/{session_id}/check-conflicts` | admin/editor |
| `POST` | `/api/planning-sessions/{session_id}/acquire-lock` | admin/editor |
| `POST` | `/api/planning-sessions/{session_id}/release-lock` | admin/editor |
| `POST` | `/api/planning-sessions/{session_id}/commit` | admin/editor — atomic apply |
| `DELETE` | `/api/planning-sessions/{session_id}` | admin/editor |

**Route ordering gotcha:** FastAPI matches paths in declaration order. Routes with a literal segment (`/active`) must be declared BEFORE the parameterized version (`/{session_id}`) or the literal will be captured as a path param. This is already correct in the code — keep it that way.

---

## Planning sessions in detail

Three session types, each opens different controls in the Planning view:

| Type | Approve | Defer | Sprint start dates | At Risk | Release block | Release metadata |
|---|---|---|---|---|---|---|
| Review | ✓ | ✓ | – | – | – | – |
| Sprint | – | ✓ | ✓ | ✓ | – | – |
| Release | – | ✓ | – | – | ✓ | ✓ |

Lifecycle: `draft` → `committed` (or `discarded`). Drafts autosave every ~30s via `PUT .../draft`. Conflict detection (`POST .../check-conflicts`) checks if any item in the session changed since the snapshot was taken; the commit endpoint refuses if any conflict is unresolved.

Commit endpoint applies all changes in a single SQLite transaction:

- Review: approved → `statusIsApproved` status.
- Sprint: sprint items → `statusIsActive` (first) + start date set; `at_risk_ids` → posts `At Risk` activities.
- Release: release items → `statusIsReleased` + `releaseDate`, `releaseNumber`, `releaseNotes`; `blocked_ids` → posts `Blocked` activities.
- All types: deferred → `statusIsDeferred`, dates cleared. Priority changes applied.

Commit returns 422 with `{"errors": [...]}` if validation fails. Frontend treats this as belt-and-suspenders alongside the per-item PUTs it also fires, so partial server failures don't lose data.

---

## Activities & auto-notifications

The `activities` table powers the Activity Center (a side panel in the UI). Activities have a `status` lifecycle:

- `Open` — created, not yet seen
- `Read` — viewer has acknowledged but not resolved
- `Resolved` / `Approved` / `Rejected` / `Dismissed` — terminal states with manual action
- `Auto-Cleared` — terminal state set by the system when the underlying condition resolves itself

Transitions are validated server-side in `PUT /api/activities/{aid}`: `Open → terminal` is allowed; any terminal → `Open` is allowed (reopen); other transitions are 400. **De-duplication:** `POST /api/activities` checks for an existing `Open` activity with the same `activity_type` + `item_id` and updates that row instead of inserting a duplicate.

**Activity types (what shows up in the AC filter dropdown):**

| Type | Source |
|---|---|
| `At Risk` | Auto-rule: item entered testing period without `statusIsTesting`. Also from Sprint sessions. |
| `Blocked` | Release sessions. |
| `Capacity Conflict` | `runCapacityConflictNotifications()` — owner exceeds `ownerCapacity[owner]` on a given window. Severity is `Critical` or `Warning`. |
| `Due Date Change` | Item PUT detects a `due` field change. |
| `Delay Date Change` | Item PUT detects a `revised` field change. |
| `Needs Decision` | Auto-rule: start date arrived with a non-active status. |
| `Needs Date Check` | Auto-rule: dates look inconsistent. |
| `Flagged Issue` | Manual flag. |
| `Priority Change` | Planning session reprioritizations. |

`runAutoNotifications()` (frontend) is the central auto-rule engine. It runs on view load and after item edits. It also performs **auto-release** of recurring items whose due date has passed (calls `POST /api/projects/{pid}/recur` to spawn the next occurrence).

**Auto-clear:** capacity conflict activities auto-clear when the underlying overlap is no longer present (item dates shifted, capacity overrides added, etc.).

---

## Item Page (deep-link view)

URL: `/?item={id}`. Implementation lives entirely in the frontend — there is no server route for it; the SPA detects the query param via `checkItemPageParam()` on boot (after `/api/all` returns) and opens the overlay.

Layout: sticky header (breadcrumb, status badge, Edit Item button), two-column body. **Left:** Details, Notes (rich-text), Dependencies (clickable cross-links to other items), Activity & History (last 20), Comments thread. **Right sidebar:** Schedule card, JIRA tickets with live status fetch, Admin Controls (recurrence, defer state, item ID — admin only).

Edit rules mirror the modal. Viewers see read-only content; the Edit Item button is hidden. The same `isAdmin/isEditor/isTerm/isLocked` flag tree governs both surfaces — if you add a new edit gate, apply it in both places.

Navigation uses `history.pushState({itemPage:id}, '', '?item=${id}')`. A `popstate` handler closes the page when the user hits Back. Opening an item in a new tab is via a plain `<a href="/?item=${p.id}" target="_blank">` so it works without JS.

---

## Jira sync mechanics

**Forward-only status sync:** when pulling Jira → roadmap, we map Jira status through `jiraStatusMapping` (Jira name → roadmap status). We only ever advance the roadmap status (per `config.statuses` order), never regress.

- `jiraLastKnownStatus[ticket]` — last Jira status we successfully processed. If unchanged, skip.
- `jiraSyncSkipped[ticket]` — Jira status we declined as a regression. Won't reprocess the same status; will reprocess if Jira moves forward again.

**Multi-ticket items:** an item can have up to 10 linked tickets. We pick the "best" status across them by ranking each mapped roadmap status in `config.statuses` order; the highest-ranked advance wins.

**Feature flag pull:** walks the Jira hierarchy (ticket → parent → children → grandchildren) collecting `customfield_10064` values (a label-type field; constant `JIRA_FF_FIELD` at top of `server.py`). Deduped result goes into `jiraFeatureFlags`. Union with manual `featureFlags` is computed on save.

**Release trigger:** when `update_project` sees a transition into a status where `statusIsReleased[newStatus]` is true, it pulls FF flags from the full hierarchy and saves them on the item before returning. Best-effort; failures are logged, not raised.

**Recurring item children:** items with `syncChildren: true` and a linked Jira ticket pull Jira sub-issues into roadmap items as hidden children. `_do_sync_children` handles this. When recurrence spawns a new occurrence (`POST /api/projects/{pid}/recur`), the new item starts with NO children — they get pulled fresh on the next sync, after the new item has its own Jira ticket linked.

**Push side:** PUTs that change status push to Jira via `POST /api/jira/transition/{key}` if the new status maps backward via `jiraStatusMapping` (roadmap → Jira). Comments posted to Jira describe the roadmap-side change.

---

## Frontend module map

`roadmap.html` is one big script. Major sections, roughly in file order:

| Lines (approx) | Area |
|---|---|
| 1–800 | CSS (dark + light vars, all component styles) |
| 800–1900 | HTML body: header, filters, view containers, modals |
| 1900–2050 | Constants (`SK`, `APP_VERSION`, defaults, `API` object, globals) |
| 2050–2860 | Date helpers, `applyFilters`, group/sort, **`renderCurrentView`** dispatch |
| 2860–4310 | `render()` — the Gantt renderer (the big one) |
| 4311–4560 | Jira tab UI (admin) |
| 4560–5430 | Status / type / ignore-conflicts admin |
| 5430–5760 | Capacity calendar (`renderCapCal`, modal) |
| 5760–6630 | Admin tabs: devs, users, statuses, reasons, types |
| 6630–7270 | Activity Center (`loadActivities`, `renderAcOpen`, `renderAcHistory`) |
| 7270–7960 | Auth (login modal, force-change-password, role gating) |
| 7960–8480 | View mode switching, dashboard renderer |
| 8480–9000 | Kanban renderer (`renderKanban`) |
| 9000–9700 | Planning view (`renderPlanningBoard`, session lifecycle) |
| 9700–end | Item modal, item page, modals (cap cal, release wizard, etc.) |

Use `grep -n "function <name>"` to navigate.

**Key globals (all top-level `let`):**

`projects, developers, statuses, delayReasons, products, users, types, changeReasons, deferReasons, ownerCapacity, statusIgnoreConflicts, statusIsActive, statusIsTerminal, statusIsDefault, statusIsDeferred, statusIsReleased, statusIsApproved, statusIsTesting, typeIgnoreConflicts, viewMode, collapsed, groupBy, filterDev, filterType, filterProduct, filterStatus, search, isAdmin, isViewer, currentUser, currentRole`

**The `API` object** (~line 1990) is the central HTTP wrapper. Every method adds the bearer token and `X-Team` header and parses JSON. New endpoints get a method here, not a raw `fetch()`.

`API.del` is the delete method — NOT `API.delete` (which is a reserved word in some contexts). Use `API.del`.

**Render flow:** state changes → call `render()` (Gantt) or `renderCurrentView()` (dispatches by `viewMode`). Filters live in module-level vars; `applyFilters()` reads them and returns the filtered array.

**Activities scheduling:** `scheduleCapacityConflictNotifications()` recomputes capacity conflicts and posts/clears activities after a debounce. Auto-notifications (At Risk, Needs Decision, Auto-Release) run via `runAutoNotifications()` on view load and after item edits.

**Date arithmetic:** all dates are treated as Mountain Time (`MT_TZ = 'America/Denver'`). `todayMT()` returns today in MT. `addDays`, `startOfW`, `nextMonday` are the workhorses. Store/return YYYY-MM-DD strings; never store time-of-day.

---

## Audit log

Server-rendered HTML page at `/audit` (admin only). Every dynamic value passes through `html.escape`. Search filter is included in the query string and also escaped on render. Action types are color-coded via the `ACTION_COLOR` dict.

`write_audit(team, action, username, project_id=None, project_name=None, changes=None)` is the central writer. `changes` is a free-form dict; for field updates, use the convention `{ field: {"from": old, "to": new} }` so the renderer shows the diff.

---

## Rate limiting

`server.py` has an in-memory rate limiter for the login endpoint. It's keyed by IP. Limits reset on process restart. There's a known issue: the dict isn't pruned, so it grows over time. Acceptable for a small-team internal tool; would need to be cleaned up if exposed publicly.

---

## Things that have bitten us before

- **Route ordering** for `/planning-sessions/active` vs `/planning-sessions/{session_id}` — literal MUST come first.
- **`API.delete`** vs **`API.del`** — `delete` shadows the reserved word in some JS contexts; we standardized on `del`.
- **`event.stopPropagation()` in inline `onclick`** — doesn't always prevent the document-level handler. Use proper `addEventListener` with capture if you need to.
- **Closing dropdowns** — every dropdown that opens with a button outside a `closeAllDropdowns()`-managed list must be added to that list, or it won't close when other dropdowns open.
- **Single-quote-in-onclick** — strings stored in HTML `onclick=""` attributes break if they contain unescaped single quotes. Use delegated handlers via `window._dashActions` and a single root listener instead.
- **`applyViewMode()` reads `viewMode` global, not `viewSelect.value`** — set the global before calling apply, then sync the select.
- **Capacity health classes count resource units, not items** — `capacityHealthClass` must use `parallelResources` sums, not item counts.
- **`offset_days` in recurrence** was dead code — gone, but watch for it sneaking back. Related: recurrence spawn uses `prev_start + period`, NOT `date.today()`.
- **In-context dict as set key** — Python sets can't hold dicts; if a Jira field comes back wrapped, unwrap before deduping with `set()`.
- **`forcePasswordBg` z-index** — must be higher than `loginWall` (which is 1000). Past bug: force-change modal was hidden behind the login wall because it inherited `z-index: 200` from `.modal-bg`.
- **Hardcoded status names in spawn_recurrence** — past bug: new occurrences got `status="Planned"` regardless of team config. Always resolve through `statusIsDefault`.
- **Status color classes** — `statusCls()` returns CSS classes based on `statusIs*` flags (in priority order: Released → Testing → Approved → Deferred → Terminal → Active → Default), NOT based on the status's position in the list. Don't reintroduce positional fallbacks for flagged statuses.
- **`__main__` block placement** — must live at the bottom of `server.py`, not in the middle. Past bug: a refactor left it before some endpoint definitions, causing the server to not register those routes when run directly.
- **`originalValues` snapshot must include `description`** — Jira description-sync diff compares `originalValues.description` against `saved.description`. If you add a new field that should sync to Jira, snapshot it at modal open too.
- **Auto-save Jira links** — adding/linking a Jira ticket from the modal must persist immediately (no Save click required). The user expects it.

---

## Known limits and open issues

- **In-memory rate limiter dict isn't pruned** — small leak; restart clears it. Fine for current scale.
- **CORS is permissive by default** — production should set `CORS_ORIGINS` to a real allowlist.
- **No tests** — there is no pytest suite or playwright harness. Changes are verified by syntax check + manual smoke. If introducing tests, treat it as a project decision, not a drive-by.
- **No multi-process safety on SQLite WAL** — gunicorn runs 2 workers against the same DB file. SQLite WAL handles concurrent reads well; concurrent writes are serialized. Bumping worker count beyond 4 starts to risk contention.
