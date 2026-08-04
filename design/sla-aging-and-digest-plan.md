# IT/Ops SLA + aging + weekly digest (plan)

Status: **BUILT (A1 + A2 + B1), pending deploy.** B2 (enable the systemd timer on prod) is an
ops step - unit files + steps are in DEPLOYMENT.md ("Weekly queue-health digest"). Serves the
IT/Ops ticketing go-live. Additive, config-driven, team-aware. Version bumps are J.R.'s.

Implemented:
- **A1** - `slaTargets` config (5 sites) + admin "Resolution SLA" card + shared `slaState(p)`
  helper + badges on List (team-aware column + mobile card), Kanban cards, and the item page.
- **A2** - Reports SLA tiles (breached / at-risk / % within SLA, team-aware) + "Aging (open)"
  aggregate card (0-2 / 3-7 / 8-30 / 30d+). `_rptComputed` rows now carry `slaKind`.
- **B1** - `digestConfig` config + `_digest_summary` / `_render_digest_email` / `send_team_digests`
  + `--send-digests` CLI + `POST /api/admin/send-digest-preview` + admin "Weekly digest" card.
  Recipients reuse intakeNotifyEmail (team-wide) + departmentMeta[d].emails (per-dept).
- Tests: +4 config (test_config.py), +8 digest (test_digest.py). Frontend verified on a local
  seed + Chrome (SLA badges/branches, Reports tiles/aging, digest preview path).

Decided direction (2026-08-03): picked over AI copilot / analytics-trend / self-host-editor
because it compounds the ticketing rollout and reuses infrastructure we already have (the Reports
measures engine + SES `send_email` + the CLI-subcommand pattern).

---

## Grounding (what already exists)

- **Overdue** is already computed (client `_rptComputed`: `due < today AND not terminal`; item-level
  too). SLA is the same shape but time-based off `createdAt`, not the `due` date.
- **`firstResponseAt`** IS stamped (server.py: first assignment OR first non-reporter comment), and
  **`completedAt`** is the first terminal entry. So resolution time + response time are real data.
- **`send_email(to, subject, text, html)`** (SES via instance role) exists - the digest reuses it.
- **CLI subcommands** (`--new-team`, `--sync-litestream`) are handled near the top of `server.py`
  BEFORE the app boots - a `--send-digests` subcommand fits the exact same slot.
- **Recipient sources** exist: `intakeNotifyEmail` (team inbox) + `departmentMeta[dept].emails`.
- **Reports** (5.12.0) already renders KPI tiles + a group-by pivot - aging + SLA slot straight in.

---

## Stage A - SLA targets + breach badges + aging  (frontend-heavy, 1 new config key)

### Config: `slaTargets` (new key, 5 sites + admin UI)
```
slaTargets = {
  "enabled": false,
  "resolution": { "1": 4, "2": 24, "3": 72, "4": 168 },   // hours, by priority (Urgent..Low)
  "atRiskPct": 80                                          // >= this % of target elapsed = "at risk"
}
```
- **By priority only** for v1 (the existing 1-4). Type-based targets are a later refinement.
- **Calendar hours** for v1 (elapsed wall-clock). Business-hours/working-calendar is a big follow-up.
- **Resolution only** for v1. A `response` map (first-response SLA) is a clean later add - the data
  (`firstResponseAt`) is already there.
- Admin editor lives in the tabbed admin **Configuration** tab: enable toggle + 4 hour inputs.

### Client SLA state (mirrors the overdue helper)
For each item, given target hours `H = resolution[priority]`:
- `slaDueAt = createdAt + H hours`
- **Open, terminal-excluded:** `breached` if `now > slaDueAt`; `atRisk` if elapsed >= `atRiskPct%`
  of `H`; else `onTrack`. Show remaining/overrun ("2h left" / "breached 3h ago").
- **Closed:** `met` if `completedAt <= slaDueAt`, else `missed`.
- No priority or no `createdAt` or SLA disabled -> no SLA state (renders nothing).

### Where the badge shows
- **List** rows, **Kanban** cards, **item-page** Details, and the **edit modal** - a small pill:
  red "SLA breached", amber "SLA at risk / Nh left", green tick "met", grey "missed". Reuse the
  overdue color language; no new palette; no indigo.
- Only when `slaTargets.enabled` (so non-IT teams see nothing) - a team-aware gate like the others.

### Reports (extends 5.12.0)
- **Aging buckets** for OPEN items: 0-2d / 3-7d / 8-30d / 30d+ - a tile row (or a group-by option).
- **SLA tiles:** % within SLA (closed in window), open breached count, at-risk count.
- Fits the existing team-aware tile/pivot machinery; no restructure.

---

## Stage B - weekly queue-health digest email  (server + CLI trigger)

### Config: `digestConfig` (new key)
```
digestConfig = {
  "enabled": false,
  "recipients": [],            // explicit To list (managers / team inbox)
  "includeDeptEmails": false   // also send per-dept summaries to departmentMeta[d].emails
}
```

### `--send-digests` CLI subcommand (idiomatic; no scheduler dep, no new auth surface)
- Iterates every team with `digestConfig.enabled`; builds a per-team summary from the SAME row
  model Reports uses: **open**, **overdue**, **SLA-breached**, **aged 30d+**, **closed last 7d**,
  and the **top N oldest open** tickets (with age + assignee). Renders a compact HTML + text email.
- Sends via `send_email` to `recipients` (+ per-dept if `includeDeptEmails`). Best-effort per team:
  one team's failure never aborts the rest; logs each send.
- Idempotent-ish: it reports current state, so a double-run just re-sends the same snapshot.

### Trigger = a systemd timer on prod (an OPS step, needs J.R.)
- `roadmap-digest.timer` -> `roadmap-digest.service` runs `python server.py --send-digests` weekly
  (e.g. Mon 08:00 America/Denver). I write the CLI + the unit files + a DEPLOYMENT.md section;
  **enabling the timer on the box is J.R.'s ops action** (same as Litestream enablement).
- Optional convenience: an admin-only `POST /api/admin/send-digest-preview` that emails the caller a
  one-off preview, so the content can be reviewed without waiting for the timer.

---

## Testing
- pytest: `slaTargets`/`digestConfig` config validation (VALID_KEYS + presence-only migration);
  a pure SLA-state helper if any lands server-side; the digest **builder** (given seeded items,
  the summary counts + oldest-N are correct) with `send_email` monkeypatched (no real SES).
- Frontend: SLA badge states + the Reports aging/SLA tiles verified on a local seed + Chrome.

## Staging
- **A1:** `slaTargets` config + admin editor + client SLA state + badges (List/Kanban/item/modal).
- **A2:** Reports aging buckets + SLA tiles.
- **B1:** `digestConfig` + `--send-digests` builder/sender + preview endpoint + unit files + docs.
- **B2 (ops):** enable the systemd timer on prod.

## Decisions (locked 2026-08-03)
1. SLA basis: **by priority, calendar hours** (no type dimension, no business-hours in v1).
2. SLA metric: **resolution only** (created -> terminal). Response SLA deferred.
3. Digest delivery: **CLI `--send-digests` + a weekly systemd timer**; recipients **reuse the
   existing emails** - `intakeNotifyEmail` (team-wide) + `departmentMeta[d].emails` (per-dept),
   no new recipient list. So `digestConfig` reduces to `{ "enabled": false }` (a per-dept summary
   goes to that dept's emails; the team-wide summary goes to intakeNotifyEmail).
4. Cadence: weekly, Mon 08:00 America/Denver (set in the timer; the CLI just sends current state).

Consequently `slaTargets` drops the `response` map, and `digestConfig` drops `recipients` /
`includeDeptEmails` (dept emails are always used when present).
