# Frazil Roadmap `/beta` — Jira-Parity Feature Notes

Working list of features needed to fully replace Jira, beyond the
Boards/Sprints/Releases spec (frazil-boards-planning-spec.md). Not yet a
build spec — planning notes to be turned into specs per tier.

## Already on J.R.'s pending list

1. **Attachments** — store directly in the existing S3 environment
   (infrastructure already provisioned; no intermediary service).
   Item page gets an Attachments section: upload button, drag-and-drop,
   and — critically — paste-from-clipboard in the description/comments
   (screenshots are most bug-report attachments). Thumbnails for images,
   filename rows for everything else. Presigned URLs for upload/download;
   nothing public.
2. **Flag issue** — flag/unflag on the item page (and card corner
   indicator on Kanban/List); flagged state filterable.
3. **Mentions** — @mentions in comments and description with typeahead;
   pairs with notifications below (a mention must notify or it's
   decorative).
4. **Recents** — fold into the Cmd+K quick switcher (Tier 1) rather than
   a standalone list.

## Tier 1 — required before anyone can leave Jira

- **Notifications + watchers.** Notify on: assignment, @mention, status
  change on watched items, comment on watched items. Auto-watch what you
  create/comment on/are mentioned in; manual watch toggle on item page.
  Delivery: email + chat webhook (Teams/Slack — confirm which); in-app
  Inbox (bell + feed) Linear-style.
- **My Work.** "My items" default view/filter (assignee = me, open,
  sorted by priority/due). Requires actually populating Assignee —
  currently mostly empty; add Assignee to the sprint-readiness modal as
  an optional-but-encouraged field.
- **Intake funnel.** Lightweight "Report an issue" form (title,
  description, screenshot paste, reporter auto-set → New status), or
  email-to-item. At creation, suggest possible duplicates by fuzzy title
  match (real duplicates exist in current data: FRAZ-72/55, FRAZ-69/46,
  FRAZ-67/54/43). Add "Duplicate of" link type.
- **Cmd+K quick switcher.** Fuzzy search across item keys, titles,
  boards, views, saved filters; recent items pre-populated on open.

## Tier 2 — needed within ~a month of cutover

- **Bulk edit.** Wire up the existing List checkboxes: set status /
  sprint / release / assignee / priority on N items at once.
- **Roles, light.** Viewer / Member / Admin only. Needed once intake
  opens the tool to non-dev users.
- **Export & backup.** Full JSON/CSV export of items + history; scheduled
  dump to S3 (same bucket env as attachments).
- **Migration plan.** Use existing Jira sync as the bridge: import open
  Jira issues with key mapping preserved, two-way sync for a few sprints,
  then Jira read-only. Write as its own ticket before Tier 1 build so
  data-model decisions account for it.

## Tier 3 — quality of life, build when felt

Keyboard shortcut grammar (c create, e edit status, a assign…); REST API +
webhooks for internal automation (SAP/HubSpot glue); minimal automation
rules engine (later — the NEEDS DATE CHECK auto-clear pattern, formalized);
item templates per type for intake quality; responsive item page/list for
warehouse-floor checks; **prompt-on-drop for multi-status board columns** —
when a card is dropped into a column with >1 status, a small popover asks
which status to apply instead of always using the column's drop status;
eliminates the lossy round-trip (e.g. Approved → drag out → drag back lands
as New). Build only if teams are regularly re-correcting statuses after
drags after ~a month of board usage.

## Deliberately NOT building

Custom fields per project (fixed schema is a feature, not a gap), time
tracking / hour logging, full workflow editors with conditions and
validators (the readiness-modal status floor is the right amount of
enforcement).

## Sequencing

Boards/Sprints/Releases (in progress) → Tier 1 spec → cutover prep
(Tier 2) → Tier 3 as demand appears. Rename (Flow/Cairn) lands with the
graduation-to-production ticket.

## Deferred — status ranking + sprint-readiness floor

The sprint-readiness "Make sprint-ready" modal currently shows below-active
statuses as unchanged (e.g. "Status: New (unchanged)") and does not raise
them. This is intentional and correct for now: the original spec's floor
("below Approved → Approved") assumed a status ordering the data model
doesn't have. Statuses (New, Approved, Needs Scoping, In Progress, In
Testing, Released, Inactive) are currently unordered.

Prerequisite before implementing the floor: add an explicit status-rank
field (an integer order on each status) so "raise to the floor" is well
defined rather than hardcoded. Once ranking exists, the readiness modal
should: floor any item below the configured ready-rank up to it, show
"Status will change: X → Y", log the change to item history, and never
demote items at/above the floor. Until then, status is left unchanged on
sprint add — by design, not a defect.

Note this supersedes spec check B2/#2 in frazil-boards-planning-spec.md.

## Known limitation — sprint-history reconstruction window (Stage 2b)

Sprint History on the item page is reconstructed by replaying the item's
"Sprint planning:" activity entries. The /api/activities endpoint returns
only the latest 500 rows, so reconstruction sees that window. Fine for
current data, but a long-lived item whose earliest sprint events have
scrolled past the most recent 500 system-wide activities could miss its
oldest memberships. Not a problem at current data volumes. If it becomes
one, the fix is a server-side backfill that reads the full activity table
(or a dedicated sprint-membership table) instead of the 500-row API
window. Deferred — do not build speculatively.

## Stage 3 follow-ups (raised post-spec, not yet decided)

Stage 3 (My Work + notifications/@mentions/watchers) shipped and verified.
Two open questions surfaced after the spec was written:

1. WATCH TRIGGER BREADTH. Watching currently notifies on 4 events: status
   change, new comment, @mention, assignment. It does NOT notify on other
   field changes (priority, due/target dates, story points, sprint, release,
   description). Jira/Linear convention is chattier. Decision needed: keep
   the narrow quiet set, or broaden — leading candidates to add are PRIORITY
   and DUE/TARGET-DATE changes (a watcher being surprised a deadline moved
   is the exact failure watching should prevent). Lean: add those two.

2. WATCHED-ITEMS ACCESS POINT. Watched items have no browsable home today —
   they surface only via the notification bell. Do NOT fold them into My
   Work (My Work must stay strictly assignee = accountability; watching =
   awareness, a different relationship — conflating breaks both). Options:
   (a) do nothing — the bell/inbox is the feed, sufficient if users only
   want to be TOLD when watched items change; (b) a separate "Watching" rail
   entry under VIEWS listing items where you're a watcher (cheap — reuses
   the My Work view pattern + existing watcher data). Decide by the
   behavioral test: do users browse watched items, or just want pings? If
   browse → rail entry; if pings → bell is enough, skip it.
