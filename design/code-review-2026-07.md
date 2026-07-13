# Code Review — 2026-07-06 (post-4.10.2) — findings & 4.11.0 plan

Full multi-lens review of `server.py` + `roadmap.html` triggered by the item_key
data-loss incident (fixed 4.10.2). Findings below; ✅ = shipped in the 4.10.3
hotfix, ⏳ = deferred to 4.11.0, ○ = backlog. Verdicts: **verified** = traced to
code by hand this session; **agent-tested** = a review subagent ran a probe;
**reported** = code-consistent, not independently re-traced.

## Shipped — 4.10.3 hotfix (commit 24d33d7)
- ✅ **CRITICAL** `spawn_recurrence` 500 on every spawn — inherited the parent's
  `itemKey`, tripping the unique `item_key` index (armed by the 4.10.2 key fix).
  Now strips `itemKey` (+ attachments/sprint/release/defer/preBlock) and assigns a
  fresh key; added an idempotency guard + recur tests. **verified.**
- ✅ **HIGH** Unauthenticated cross-tenant read — `require_auth` granted a viewer
  session on an `X-Team` header with no token; `GET /api/teams` (public) enumerates
  slugs → `GET /api/all` dumped a team's data + user roster. Fallback removed; token
  now required. **verified.**
- ✅ **MEDIUM** Attribute-breakout XSS — `esc()` (no quote escaping) in `title=`
  attributes fed by Jira ticket summaries (roadmap.html:4683) and owner names
  (:12910). Now `escA(esc(...))`. **verified.**

Also confirmed this session: dev-team item_key wipe hit 4 items (10/21/33/223),
all recovered to originals (FRAZ-10/15/27/169); scan found **no orphaned recurring
chains** (every terminal recurring item already had a successor).

## 4.11.0 — the structural work (own verification pass)

### T1. Kill the wholesale-PUT wipe class — ✅ SHIPPED 4.11.0 (commit 3d8136a)
`update_project` now merges (`merged = {**old, **body}`) instead of replacing the
blob, so omitted fields are preserved while explicit sends (incl. empty-string
clears) still apply. itemKey/attachments guards kept as belt-and-suspenders for
the stale-SENT case. Tests added (omitted preserved + durable; explicit clear
works). Retiring the per-field guards is optional cleanup. Original writeup below.

<details><summary>T1 (original) — the root cause</summary>
`update_project` (server.py ~1719) writes the client body as the ENTIRE item blob
via `_save_project(c, pid, body)`. The classic edit modal (roadmap.html ~5308)
builds that body **from scratch**, so any blob field it omits is destroyed. Today
only `itemKey` + `attachments` are guarded — the item_key incident was one head of
a hydra. Fields empirically wiped by a modal-shaped PUT include: `sprintId`,
`sprintHistory`, `assignee`, `release`, **`archived` (modal-saving an archived item
un-archives it)**, `reporter`, planning outcomes (`deferred`/`defer*`/`releaseNumber`/
`releaseNotes`), `storyPoints`, `rank`, `jiraLastKnownStatus`, `jiraSyncSkipped`,
`preBlockStatus`, `recurrence_parent`, `hubspotId`, `departments`, typed `links`.

**Fix (strategic, one change kills the class incl. future fields):** in
`update_project`, stop replacing the blob. Either
  (a) **merge-patch:** `merged = {**old, **body}` — behavior-preserving because the
      modal always sends every field it lets you clear; or
  (b) stricter **`_EDITABLE_FIELDS` whitelist** (mirror `_BULK_FIELDS`' philosophy):
      copy only known-editable keys from `body` onto `old`.
Keep the `itemKey`-immutability line even after merging (client must not overwrite
it with a stale/forged value). Prefer (b) if we want server-owned fields
(jira*/sprintHistory/reporter/release/etc.) structurally unreachable by any client
PUT. Add tests asserting each server-owned field survives a modal-shaped PUT.
Retire the per-field guards once the merge/whitelist lands.
</details>

### T2. Enforce user revocation — ✅ SHIPPED 4.11.1 (commit 310cd70)
`revokedAt` was only honored in forgot-password. Now rejected at `login` (folded
into the not-found branch — no enumeration) AND per-request in `require_auth` via
`_is_user_revoked` (fail-open on DB error), so a revoked user's live token dies
immediately. Tests: revoked login 401, revoked live-token 401, non-revoked 200.

### T3. Concurrent-edit protection — ✅ SHIPPED 4.12.0 (commit d64868b)
Both parts landed. **Part A (user↔user):** `get_all` exposes `updated_ts`;
`update_project` 409s when the client's `_baseUpdatedTs` != current (opt-in, returns
a fresh token); client `putItemGuarded` + a Reload/Override dialog wired into the
edit modal save and `_ipPersist` (all item-page edits) — quick/system ops stay
unguarded. **Part B (sync↔user):** `jira_pull_sync` + `_sync_recurrence_child_statuses`
now re-read at write time and merge only sync-owned fields (status advance applied
only if current status is unchanged). Coarse locking; field-level deferred. Tests:
409 on stale token / 200 on current / 200 when omitted. **All three code-review HIGHs
(T1/T2/T3) are now closed.** Original writeup below.

<details><summary>T3 (original)</summary>
No versioning/ETag anywhere — last-write-wins full-blob replace. Worst window:
`jira_pull_sync` (server.py ~3295 read → ~3458 write) holds a stale blob across up
to 10 Jira GETs + the FF hierarchy walk, then writes the whole blob back, silently
reverting concurrent user edits (pull-all runs on a timer in every admin tab, 2
workers). **Fix:** targeted re-read-and-merge of only sync-owned fields in
`jira_pull_sync` and `_sync_recurrence_child_statuses` (the pattern already used
correctly in the update_project FF-pull and `sync_attachments_to_jira`); add an
`updated_ts` precondition → 409 on item PUTs (`updated_ts` is already maintained by
`_reindex_project`). Composes with T1.
</details>

## 4.13.0 — SHIPPED (security batch, commit fd0be73)
- ✅ Audit-actor spoofing — `_audit_actor()` allows only the 'System' sentinel;
  else auth user. Applied across item/config/attachment/jira endpoints; comment
  author forced to the poster.
- ✅ `delete_comment` ownership (editor→own only, admin→any) + audit entry.
- ✅ `/audit` reflected XSS — `date_from`/`date_to` validated to YYYY-MM-DD.
- ✅ `add_attachment` client key confined to `items/{pid}/{attId}/`.

## Still open — medium findings (fold in where cheap)
- Planning commit: no draft-state/lock enforcement → double-commit re-inserts
  activity rows. Guard with `UPDATE planning_sessions SET status='committed' WHERE
  id=? AND status='draft'` (409 on rowcount 0); verify `locked_by`.
- Boot backfill footgun: `_backfill_item_keys` re-keyed the wiped items with WRONG
  fresh keys before recovery. Demote to detect-and-alarm (log.error + audit, don't
  auto-mint for a keyless row whose id < max keyed id); assign keys at create-time
  in child-sync (`_do_sync_children` ~3586) and `bulk_import` (~2512) instead.
- Audit integrity: (a) the `update` diff only logs fields PRESENT in the body, so
  field REMOVAL (the item_key wipe) left zero audit trail — capture removals, or
  store the prior blob (see T-history below); (b) `_username`/`author`/`username` in
  request bodies let any editor spoof the audit actor — use `auth["username"]`;
  (c) `delete_comment` has no ownership check and no audit.
- Reflected XSS on `/audit` via unescaped `date_from`/`date_to` (server.py ~2697);
  admin-session-gated. `html.escape` + `^\d{4}-\d{2}-\d{2}$` validation.
- `add_attachment` trusts a client-supplied S3 `key` (server.py ~2241) → cross-item/
  tenant object read via the shared bucket. Validate `^items/{pid}/[0-9a-f]{32}/`.
- Attachment 50 MB cap is advisory (checks client-declared size); enforce via
  `ContentLengthRange` on the presign or verify object size on record.
- `create_project` skips the `testWeeks >= dueWeeks` rule and doesn't coerce numeric
  fields (feeds later 500s in recur/round_up).
- 3 hardcoded status-name logic sites break rule 3 for teams that rename statuses:
  Gantt scheduling (roadmap.html ~3338/3346), `PROTECTED_STATUSES` (~6716), editor
  filter (~10403) — resolve via the status-flag maps.
- Dead code: `startJiraBackgroundSync`/`_jiraSyncInterval` (roadmap.html ~14395) is a
  superseded second sync engine — remove to avoid reviving the wrong one.
- Jira href/onclick sinks (roadmap.html ~14422–14448) use `esc()` in attribute/JS-
  string contexts — low risk (format-constrained), fix for consistency.

## Operational / product gaps (see also design/flow-road-off-jira.md)
- No real backup/restore: EBS snapshots only; client JSON export omits comments/
  activities/audit/boards/sprints/releases; no server export endpoint, no scheduled
  DB dump, no tested restore. (Off-Jira Tier 0 "non-negotiable", still open.)
- No monitoring/alerting, no `/healthz`, no staging, scp-to-prod, manual rollback.
- Hard-delete cascade on items — no soft-delete/trash; one misclick is permanent.

## Feature opportunities (ranked, two-file/SQLite-compatible)
1. **Item version history + restore** (M) — store the prior blob per `update` audit
   row; item-page History tab. *Direct app-level insurance for the wipe class.*
2. **Server export endpoint + nightly `sqlite3 .backup` → S3** (S) — Tier-0 trust gap.
3. **Soft-delete / 30-day trash** (S).
4. **Optimistic concurrency on item PUT** (M) — see T3.
5. **Email notifications** (M) — SES + notification hooks + settings-tab stub exist.
6. **Jira history importer** (L) — the actual off-Jira migration engine.
7. `/healthz` + uptime + error alerting (S); Teams webhook (S); generalized bulk
   edit (S); Cmd+K (M); read-only API tokens (S–M).
