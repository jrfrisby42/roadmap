# Departments feature - plan (mapped 2026-07-16)

Make departments a first-class, admin-managed concept: per-dept color + notify
emails, dept-scoped notifications on submit, and a no-login department queue on
the existing "My Tickets" page.

## Decisions (locked)
- Dept people are **external / no-login**: they get an emailed link and view their
  queue on the token-gated `/my-tickets` page (toggle: My tickets / Dept queue).
- Dept notify emails fire **on submit only**.
- Dept queue is **submitter-safe** (same fields as the `/ticket` reporter view -
  no internal owner/assignee).
- Departments remain **per-team** (managed in that team's admin).

## Data model
- Keep the existing `departments` name list unchanged (all current readers - item
  tags, intake dropdown, item-page chips - keep working).
- New config key **`departmentMeta`** (dict keyed by dept name):
  `{ "IT": { "color": "#0059A9", "emails": "a@x.com, b@y.com" }, ... }`.
  Additive, optional per dept; mirrors the `intakeProjectEmails` pattern.
- Plumbing: add to `VALID_KEYS`, `init_team_db` defaults (`{}`),
  `_migrate_config_keys` (presence-only), and `get_all`.

## Phase 1 - config + admin panel + pill colors
- Server: `departmentMeta` config plumbing.
- Admin: new **Departments** tab in the admin modal. One row per department:
  name (from `departments`) + color picker (`<input type=color>`) + notify-emails
  text field + add / remove. Add/remove edits BOTH `departments` and
  `departmentMeta`. (This also becomes the authoritative place to manage the dept
  list, which today has no real editor.)
  - Note: no in-place rename in v1 (item tags store the name; a rename would
    orphan them). Add/remove + edit color/emails only.
- Pills: item-page department chips (and the `/ticket` status page) use
  `departmentMeta[name].color`; text color auto-picked (black/white) by luminance
  so any color stays readable. No color set -> today's default.
- Client: load `departmentMeta` in `boot()`.
- Tests: config round-trip; admin-gate.

## Phase 2 - dept notify emails on submit
- Server helpers: `_department_emails(team, dept)` and `_dept_notify_addrs(team,
  item)` (union across the item's departments).
- In `intake_submit`/`_intake_send_emails`: after reporter + team + per-project
  emails, email each dept notify address, **deduped** against those already
  emailed. Email includes a CTA to the dept queue
  (`/my-tickets?email=<addr>&t=<token>&team=<T>&dept=<D>`).
- Tests: dept emails fire on submit; dedup; none when no dept/emails set.

## Phase 3 - My Tickets dept queue + toggle
- Server helper `_depts_for_email(team, email)` -> dept names in team T whose
  `departmentMeta[d].emails` include the email. Scan exposed teams to find all
  (team, dept) the email belongs to.
- `/my-tickets` route: accept `&team=&dept=` for a dept scope. Gate server-side:
  valid reporter-list token (email ownership) AND email is in that dept's emails
  (anti-enumeration). Return all portal tickets in team T tagged with dept D,
  rendered submitter-safe (reuse the ticket-card rendering).
- `_my_tickets_page`: render a toggle - "My tickets" + one button per (team,dept)
  the email belongs to. Default = My tickets.
- Tests: dept queue lists dept tickets; blocked when email not in the dept
  (even with a valid token); toggle links carry the right params.

## Cross-cutting
- Admin-modal growth: this adds a ~4th panel. Fine as a tab now; reinforces the
  separate "routed admin surface" follow-up (see earlier discussion) - not a
  blocker.
- Color contrast: `text = luminance(color) > 0.6 ? #1f2733 : #fff`.
- Deploy each phase independently (two-file scp + restart), tests green per phase.
