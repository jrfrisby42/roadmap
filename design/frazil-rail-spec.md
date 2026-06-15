# Frazil Roadmap — Left-Rail Shell Spec (`/beta`)

Paste this whole file to your coding CLI along with `frazil-leftnav-mockup.html`
(the mockup is the visual source of truth; this file is the contract).

## Goal

Build a NEW app shell at `https://roadmap.frazil.app/beta` so it can be tested
alongside the current app without disrupting it. The shell is: a collapsible
left navigation rail + a slim global top bar + a per-view filter row. The five
existing views (Gantt, Kanban, List, Planning, Dashboard) and the item detail
page render inside this shell, reusing their existing data layer and view
components wherever possible. Do not modify the current production routes.

## Routing

- `/beta` → redirect to the user's last-used view (default `/beta/gantt`).
- `/beta/gantt`, `/beta/kanban`, `/beta/list`, `/beta/planning`,
  `/beta/dashboard` — one URL per view; switching views is client-side
  navigation, no full page reload.
- `/beta/item/:id` (keep `?item=:id` working as an alias if that's easier) —
  item detail inside the same shell.
- Active filters serialize into query params (e.g.
  `/beta/list?owner=wasatch&type=bug&status=testing`) so any filtered view is
  linkable and shareable. Saved filters are just named bookmarks of these
  params.
- Add a small "Beta" pill next to the brand name in the rail, and a link
  somewhere unobtrusive (user menu) back to the classic UI.

## Design tokens

Same token set as the toolbar spec; rail additions marked NEW.

The palette is derived from the Frazil logo (sampled from the brand asset):
script-fill blue `#0059A9`, swoosh/sky blue `#65B1E3`, white. Neutrals are
blue-tinted (not purple) so the chrome sits in the same color world as the
logo.

```css
--bg-app: #F5F8FB;
--bg-surface: #FFFFFF;
--bg-rail: #FAFCFE;            /* NEW: rail background, one step off white */
--border: #E1E8F0;
--border-strong: #C9D6E3;

--accent: #0059A9;             /* Frazil logo script blue */
--accent-hover: #004A8F;
--accent-soft: #E7F1FA;        /* active-chip / active-nav fill */
--accent-soft-border: #C3DCF1;
--sky: #65B1E3;                /* logo swoosh blue: secondary accent */

--text: #14283F;               /* navy ink */
--text-secondary: #4A6178;
--text-muted: #8298AD;

--radius-chip: 999px;
--radius-control: 8px;

--topbar-h: 52px;              /* NEW: slimmer than the old 56px */
--rail-w: 232px;               /* NEW */
--rail-w-collapsed: 56px;      /* NEW */
```


### Brand color usage rules

- `--accent` (#0059A9) is the ONLY color for primary actions, active nav
  states, active chips, links, and focus rings.
- `--sky` (#65B1E3) is the supporting accent: progress fills, informational
  highlights, hover glows, the second project dot, avatar gradients. Never
  use it for clickable primary actions — it fails contrast on white for
  small text (use it for fills/strokes, not text).
- Status badge colors (New / In Progress / In Testing / Approved / Released /
  Inactive) and priority badge colors (Urgent / High / Medium / Low) are
  SEMANTIC and must remain EXACTLY as they are in the current production app
  — copy the hex values from production, do not rebrand, re-tint, or
  "harmonize" them with the new blue palette in any way. The proximity of
  the In Progress blue to `--accent` is accepted and intentional.
- All shadows use navy ink: `rgba(20,40,63,…)` — no purple-tinted shadows
  anywhere.
- Verify AA contrast: `--accent` on white passes for text; `--text-muted`
  (#8298AD) is for 11–12px labels on white/--bg-app only.

Typography: app's existing font stack. Rail items 13.5px/500 (600 when
active); rail section headings 11px/700, letter-spacing .07em, `--text-muted`
— this is the ONLY uppercase text in the shell. Top bar title 15px/700.

## Layout skeleton

```
┌──────────┬──────────────────────────────────────────────────┐
│  RAIL    │ TOP BAR  [⟨⟩][View title · crumbs]   [search][+ New item][JR] │
│          ├──────────────────────────────────────────────────┤
│ Views    │ FILTER ROW  [chips…][+ Filter][Save]      [view-specific] │
│ Projects ├──────────────────────────────────────────────────┤
│ Saved    │                                                  │
│ filters  │                VIEW CONTENT                      │
│          │                                                  │
│ ──────   │                                                  │
│ Settings │                                                  │
│ Admin    │                                                  │
└──────────┴──────────────────────────────────────────────────┘
```

CSS grid on the app root: `grid-template-columns: var(--rail-w) 1fr`, full
viewport height, content area scrolls — the rail and top bar never scroll
away. Collapsed state swaps the first column to `--rail-w-collapsed` with a
180ms ease transition.

## Component 1 — `<NavRail>`

Background `--bg-rail`, right edge 1px `--border`. Three regions:

**Brand row** (height `--topbar-h`, bottom 1px border): the EXISTING Frazil
logo (the blue script wordmark already used in the production header — reuse
that exact image/SVG asset, do not substitute an initial, monogram, or any
generated mark) + "Roadmap" + small "Beta" pill (`--accent-soft` bg,
`--accent` text, 11px/700). Logo renders at ~28px height, left-aligned,
linking to `/beta`. In the collapsed rail, keep the Frazil logo (scaled to
fit ~40px width, centered) rather than swapping to a different mark; if the
full wordmark is illegible that small and a dedicated compact Frazil mark
asset exists, use that — never an invented one.

**Scrollable middle**, sections in order:

1. `VIEWS` — Gantt, Kanban, List, Planning, Dashboard. Each item: 16px stroke
   icon + label + optional right-aligned count pill (item totals for List and
   Planning; counts respect the active Project but NOT the filter row).
   Reuse the icon set from the mockup SVGs.
2. `PROJECTS` — colored 8px dot + project name + count pill. Heading has a
   "+" button (stub it; opens nothing yet). Clicking a project sets the
   project scope for every view (this REPLACES the old Project filter chip —
   project is navigation now, not a filter).
3. `SAVED FILTERS` — flag icon + name. Heading "+" saves the current view +
   filter params under a user-supplied name. Clicking one navigates to its
   stored view URL with its stored params. Right-click or "…" on hover →
   rename / delete. Persist per user (profile if available, else
   localStorage).

**Footer** (top 1px border, never scrolls): Settings, Admin.

Item states: default `--text-secondary`; hover bg `#F0F0F7`;
active (`aria-current="true"`) bg `--accent-soft`, text `--accent`, 600
weight. Active view derives from the route.

### Collapsed state

- Width `--rail-w-collapsed`; Views + Settings/Admin become centered
  icon-only buttons with native `title` tooltips; PROJECTS and SAVED FILTERS
  sections hide entirely; brand shows mark only.
- Toggle: button at the far left of the top bar, plus keyboard shortcut `[`
  (ignored while typing in an input).
- Persist the collapsed/expanded choice per user. Default: collapsed on
  Gantt, expanded everywhere else — but once the user toggles manually,
  their choice wins globally and the per-view default stops applying.

## Component 2 — Top bar

Height `--topbar-h`, bottom 1px border, background `--bg-surface`. Contains,
left to right: rail-collapse toggle button (32px icon button), view title
(15px/700), muted crumb text (`· Fraznet` = active project; on item pages the
full breadcrumb), then right-aligned: global search, "New item" primary
button, account avatar (initials, 30px circle).

- NO filter controls in the top bar, ever.
- Global search: 210px, `/` focuses it from anywhere (when not in an input),
  searches across items regardless of current view.
- "New item": accent button, opens the existing item-create flow.
- The right cluster is pixel-identical on every screen including item detail.

## Component 3 — Filter row

One row directly under the top bar, owned by each view. Background
`--bg-surface`, bottom 1px border, padding 10px 14px, horizontal scroll with
hidden scrollbar on overflow.

Order: shared `<FilterChip>`s → "+ Filter" ghost chip → "Save filter" ghost
chip (rendered only when ≥1 chip is active) → spacer → view-specific
controls pinned right.

`<FilterChip>` is the SAME component from the toolbar spec (same anatomy and
default/hover/active/clear states) — reuse it, do not fork it.

Per-view configuration:

| View      | Shared chips                        | Pinned right                    |
|-----------|-------------------------------------|---------------------------------|
| Gantt     | Owner · Type · Status               | Group by · Legend · Capacity    |
| Kanban    | Owner · Type                        | Hide empty columns (toggle)     |
| List      | Owner · Assignee · Type · Status    | Columns (picker)                |
| Planning  | Owner · Statuses                    | New session · History           |
| Dashboard | Owner · Range (date-range chip)     | —                               |

Notes:
- Project is gone from this row (lives in the rail).
- "+ Filter" opens a menu of less-common fields (Sprint, Reporter, Priority,
  has-Jira-link, …); choosing one appends a chip to the row.
- "Save filter": prompts for a name, stores current view + query params,
  appends to the rail's SAVED FILTERS.
- "Legend" (Gantt) opens the legend as a popover instead of the current
  always-visible legend strip — inside `/beta`, remove that strip.
- Filter row state round-trips through the URL (see Routing).

## Component 4 — Item detail inside the shell

`/beta/item/:id` keeps the rail and top bar. Top bar left side becomes:
collapse toggle · `← Back` (returns to the previous view WITH its filters
intact — use history, not a hardcoded route) · breadcrumb
`Fraznet › FRAZ-137 › Item name` (key links to Jira-style copy, name
truncates). Status + priority badges sit just left of the search. Filter row
is hidden on item pages. Body reuses the existing item page content; carry
over the v2 improvements (linked key in crumb, feature-flag chips).

## Acceptance criteria

1. `/beta/*` routes work as specified; production routes untouched; "Beta"
   pill visible; link back to classic UI exists.
2. Switching among all five views: rail, top bar, and the right-hand control
   cluster do not shift by a pixel; only the filter row and content change.
3. Rail collapse: animates, persists per user, `[` toggles it, icon-only
   mode shows tooltips, default-collapsed on Gantt until the user overrides.
4. Clicking a project in the rail re-scopes every view and every count pill;
   no Project chip appears in any filter row.
5. A filtered view's URL, pasted into a new tab, reproduces the exact view +
   chips. Saving it as a filter adds a working rail entry that survives
   reload; rename and delete work.
6. "Save filter" chip only renders when at least one filter is active.
7. Item page: Back returns to the prior view with filters intact; right
   cluster identical to other screens; no filter row.
8. Gantt legend strip is replaced by the Legend popover.
9. Only one search input in the DOM per screen; `/` focuses it.
10. Visible keyboard focus everywhere; rail nav is arrow-key navigable;
    `prefers-reduced-motion` disables the collapse animation.
11. At 1024px the layout holds with the rail expanded; below 900px the rail
    auto-collapses (user can still expand as an overlay).
12. The brand row uses the production Frazil logo asset in both expanded and
    collapsed rail states — no placeholder or generated logo anywhere in
    `/beta`.
13. Status and priority badges render with pixel-identical colors to the
    current production app on every `/beta` screen (compare side by side).

## Out of scope

Kanban empty-column collapse internals, List column-picker internals,
slide-over item panel, multi-project admin, dark mode — separate tickets.
The "+ Filter" menu can ship with a minimal field list.
