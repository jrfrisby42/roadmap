# Multi-Team Accounts / Cross-Team Identity (Option B) — Design Plan

> Status: **PLAN ONLY — not yet built.** Approved approach; implementation deferred.
> Supersedes the client-side team switcher (Option A, shipped in 4.34.0) as the eventual
> model. Author: planning session 2026-07-20.

## Context

Today identity is **strictly per-team**: a "user" is a record inside each team's own
SQLite DB (`config` key `users`), with its own password and role; the auth token is
stamped to one team and every request's `X-Team` header must match it (`require_auth`,
`server.py:406-443`). There is **no global user store** (verified — the only DBs are
`/data/tenants/{team}/roadmap.db`).

Option A (client-side switcher, 4.34.0) lets one person sign into each team once and hop
between them. Option B is the real thing for **many/all users**: one identity
(email + password), membership in multiple teams with per-team roles, a switcher backed by
that identity, and eventually **no team field on login**.

### Decisions locked with the user
- **Admin model: Both** — team admins manage their own team's members by email, **plus** a
  new org **super-admin** who can manage all teams/memberships.
- **Credentials: carry over one password** — auto-link existing per-team users by email;
  seed the global password from one existing record (deterministic pick), discard the rest.
  No forced reset.
- **Email required** — email becomes the global identity; users without one are backfilled
  before they migrate (pre-flight report lists them).
- **Rollout: parallel, then cut over** — ship global auth alongside today's per-team login
  behind a flag, validate with a couple teams, then flip the default and remove the team
  field. Reversible.

## Core architecture (the key simplification)

**Do NOT change the per-request token model.** Instead:
1. A new **global auth** authenticates email+password **once** against a shared accounts
   store and returns the person's teams (from membership).
2. A **switch** step verifies membership for the target team (the authoritative trust
   anchor) and mints a **normal per-team token** via the existing `create_token(team,
   username, role)` (`server.py:337`). `require_auth`/`require_role`/`X-Team` stay
   **unchanged**.

This makes multi-team access secure (membership gates every team entry) while touching the
smallest possible surface, and it composes with the Option A switcher UI already built
(`renderTeamSwitch`/`switchTeam`, `roadmap.html:7862-7913`) — repoint that UI at the global
session instead of the local `frazil_sessions` map.

## Data model — new shared store `accounts.db`

New SQLite DB, sibling to the tenants (reuse the `sqlite3` stack + the `.token_secret`
"shared process artifact" precedent, `server.py:308`; **no new deps**). Path:
`ACCOUNTS_DB = os.environ.get("FRAZIL_ACCOUNTS_DB") or os.path.join(TENANTS_DIR, "_accounts.db")`.
The leading underscore keeps it out of `valid_team` (`^[a-z0-9]+$`, `server.py:187`) and
`/api/teams` enumeration (`server.py:1147`); tests auto-isolate it since they pin
`FRAZIL_TENANTS_DIR`. Add an `_accounts_db()` context manager mirroring `db()` (`:499`).

Tables:
- `accounts(email TEXT PRIMARY KEY, password TEXT, must_change INTEGER DEFAULT 0, is_super INTEGER DEFAULT 0, revoked_at TEXT, created_ts TEXT)` — email lowercased; the single global credential.
- `memberships(email TEXT, team TEXT, role TEXT, owner_filter TEXT DEFAULT '', PRIMARY KEY(email, team))` — source of truth for "who's in team X and their role".

Per-team `users` config **stays** (avatar/initials/color, display, legacy login during the
parallel phase). Membership/role/credential *authority* moves to `accounts.db`; a thin sync
keeps the per-team `users` record present (for avatars) when a member is added.

## Backend changes (`server.py`) — all additive during parallel phase

Reuse verbatim (already team-agnostic): `hash_password`/`verify_password`/`is_hashed`
(`:204-230`), `make_password_token`/`decode_password_token` (`:370-389`),
`send_email`/`mail_configured` (`:265-290`), `_check_rate_limit` (`:914`).

New auth surface:
- `create_account_token(email)` / `decode_account_token` — HMAC-signed via existing `_sign`
  (`:334`); short-ish TTL; identity = email. The **global session token**.
- `POST /api/auth/login` `{email, password}` (public, rate-limited, anti-enumeration
  mirroring `login` at `:2157`): verify against `accounts.db`; return
  `{accountToken, teams:[{team,role}], isSuper, mustChange}`. Uniform failure response.
- `POST /api/auth/switch` `{team}` (requires a valid account token): look up
  `memberships(email, team)`; **403 if not a member** (the security anchor); else mint a
  per-team token with `create_token(team, per_team_username, membership_role)`.
  `per_team_username` from the team's `users` record (kept in sync); role from membership.
- `require_account(...)` dependency (decode account token + honor `accounts.revoked_at`);
  `require_super_admin(...)` (account + `is_super`). New privileged surface → security review.

Membership management (admin model = Both):
- Team-admin, team-scoped: `GET/POST/DELETE /api/teams/{team}/members` (+ role change),
  gated by `require_role("admin")`. Add-by-email: create the global account if new
  (invite/set-password link via `make_password_token`), else link; write `memberships` +
  ensure a per-team `users` record exists. Reuses the invite path (`send-invite`, `:2399`).
- Org super-admin: `/api/admin/accounts` + `/api/admin/memberships` (org-wide), gated by
  `require_super_admin`. Seed the first super-admin at migration (owner email) or a
  `RESET_SECRET`-style break-glass (mirror `:2608`).

Keep the existing per-team `POST /api/login` **unchanged** for the parallel phase. Extend
`_is_user_revoked` (`:391`) usage so a revoked global account can't login/switch (checked
at login/switch; the ≤24h per-team-token window is the same tradeoff as today).

## Migration (`--build-accounts` CLI, mirroring `--new-team` at `server.py:34`)

Idempotent, additive (never mutates per-team `users`; only writes `accounts.db`), **dry-run
report first**:
- Enumerate teams (`os.listdir(TENANTS_DIR)`, the `boot()` pattern `:899`); for each team's
  `users`, for each user **with an email**: upsert `accounts(email)` — if new, carry over
  that record's password hash (first-seen wins under a deterministic team order); always
  add `memberships(email, team, role, owner_filter)`.
- Users **without an email** → listed in the report, **skipped** (backfill required); keep
  legacy per-team login during parallel phase.
- Same email across teams → one account (first password kept), all memberships attached.
- Seed `is_super` for the owner email (`jr.frisby@frazil.com`) via a CLI flag.
- Note: `--new-team` currently hardcodes `/data/tenants` (ignores `TENANTS_DIR`, `:45`); the
  new CLI must use `TENANTS_DIR`/`ACCOUNTS_DB` consistently.

## Frontend changes (`roadmap.html`)

- **Login wall** (`showLoginWall` `:7915`, `attemptLogin` `:8020`, `#loginTeam` `:954`):
  - Parallel phase: add "email + password (all teams)" sign-in calling `/api/auth/login`,
    store the account token (`frazil_account_token`); one team → auto-switch; many → team
    picker (reuse `renderTeamSwitch`).
  - Cutover: make global sign-in default and **remove `#loginTeam`**; keep `?team=`
    deep-links to preselect after login.
- **Team switcher** (built: `#frzTeamSwitch`, `renderTeamSwitch`, `switchTeam`,
  `roadmap.html:7862-7913`, account menu `:16764`): repoint at the global session — teams
  from the `/api/auth/login` response; `switchTeam(team)` calls `POST /api/auth/switch` for
  the per-team token instead of a local `frazil_sessions` entry. The `API` layer
  (`X-Team` + per-team Bearer, `:2186-2202`) is unchanged.
- **Storage:** add `frazil_account_token`; keep the active per-team token in `frazil_token`.
  `doLogout` (`:8299`) clears both.
- **Membership UI:** a team-settings "Members" panel (team admins) reusing the user-list
  rendering; a minimal super-admin console (org-wide).

## Rollout phases
1. **Build + ship dormant:** schema + migration CLI (dry-run), global auth endpoints,
   membership endpoints, super-admin — deployed but login default stays per-team. Backfill
   missing emails (report-driven).
2. **Opt-in validate:** enable global sign-in for the owner + one team; switcher powered by
   the global session. Confirm switch-gating, roles, revocation.
3. **Cut over:** flip global login to default, remove the team field, retire legacy
   per-team login (keep a hidden fallback briefly).

## Security review + tests (required)
New privileged/cross-team surface → run `/security-review` before cutover. New tests
(new `tests/test_accounts.py`, extend `test_roles.py`/`test_auth.py`; conftest already
isolates `FRAZIL_TENANTS_DIR` so `_accounts.db` is sandboxed):
- Global login success/failure + anti-enumeration + rate-limit.
- **Switch gating:** member can switch into their teams; non-member gets 403 (core property).
- Role comes from membership (viewer/editor/admin enforced per team via the minted token).
- Super-admin gating (non-super can't hit `/api/admin/*`).
- Migration correctness: link-by-email, one-password-carry-over, memberships complete,
  emailless users skipped + reported; idempotent re-run.
- Revocation: revoked global account can't login/switch.

## Concerns / tradeoffs to accept
- **Departs from strict per-team isolation** (inherent to B): `accounts.db` is a new
  high-value target holding all credentials + the membership graph. Same instance-role/SES
  protections; must be added to the (currently thin) backup story.
- **"Carry over one password"**: multi-team people keep one team's password; others are
  discarded — communicate this; they can self-reset.
- **Global revocation latency**: up to the 24h per-team token TTL unless we add a
  per-request membership check (documented; tightenable later).
- **Super-admin** is a brand-new powerful role — smallest viable scope + audited.

## Verification (when executed)
- `pytest` green incl. the new `test_accounts.py`; JS syntax check on both `<script>`
  blocks; `python -c ast.parse` on `server.py` (per CLAUDE.md).
- Local end-to-end via throwaway teams (as with Option A): build accounts.db from two
  teams, global-login, verify the switcher lists both, switch mints a working per-team
  token, non-member switch is refused, super-admin console works. Drive with chrome-devtools.
- Deploy adds a new file (`accounts.db`) on the box — one-time migration step in
  `DEPLOYMENT.md`; confirm `/api/version` + a real global login on prod.
