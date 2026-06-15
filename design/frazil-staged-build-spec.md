# Frazil Roadmap `/beta` — Staged Build Spec (Stages 1–4)

Builds on the existing `/beta` shell and the Boards/Planning features
(Features A–C, all shipped). Same ground rules throughout:

- Additive to production; no production routes/styles/behavior changed.
- Reuse existing `/beta` components: filter chips, the Save-filter popover,
  the modal pattern, accent `#0059A9`, zero indigo.
- Item status & priority badge colors stay exactly as they are in
  production — never rebranded.
- Every automated/material field change writes an attributed entry to the
  item's Activity & History.
- URLs are the state (deep-linkable) wherever a new view/panel is added.
- End every task with a diff summary: every file touched and why. No
  unrequested changes riding along with scoped work.

Build the four stages IN ORDER. Each stage is independently shippable and
gets a review checkpoint before the next begins. Present a short plan
(data model, routes, components, files to touch) and WAIT for approval
before building each stage.

---

## STAGE 1 — Foundations: ranked statuses, the readiness floor, board reorder

Everything downstream that reasons about "how far along" or "is this done"
depends on statuses having an order. This stage establishes that, applies
the deferred sprint-readiness floor on top of it, and folds in the small
board-column reorder cleanup that shares the same "ordering" theme.

### 1a — statusOrder (canonical status ranking)

Add an explicit integer rank to each status so "promote to the floor" and
"is terminal" are well-defined rather than hardcoded.

Default ranking (DRAFT — user to confirm, especially Inactive):

| Status        | rank | terminal? | on main flow? |
|---------------|------|-----------|----------------|
| New           | 10   | no        | yes            |
| Needs Scoping | 20   | no        | yes            |
| Approved      | 30   | no        | yes  ← readiness floor target |
| In Progress   | 40   | no        | yes            |
| In Testing    | 50   | no        | yes            |
| Released      | 60   | YES       | yes            |
| Inactive      | —    | NO        | NO — parking lot, off the linear flow |

Design intent for **Inactive** (per user): it is a PARKING LOT for items
not yet decided/started — the opposite of done. So:
- It is NOT terminal. An inactive item linked to a release counts as
  OUTSTANDING (part of the "not done" denominator), and at sprint
  completion an inactive item is flagged as "not Done/Released."
- It is NOT on the linear flow (you don't "advance" into Inactive; you
  park there from anywhere and rejoin anywhere), so it has no main-flow
  rank.
- The only terminal status is **Released** (plus a future "Done" if added).

Future note: a "Backlog" status may be added later as a second flavor of
"parked" (Backlog = intended-but-not-now; Inactive = undecided/shelved).
Both would be non-terminal and off/low on the main flow. Adding it must be
a config change (a new row + rank + terminal flag), NOT a code change —
which is why terminal-ness and ranking MUST live in one config (see
implementation note below), with zero hardcoded status checks anywhere.

Implementation:
- Store the rank + terminal flag in ONE place (config/constants in
  server.py and/or a statuses table) — the single source of truth. Adding
  a future status (e.g. Backlog) must be a one-row config change, never a
  code change. Do not scatter status lists or "is it done" checks across
  views.
- Terminal statuses are Released only (today). Inactive is NOT terminal.
- Replace any existing hardcoded "is this status done/terminal" checks
  (release progress bars, sprint completion) with reads from this config.
  In particular, verify the Feature C release progress bar and sprint
  completion now treat Inactive as OUTSTANDING, not done — this is a
  behavior change from how they may currently work. List every check you
  find and change in the diff summary.

### 1b — Readiness status-floor (supersedes the deferred item)

In the "Make sprint-ready" modal: if an item's status is on the main flow
and ranks BELOW the floor (Approved, rank 30), promote it to Approved on
add. Specifically:
- Below floor (New, Needs Scoping) → show info line "Status will change:
  {X} → Approved" and apply it on confirm.
- At/above floor (Approved, In Progress, In Testing, Released) → "Status:
  {X} (unchanged)". NEVER demote.
- Inactive (parking lot, no main-flow rank) → treat as below-floor for
  promotion: "Status will change: Inactive → Approved" — adding a parked
  item to a sprint IS the "we've decided to do this" moment, so promoting
  (reactivating) it is correct. Confirmed by user.
- Log the change to item history: "Sprint planning: Status {X} → Approved".

### 1c — Board column reorder via ↑/↓ (Feature A cleanup)

The board editor already shows up/down arrows on columns. Make them
functional: ↑/↓ reorder the column within the list (drag is NOT required).
Disable ↑ on the first column and ↓ on the last. Persist order on save.

### Stage 1 acceptance
1a. Status ranking exists in one place; release progress bars and sprint
    completion read terminal-ness from it (verified unchanged behavior).
1b. Below-floor item shows the promotion line and is promoted to Approved
    on add, logged to history; at/above-floor items are never changed.
1c. Board column order can be changed with ↑/↓ and persists.

---

## STAGE 2 — Item depth: attachments + multi-sprint history

Both are item-page-centric and low cross-system risk.

### 2a — Attachments (S3)

Add an Attachments section to the item page. Files store DIRECTLY in the
existing S3 environment (already provisioned — no third-party service).

- Upload: button + drag-and-drop onto the section.
- **Clipboard paste**: pasting an image (Ctrl/Cmd+V) while focused in the
  description OR the attachments section uploads it as an attachment.
  Screenshots are most bug-report attachments — this is required, not
  optional.
- Display: image attachments show thumbnails; non-images show a filename
  row with type icon and size. Click to view/download.
- Storage: presigned URLs for upload and download; nothing public. Suggested
  key shape: `roadmap/items/{itemId}/{uuid}-{filename}`. Enforce a max file
  size (propose a value in the plan).
- Each add/remove logs to item history (attributed).
- USER NOTE in plan: confirm bucket/prefix and max size before building.

### 2b — Multi-sprint history (NOT simultaneous membership)

Track every sprint an item has been assigned to — a HISTORY list, not
active membership in multiple sprints at once. An item still belongs to at
most one current sprint; the difference is we no longer overwrite/forget
prior assignments.

- Data: add `sprintHistory` (ordered list of {sprintId, addedAt,
  outcome}) alongside the existing current-sprint reference. The
  carry-over flow already moves items between sprints — append to this
  list at each assignment and record the outcome (completed / carried-over
  / returned-to-backlog) when a sprint closes.
- Item page: a "Sprint history" line/section showing each sprint the item
  has been in, with outcome (e.g. "Sprint 1 — carried over, Sprint 2 —
  carried over, Sprint 3 — current").
- This makes item-level slippage visible ("in 3 sprints and counting"),
  complementing the sprint-level carry-over count from Feature C.
- Explicitly OUT OF SCOPE: an item being active in two sprints
  simultaneously, and any change to counts/capacity that would imply it.

### Stage 2 acceptance
2a. Upload, drag-drop, and clipboard-paste all attach to the item and
    persist via S3; thumbnails for images; add/remove logged to history.
2b. An item carried across sprints shows its full sprint history with
    outcomes; current-sprint behavior and all counts are unchanged
    (still one current sprint per item).

---

## STAGE 3 — People & flow: My Work + notification center with @mentions

My Work makes Assignee matter; the notification center makes @mentions
real. These ship together because a mention that doesn't notify is
decorative.

### 3a — My Work

A view answering "what should I be doing." Implement as a built-in saved
view (assignee = current user, not completed, sorted by priority then due
date). Reachable from the rail (its own entry or pinned at top of Saved
Filters — propose in plan). Encourage Assignee usage: add Assignee as an
optional field in the sprint-readiness modal and ensure it's editable on
the item page.

### 3b — Internal notification center + @mentions

**@mentions:** in comments and the description, typing `@` opens a
typeahead of users; selecting one inserts a mention token and, on save,
notifies that user. Render mentions as styled inline chips.

**Watchers:** an item auto-watches a user who created it, commented on it,
or was mentioned in it; manual watch/unwatch toggle on the item page.

**Notification center:** a bell icon in the top bar (right zone) with an
unread count; opens an in-app inbox feed. Notify on:
- you're @mentioned (comment or description)
- an item you watch changes status
- an item you watch gets a new comment
- you're assigned an item

Each notification links to the item; mark-as-read (individual + all);
unread count clears appropriately. Internal/in-app only for this stage —
email/chat delivery is a later stage, NOT now. Notifications are
per-user and private.

### Stage 3 acceptance
3a. My Work shows the current user's open items sorted sensibly; Assignee
    is editable on the item page and offered in the readiness modal.
3b. @mention typeahead works in comments + description; mentioning a user
    creates a notification; status change / new comment on a watched item
    notifies watchers; assignment notifies the assignee; bell shows unread
    count; inbox links to items; mark-as-read works.

---

## STAGE 4 — Cleanups: tabbed Settings + List multi-select release linking

### 4a — Tabbed Settings panel

The rail's Settings entry currently opens "My Account." Make it open a
tabbed settings panel instead, with My Account as the first tab. Other
tabs may start minimal/placeholder (propose the tab list in plan — likely
My Account, Notifications, and an admin-ish tab). Scope is just the
tabbed shell + moving the existing My Account content into tab 1; don't
build out empty tabs' contents beyond stubs.

### 4b — List multi-select → release linking (deferred from Feature C)

The List view has row checkboxes. Wire a bulk action: select N items →
"Add to release" → choose a release → links all selected items to it
(updating each item's Release field, logged to history). This was
deferred to item-page-only in Feature C; now generalize to the List.
Keep the item-page Release field working as-is.

### Stage 4 acceptance
4a. Rail Settings opens a tabbed panel; My Account is tab 1 with its
    existing content; other tabs are present as stubs.
4b. Selecting multiple List rows and choosing a release links them all;
    release progress bars update; each link logged to item history.

---

## Open questions for the user (resolve in/ before Stage 1 plan)
1. RESOLVED: Inactive is a non-terminal parking lot (undecided/shelved
   items); only Released is terminal; adding an Inactive item to a sprint
   promotes it to Approved. A future "Backlog" status may join it as a
   second parked flavor — config-only when it comes. Still confirm the
   non-Inactive ranking order (New → Needs Scoping → Approved → In
   Progress → In Testing → Released) if you want any changes.
2. RESOLVED: multi-sprint = HISTORY, not simultaneous membership.
3. Stage 2 attachments: confirm S3 bucket/prefix and max file size.

## Out of scope (these stages)
Email/chat notification delivery (in-app only for now); simultaneous
multi-sprint membership; Cmd+K quick switcher; intake form + duplicate
detection; roles/permissions; export/backup; prompt-on-drop boards. These
remain in the parity notes for later.
