# CLAUDE.md

This file gives Claude Code the context it needs to be useful immediately. Read it before touching code. See `ARCHITECTURE.md` for deep references, `DEPLOYMENT.md` for production operations, and `INTAKE-PORTAL.md` for the public ticket portal's deep-link (prefill) parameters + testing guide.

---

## What this project is

**Frazil Roadmap** (now branded **Frazil Flow**) is an internal, multi-tenant team roadmap / Gantt / Kanban / planning tool. It is live in production at `https://flow.frazil.app` (running on EC2; the legacy `https://roadmap.frazil.app` 301-redirects to it via Caddy). Current version is **4.32.1** (set in both `server.py` `APP_VERSION` and `roadmap.html` `const APP_VERSION`).

The whole app is **two files**:

- `server.py` — FastAPI + SQLite backend (~4,700 lines)
- `roadmap.html` — entire frontend: HTML, CSS, and JS in one file (~20,200 lines)

No build step. No bundler. No npm. The HTML file is served directly by FastAPI at `/`. This is intentional — it keeps deploy to a single `scp` of two files.

**The "Flow" shell is now the primary UI, served at root (`/`).** It began in 4.0.0 as an opt-in `/beta/*` left-rail surface alongside the classic top-bar UI, then was **promoted to the default at root in 4.8.0** (URL cleanup in 4.8.1; legacy `/beta/*` now **301-redirects** to the equivalent root path). The classic top-bar UI still exists but is now **opt-in via `?classic=1`**. See **"The Flow shell"** section below before touching it.

---

## The Flow shell (read before editing it)

The Flow shell is an **additive** left-rail UI built on top of the original app. It is **one** appended `<script>` + `<style id="frzBetaStyle">` block at the end of `roadmap.html`, plus a handful of additive routes/endpoints in `server.py`. (Historical naming: the code, CSS class `.frz-beta`, and `frzBeta*` identifiers all date from when it lived at `/beta` — they were kept on the root promotion to avoid a churny rename.)

**Activation (current, post-4.8.0):** Flow is the **default** UI. `boot()` (roadmap.html ~`async function boot()`) activates the shell at root and normalizes URLs — legacy `/beta/*` → root (the server also 301s), bare `?item=ID` → `/item/ID`. The classic top-bar UI is **opt-in via `?classic=1`** (also left alone for `?pwtoken=` set-password links). When the shell mounts it tags `<body>` with **`.frz-beta-active`**; the classic root (no `?classic=1`, so no `.frz-beta-active`) is left untouched. Many runtime branches still key off `document.body.classList.contains('frz-beta-active')` to decide Flow-vs-classic behavior.

**Hard rules (these were agreed and enforced across the whole build):**
- Additive only — do **not** change production routes, components, styles, or behavior. The shell reuses the five production views by **re-parenting their DOM containers** into `#frzContent` and **wrapping** global render functions (`window.openItemPage`, `window.renderKanban`, `window.renderPlanningBoard`), never by editing the views' own code.
- All shell CSS is scoped under a single root class **`.frz-beta`**; no `!important`. The one exception is the `@mention` typeahead menu (`.frz-mention-menu`), which is appended to `<body>` (above the shell) so it is intentionally unscoped.
- Accent is **`#0059A9`** (`--frz-accent`). **Zero indigo** — the production `--accent` is indigo; reused item-page markup is de-indigo'd at runtime by `_debrandItem()`. Item **status & priority badge colors are the production colors, untouched.**
- The shell reaches production globals from its own (second) classic script via helpers near the top of the beta module: **`_call(name, ...args)`** (invoke a global fn), **`_val(name, dflt)`** (read a top-level `let`/`const` via `eval`), **`_g(name)`**. Top-level `let`/`const` from the first script ARE visible to the second script's `eval` (shared global lexical scope) — this is how `_val('projects')`, `_val('_listSelected')`, etc. work.

**Routing** (`navigate`/`routeFromLocation`/`syncURL` in the beta module), all at **root**: `/{gantt|kanban|list|planning|dashboard|my-home}`, `/item/:id` (deep-linkable item page), `/my-home/{assigned|recent|watching}` (My Home sub-tabs), `/planning/{sprints|releases}` (planning sub-tabs), `?board=` (kanban board scope), `?project=` (project scope). URLs are the state. Legacy redirects: `/beta/*` → root (server-side 301 + client-side normalize); `/my-work` → `/my-home` (client-side, Stage 5 absorbed My Work into My Home).

**Features shipped in the shell (4.0.0 → 4.3.1) — the foundational set:**
- **Custom Kanban boards** — config key `boards`; per-board columns→statuses mapping with a drop-status; column ↑/↓ reorder.
- **Planning rework** — tabbed Plan / Sprints / Releases. Sprints (config key `sprints`) with a readiness modal; Releases (config key `releases`) with progress bars.
- **Ranked statuses + readiness floor** — `STATUS_META` in `roadmap.html` is the **single source of truth** for status rank + terminal-ness (Released is the only terminal status; Inactive is a non-terminal parking lot with no main-flow rank). Helpers `statusRank`/`isTermStatus`/`needsFloorPromotion`. Release progress + sprint completion read terminal-ness from here. (This is the beta-side ranking; the production status-flag config maps from rule 3 still apply.)
- **Multi-sprint history** — `p.sprintHistory = [{sprintId, addedAt, outcome}]`; reconstructed from the activity log for legacy items (`_parseSprintActivities`).
- **My Work** (4.0.0; superseded — see **My Home** below) — was a rail entry at `/beta/my-work` showing the current user's non-terminal items. Stage 5 (see post-4.3.1 list) absorbed it into **My Home** (`/my-home`); the old route now redirects.
- **Notifications + @mentions + watchers** — bell/inbox in the top bar; `@` typeahead in comments + the description (works in both the classic input and the Tiptap editor — see Rich-text editor); Watch/Unwatch on the item page. **Server-generated** (see below).
- **Rich-text editor (Tiptap) — Description + Comments + threads (4.2.0–4.3.1).** Description and the comment composer are a Tiptap/ProseMirror editor; comments support single-level reply threads. See the dedicated **"Rich-text editor"** section below before touching it.
- **Tabbed Settings** — rail Settings opens a tabbed panel; tab 1 is the **relocated** My Account content; Notifications + Admin tabs are stubs (Admin opens the existing Admin panel).
- **List multi-select → release linking** — "Add to release" in the list bulk bar; links each selected row via the shared `linkItemToRelease(p, rid)` (the same one the item-page Release field uses).
- **Attachments (S3) — DONE (KMS unblocked mid-June 2026; Jira sync 4.9.11–4.9.12).** Presign/upload/list/delete endpoints + item-page Attachments UI + inline images are live. Direct presigned PUT to S3 (`frazil-flow-attachments`, us-west-2, SigV4, **SSE-KMS** via a dedicated key, 50 MB max, private + Block Public Access). Inline editor images reuse the same pipeline (stored as `<img data-att-key>`, src resolved at render). Phase C (4.9.11–4.9.12) added **auto-sync of item attachments to the primary Jira ticket** (`POST /api/items/{pid}/attachments/sync-jira` → `sync_attachments_to_jira`).

**Shipped post-4.3.1 (4.4.0 → 4.9.26) — what the foundational list above predates:**
- **List detail panel (4.4.0–4.5.1)** — clicking a List row opens a side detail panel (interactive: comments + status), `?panel=` deep-link/cold-load restore, responsive overlay; Kanban card → modal; rich Notes in the item modal (`#frzModalNotes`).
- **Filter-travels + URL scoping (4.6.0–4.7.0)** — filters persist across views (chip parity), project carried in the URL (`?project=`), view-aware UI.
- **Rebrand + root promotion (4.7.x–4.8.2)** — "Roadmap" → **"Frazil Flow"**; **Flow promoted to default UI at `/`** (4.8.0), `/beta` → root URL cleanup (4.8.1), legacy `/beta/*` redirect flipped 302 → **301** (4.8.2).
- **Domain cutover (4.9.0)** — production moved `roadmap.frazil.app` → **`flow.frazil.app`** (Caddy 301s the old domain; `APP_BASE_URL`/`CORS_ORIGINS` updated).
- **PWA rebrand (4.9.1–4.9.3)** — new f-mark icons + `theme_color` → Flow accent.
- **Rich-text Jira sync, Phase C (4.9.4–4.9.12)** — Description toolbar polish; Jira description capture → formatting-preserving push (basic subset; rich blocks local-only); **auto-sync attachments to Jira**.
- **Release planning (4.9.13–4.9.26)** — add/remove items on the Releases tab, post-release lock, **release notes** (generate/edit/store/copy + dirty-guard), per-type icons, release deep-link (`?release=`), Jira-independent feature-flag links.
- **My Home (Stage 5)** — personal landing at `/my-home` that **absorbed My Work**; three tabs: **Assigned to me** (assignee == current user, non-terminal, sorted priority → due → name, reusing `_listRowHtml`), **Recent** (backed by `GET /api/my/recent`), **Watching** (backed by `GET /api/my/watching`). Rail entry navigates here; `/my-work` redirects in.
- **List columns (4.9.22–4.9.26)** — model-driven List render; user-reorderable columns (↑/↓ in the Columns picker, persisted per-user via `frazil_beta_listorder_<user>`); added Project/Description columns; Gantt inline legend; CSV export follows display order.
- **List filter-aware pills + resizable columns (4.10.1)** — rail count pills reflect the active filter; Name/Description are drag-resizable (widths per-user in `frazil_beta_listwidths_<user>`). ⚠ **KNOWN DEFERRED BUG:** the PROJECTS count pills only *approximate* assignee/`q` filters (inert until Assignee is used) — full writeup + fix in `design/flow-road-off-jira.md` item #10.

**Shipped 4.10.0 → 4.32.1 (this list predates them):**
- **Public intake portal (Tier 2)** — unauthenticated ticket submission at `/report`, token-gated status pages (`/ticket`), and a cross-team reporter list (`/my-tickets`). Server-rendered in `server.py`; config keys `intakeEnabled/Types/Projects/NotifyEmail/Domains/ProjectEmails`, Cloudflare Turnstile, SES emails. **Full guide in `INTAKE-PORTAL.md`.** Terminology: reporter-facing says "ticket", internal says "Item" (see rule below).
- **Departments (4.28–4.30, COMPLETE)** — admin-managed per-team departments with per-dept color + notify emails (config key **`departmentMeta`**). Dept-notify emails fire on portal submit (deduped vs reporter/team), and a **no-login department queue** lives on `/my-tickets?team=&dept=` (gated to the email's dept membership; anti-enumeration 404). Plan: `design/department-feature-plan.md`.
- **Portal ticket readability (4.31)** — reporter **descriptions store as safe HTML** (`html.escape(text).replace("\n","<br>")`) so line breaks persist through the app's `innerHTML`/Tiptap round-trip and show in emails. Public pages/emails render the description **as-is** (don't re-escape). Portal **status pills mirror the logged-in badge** via `_status_color`/`_status_cls` (replicates `statusCls`: 7 flags in priority order + positional fallback; returns the exact `.s-*` swatch). Portal pages carry `/favicon.png`.
- **Mobile companion UI (4.10.0, extended 4.32.x)** — phone-only (`@media (max-width:640px)`), desktop-inert. Bottom tab bar + FAB; **item taps open the item PAGE, not a modal** (guarded in `openProjectModal` + the List panel interceptor; New Item stays a sheet); the left menu is a **full-screen scrolling page** with an X to close. ⚠ **CSS gotcha:** the full-screen rail rule must match the specificity of the `.frz-narrow` drawer rule (~L15714) or the rail stays a 232px drawer under the scrim. New Item modal fields stack via `.form-grid{grid-template-columns:1fr}`.

> **Current version is 4.32.1** (keep `server.py APP_VERSION` and `roadmap.html const APP_VERSION` in sync). The "4.9.26" in the intro paragraph above is stale historical text.

**New `server.py` surface (all additive):**
- Shared config-backed endpoints (any authed user): `GET/PUT /api/boards`, `/api/sprints`, `/api/releases` (config keys, NOT `VALID_KEYS`).
- Notifications/watchers: `GET /api/notifications`, `POST /api/notifications/read`, `GET /api/items/{id}/watchers`, `POST /api/items/{id}/watch` / `/unwatch`. Two new per-team tables: **`notifications`** (private per-user inbox) and **`watchers`** (kept OUT of the item blob, which `update_project` replaces wholesale). Both `CREATE TABLE IF NOT EXISTS` in `init_team_db`.
- Attachments (live): `POST /api/items/{id}/attachments/presign`, `POST /api/items/{id}/attachments`, `GET …/attachments`, `DELETE …/attachments/{attId}`, plus `POST /api/items/{id}/attachments/sync-jira` (`sync_attachments_to_jira`).
- **Notification generation is server-side**, hooked into `add_comment`, `update_project`, `create_project`, `commit_planning_session`, and `jira_pull_sync`. Every hook runs **after** the primary write commits and is wrapped best-effort (a notification failure can never fail/roll back the mutation). Self-notifications are suppressed; bulk Jira sync (`jira_pull_all`) sets `_suppressNotify` to avoid a backfill burst.
- **Rich-text editor + comment threads (server side):** config flag **`richTextEditor`** (bool, default ON, in `VALID_KEYS` + `init_team_db` defaults + `_migrate_config_keys` presence-only seed + `get_all` returns the bool) — the master switch for the Tiptap editor. **`comments.parent_id`** (idempotent `ALTER TABLE` in `init_team_db`; NULL = top-level): `add_comment` accepts `parent_id` and **normalizes reply-to-reply to the thread root** (single level guaranteed); `_notify_on_comment` notifies the parent comment's author on a reply (new `"reply"` notification type, self-suppressed); `delete_comment` **cascade-deletes replies**. Comment bodies store sanitized rich HTML (`add_comment` stores verbatim — sanitization is client-side on save; mention notifications still resolve because the mention chip carries the literal `@name`).
- **Natural `item_key` List sort:** `list_items` (`GET /api/items`) sorts the `item_key` column **numerically** — text prefix (case-insensitive) then the trailing integer as a number, computed before `LIMIT/OFFSET` so it's correct across pagination (FRAZ-1, FRAZ-2, … FRAZ-10, not FRAZ-1, FRAZ-10, FRAZ-2). Blanks last in both directions. Only the `item_key` sort branch; literal SQL, whitelisted column + sanitized direction. Shared endpoint → also fixes the classic `/` List.

**Logo assets** are base64-embedded data URIs in the beta module (`FLOW_LOGO_FULL`/`FLOW_LOGO_MARK`) — there is no static file serving, consistent with the two-file deploy.

---

## Rich-text editor (Tiptap) — read before editing Description/Comments in the Flow shell

The item-page **Description** and **comment composer** (incl. reply threads) use a **Tiptap/ProseMirror** editor, beta-only, behind the **`richTextEditor`** config flag (default ON). It lives entirely in the beta module of `roadmap.html`. Flag OFF / viewer / CDN-load failure → the classic lightweight `.rte-box` editor + `#ipCommentText` input, unchanged.

**Loading (no build step):** Tiptap is loaded as **ESM from CDN**, pinned — `@tiptap/*@2.27.2` via `esm.sh`, `dompurify@3.1.6` UMD via jsDelivr — by `_frzLoadTiptap()` / `_frzLoadDOMPurify()` (lazy, cached). There is **no UMD build of a full Tiptap editor**; ESM-from-CDN is the only no-build path. `esm.sh` is therefore a **prod runtime dependency for editing** (acceptable for beta; "self-host the editor bundles before real rollout" is on the record). A CDN/import failure degrades gracefully to the classic editor (never a blank box).

**Shared factory:** `_frzMakeExtensions(T)` builds the extension set + custom nodes used by BOTH the description and comment editors (one definition, no fork). Custom nodes: **Mention** (`span.frz-mention[data-u]`), **Image** (extended with a durable `data-att-key`), **Highlight** (`mark.frz-hl`), **InfoPanel** (`div.frz-panel[data-variant]`), **Expand** (`details.frz-expand[data-title]` + `summary` + `div.frz-expand-body`). `_frzBuildToolbar(ed, opts)` is config-driven (`opts.buttons` set + `opts.place` callback): the description uses the full set in the card header; comments use a **trimmed** set (`_FRZ_TB_COMMENT`: bold/italic/strike, lists, link, image — no blocks).

**Storage = sanitized HTML** through a strict **DOMPurify allowlist** (`frzSanitize` / `_FRZ_SANI_CFG`), applied on save. Explicit tag/attr allowlist; **no inline `style`** (so pasted colors/fonts/table-widths are dropped — intentional; tables are structure-only), no scripts/handlers, `data:`/`blob:` blocked, `class` filtered to a set (`frz-panel/frz-expand/frz-expand-body/frz-mention/frz-hl`), `iframe` **host-gated** to a video allowlist (YouTube/Vimeo/Loom). Old plain/HTML descriptions + comments render unchanged.

**THE round-trip rule (hard-won — the "vanish on refresh" bug):** every custom node's **`parseHTML` must match the exact stored/sanitized form** and its **`renderHTML` must re-emit it**. A parse/render mismatch silently drops the node on reload, and the next save persists the loss. Examples already burned in: the Image node matches `img[data-att-key]` (not just `img[src]`) because stored images are **src-less** (the presigned `src` is disposable, resolved at render); panels/expand carry `data-variant`/`data-title`. Verify any node change with a **two-cycle** round-trip (insert → getHTML → sanitize → setContent → ×2 → identical).

**Inline images** reuse the existing S3 attachment pipeline (`uploadAttachment`) — no second path. Stored as `<img data-att-key>` (no `src`); `_frzRehydrateEditorImages` (editor) / `rehydrateInlineImages` / `_frzRehydrateCommentImages` (rendered) resolve the key → a fresh presigned `src` at display; `src` is stripped again on save. **@mentions** reuse the existing menu (`_openMentionMenu`/`_mentionUsers`) + the document-capture keydown (`_wireMentionKeys`, installed by `_frzWireMentions`).

**Jira description sync:** push a **basic subset only** (bold/italic/lists/links/code/quote/headings); rich blocks (tables/panels/expand/images) are **local-only** — `_frzStripLocalOnly` removes them and change-detection runs on a **normalized basic-subset string** (`_frzBasicSubsetText`), so editing only a local-only block produces **no phantom Jira diff**.

**Comment threads (single-level):** `renderItemPageComments` delegates to **`_frzRenderThreads`** on `frz-beta-active` (classic stays flat) — top-level **newest-first**, replies nested + **chronological**, expanded by default with a collapse toggle. The reply composer reuses the shared editor (one open at a time) and posts with `parent_id`. The composer sits at the **top** of the thread (`_frzPlaceComposerTop`, re-applied on every render). `_renderCommentBody` renders a rich body (one that starts with a known block tag) sanitized inside `.frz-rte`; plain bodies keep the classic escape + `@mention` path. See the server side under "New `server.py` surface".

**Throwaway spikes** `spike.html` (editor + tables torture test) and `spike-sanitize.html` (allowlist auto-asserts) are uncommitted dev artifacts — not deployed, not part of the two-file app.

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
TOKEN_SECRET=<long random hex>     # signs auth tokens. If absent, a secret is persisted
                                   # to .token_secret next to server.py (shared across
                                   # workers, survives restart). Set explicitly in prod.
CORS_ORIGINS=https://flow.frazil.app   # prod leads with flow; roadmap + localhost also kept
DEBUG_TRACEBACKS=0                 # 1/true to echo Python tracebacks in 500 responses
                                   # (default off — tracebacks are always logged server-side)

# Email (Amazon SES via boto3 + EC2 instance role) — password reset + invites.
# No SMTP creds / AWS keys: SES auth comes from the instance role (matches the
# dashboard & sharebox apps). The instance role needs ses:SendEmail/SendRawEmail.
MAIL_FROM=notifications@frazil.app
AWS_REGION=us-west-2
APP_BASE_URL=https://flow.frazil.app   # base for emailed reset/invite links (canonical domain)
```

Add a team: `python server.py --new-team acme` (prints an initial admin password and forces a change on first login).

---

## Testing

There is now a backend test suite under `tests/` (pytest + FastAPI `TestClient`). It is **not** shipped to production and has no bearing on the two-file deploy.

```bash
pip install -r requirements-dev.txt   # pytest + httpx (one-time)
pytest                                 # runs tests/ (scoped via pytest.ini)
```

How isolation works (see `tests/conftest.py`):
- `server.py` reads `TENANTS_DIR` from the `FRAZIL_TENANTS_DIR` env var (default `/data/tenants`). The suite points it at a throwaway temp dir **before** importing `server`, because `boot()` runs at import time. Real tenant data is never touched.
- `TOKEN_SECRET` is pinned and Jira creds are blanked in `conftest.py` before import.
- Each test gets a fresh uniquely-named team. Role-gating/business-logic tests mint tokens directly via `server.create_token(...)` to skip the login rate limiter; the login flow itself is covered in `test_auth.py`.

Current coverage (`tests/`, ~242 tests, all green): liveness; auth + role gating + login rate limit; the `testWeeks >= dueWeeks` 422; `parallelResources` rounding (create *and* update) + active-status lock; config `VALID_KEYS` allowlist + `/api/all` shape; capacity overrides (upsert/validation/ceiling/batch/delete/effective resolution); planning sessions (lifecycle, payload validation, and atomic commit applying Review/Sprint/Release/deferral status changes through the config-driven status-flag maps); and the `/beta` server surface — boards/sprints/releases endpoints + validation, attachments (size guard, filename/key shaping, auth, record/list/delete), notifications (mention/assign/status/comment generation, self-suppression, mark-read, watch/unwatch, per-user privacy, and the watchers-survive-a-full-blob-PUT regression guard), the **`richTextEditor`** flag (default-on, admin-gate, presence-only migration), **comment threads** (`parent_id` + root normalization, reply-notifies-parent self-suppressed, mention-in-reply, cascade delete), and the **natural `item_key` List sort** (asc/desc order + correctness across pagination). Extend it when you touch those areas. Run `pytest` before any deploy.

Frontend-only behavior (the Tiptap editor, sanitizer allowlist, node round-trips) has no pytest coverage — it was verified headlessly during the build via jsdom + the pinned packages (a throwaway harness, not committed). Re-verify node round-trips that way when changing the editor.

Not yet covered: Jira sync (would need HTTP mocking of `urllib`), recurrence spawning, plain comments/activities, and the S3 attachment happy-path (needs live creds/mock — the feature is live now, but the upload round-trip still isn't exercised in tests). Good next targets.

---

## PWA (installable standalone app)

The app is an installable PWA. To preserve the two-file deploy, **all PWA assets are served as FastAPI routes** (no static files):

- `GET /manifest.webmanifest` — web app manifest (`_PWA_MANIFEST` dict in `server.py`)
- `GET /sw.js` — service worker (`_PWA_SW_TEMPLATE`; `__APP_VERSION__` is substituted at request time so the cache name tracks `APP_VERSION`)
- `GET /icon-192.png`, `/icon-512.png`, `/apple-touch-icon.png` — PNG bytes base64-embedded in the `_PWA_ICON_*_B64` constants

`roadmap.html` links the manifest + apple/theme-color meta tags in `<head>`, and registers `/sw.js` at the end of the single `<script>` block.

**Caching strategy (deliberate):** network-first for the app shell / navigations so a `roadmap.html` deploy goes live immediately (consistent with "HTML changes need no restart"); cache-first only for our own icons/manifest; **`/api/*` is network-only** — there is no offline data layer (it would conflict with the server-validated planning/snapshot conflict model). Don't make the service worker cache API responses without a deliberate redesign.

**Regenerating icons:** `python tools/gen_pwa_icons.py` (needs `pip install pillow` — Pillow is **dev-only**, NOT a runtime dependency). It redraws the brand map-pin and rewrites the three `_PWA_ICON_*_B64` constants in `server.py` in place. Run the JS/Python syntax checks after. Deploy is still just `scp server.py roadmap.html`.

---

## Auth emails: password reset & user invites (Amazon SES via SMTP)

Self-service password management, sent through SES via **boto3 + the EC2 instance IAM role** — the same pattern as the `dashboard` and `sharebox` apps (no SMTP creds, no stored AWS keys). `boto3` is the one allowed runtime dependency beyond the stdlib here, justified by fleet consistency + role-based auth (it's lazily imported, so server.py still loads without it in dev/test). Config via `MAIL_FROM` / `AWS_REGION` / `APP_BASE_URL`; **degrades gracefully** when boto3 is absent (forgot-password no-ops, `send-invite` → 503) or the role lacks SES permission (`send-invite` → 502 with the SES error).

- **Users have an `email` field** (admins populate it). Emails are validated + **unique per team**, because **login accepts username *or* email**.
- **Reset/invite tokens** reuse the HMAC signing infra (`make_password_token` / `decode_password_token`) and are **bound to the user's current password hash** → single-use (a link dies once the password is set/changed). No DB table.
- Endpoints: `POST /api/forgot-password` (public, rate-limited, **uniform response** — no email enumeration), `POST /api/reset-password` (sets pw via token), `POST /api/users/{username}/send-invite` (admin → emails a 7-day setup link; reset links are 1 hour).
- Frontend: "Forgot password?" on the login wall; a `?pwtoken=…` set-password screen (handled first thing in `boot()` via `checkPasswordTokenParam()`); Add-User offers **"Set password now" vs "Email a setup link"**; per-user mail button re-sends a link; a **pending** badge marks users with no password yet.
- New-user "send link" path creates the user with **no password** (a pending account that can't log in until the link is used), then calls `send-invite`.

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

**Current `VALID_KEYS`** (the `PUT /api/config/{key}` allowlist, `server.py` ~line 1961):
- **Shared lists:** `developers`, `statuses`, `delayReasons`, `products`, `users`, `types`, `departments`, `changeReasons`, `deferReasons`
- **Capacity & scheduling:** `ownerCapacity`, `typeScheduled`
- **Status flags** (config-driven, see rule 3): `statusIsActive`, `statusIsTerminal`, `statusIsDefault`, `statusIsDeferred`, `statusIsReleased`, `statusIsApproved`, `statusIsTesting`, `statusIsBlocked`
- **Conflict ignores:** `statusIgnoreConflicts`, `typeIgnoreConflicts`, `productIgnoreConflicts`
- **Jira:** `jiraEnabled`, `jiraSyncConfig`, `jiraProjectMapping`, `jiraStatusMapping`, `jiraTypeMapping`
- **Editor:** `richTextEditor`

> `boards`, `sprints`, and `releases` are config-table-backed too but are **NOT** in `VALID_KEYS` — they have dedicated endpoints (`GET/PUT /api/boards|sprints|releases`), not the generic config route.

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
- A backend pytest suite now exists under `tests/` (see the **Testing** section). Don't add a *new* test framework (e.g. playwright/frontend e2e) unprompted — extend the existing pytest suite instead.

These are real choices we've made, not oversights. If something looks like it would benefit from one of these, flag it and ask first.

---

## Style notes

- Comments use `# ── Heading ─────...` boxes for major sections in `server.py`. Match the style for new sections.
- Server code is generally 4-space indent, type hints where convenient but not exhaustive.
- Frontend uses 2-space indent. Function names are camelCase.
- Error messages should be specific: `"Test period (3w) cannot equal or exceed the time estimate (3w)"` not `"Invalid"`.
- **No em dashes anywhere** (neither the U+2014 character nor the `&mdash;` entity), in UI copy, page text, emails, comments, or docs. Use a spaced hyphen (` - `), a colon, or reword.
- **Never render the legacy indigo accent** (`#5b4fff` / `rgb(91,79,255)` / `#7b6fff` / `#4a3de0`). Use Frazil blue `#0059A9` (dark `#2F86DE`) via `var(--accent)`/`var(--frz-accent)`.
- **Terminology by surface:** external/reporter-facing (portal, `/ticket`, `/my-tickets`, emails) says "ticket"; internal staff UI (item page, modal, Kanban, List, notifications) says "Item".

---

## Deployment cheat sheet

Full details in `DEPLOYMENT.md`. The fast version:

- Canonical domain: `https://flow.frazil.app` (Caddy 301-redirects legacy `roadmap.frazil.app` → flow, path+query preserved)
- Production host: `ubuntu@52.35.224.183` (Elastic IP), EC2 `t4g.small`, Ubuntu 24.04 ARM64
- App path: `/opt/roadmap/`
- DB path: `/data/tenants/{team}/roadmap.db` (separate EBS volume at `/data`)
- Reverse proxy: Caddy at `/etc/caddy/Caddyfile`, auto-SSL via Let's Encrypt
- Service: systemd unit `roadmap.service` running `gunicorn server:app -w 2 -k uvicorn.workers.UvicornWorker --bind 127.0.0.1:8000`
- Deploy: `scp server.py roadmap.html ubuntu@52.35.224.183:/opt/roadmap/`
- HTML changes need NO restart (Caddy reads it fresh). `server.py` changes need `sudo systemctl restart roadmap`.
- Logs: `/var/log/roadmap-access.log`, `/var/log/roadmap-error.log`, plus `journalctl -u roadmap`
