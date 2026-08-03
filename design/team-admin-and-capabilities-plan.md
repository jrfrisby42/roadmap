# Team Admin page + Capability/Licensing model + AssetHub setup (plan)

Status: Part A (tabbed admin page) BUILDING now. Parts B (capabilities/licensing) and C
(AssetHub credential) are DESIGN - not built. Frontend is `roadmap.html` beta module;
capability server-enforcement (Part B) will touch `server.py`. Version bumps are J.R.'s.

Decisions (2026-08-03):
- Releases becomes its OWN independently-toggleable view (not tied to Planning). [Q1 = b]
- When sprints are off, the item-page Sprint field + Sprint-history card are REMOVED, not
  disabled-with-hint. [Q2 = remove]
- Capabilities are a CASCADING TREE: pick the top (includes everything under it) or a more
  granular child. The tree makes the ties visible. Framed as the basis for a LICENSING model,
  not just view control. [Q3]

---

## Part A - A true per-team tabbed admin page (BUILDING)

### Where it is now
`frzAdminPage` (the Flow-owned Admin page) = the classic `.admin-modal` box re-parented in
(`build()` ~roadmap.html:18632-18636), which has its OWN internal tabs via `renderAdminPanes`
(devs / statuses / types / users / data), PLUS `frzTeamSettings` (`renderTeamSettings` ~20733)
appended below as three cards: AssetHub, Jira gate, View visibility. It reads as "classic box +
bolted-on panel," not one designed surface. Everything is already per-team (per-team SQLite), so
"works for each team" is true data-wise; the gap is coherence.

### Phase 1 (this build) - a beta-owned top tab bar, re-parent don't rewrite
Wrap `frzAdminPage` in a beta-owned tab bar (reuse `.frz-plan-tabs`/`.frz-plan-tab`), three tabs:
- **Configuration** - the classic `.admin-modal` box, moved in whole and UNTOUCHED (keeps its own
  internal sub-tabs). Zero risk to the classic config wiring.
- **Integrations** - the AssetHub card + the Jira gate card (split out of `_frzTeamSettingsHtml`).
- **Views & Access** - the View visibility card today; the capability tree (Part B) lands here.

Mechanics: split `_frzTeamSettingsHtml()` into `_tsIntegrationsHtml()` (AssetHub + Jira) and
`_tsAccessHtml()` (views). `build()` creates the tab bar + three panes and re-parents `.admin-modal`
into the Configuration pane; `renderTeamSettings()` renders the two section HTMLs into their panes
and wires them (the existing `_frzWireTeamSettings` selectors find the controls wherever they sit).
Tab state in-memory (`state.adminTab`, default 'config'); admin-only, unchanged gate.

Out of scope for Phase 1: hoisting the classic sub-areas (devs/statuses/types/users/data) up to
first-class tabs, and putting the admin sub-tab in the URL (`/admin/integrations`). Both are clean
Phase-2 follow-ups once the shell exists.

---

## Part B - Capability / cascade / licensing model (DESIGN)

### The problem (from the reference sweep)
`enabledViews` is a UI-only allowlist: `_viewEnabled()` gates the rail entry + route + `setView`
and nothing else. Turning "Planning" off still leaves live: the item-page Sprint field
(`_frzWireSprintField` ~20578) + Sprint-history card (`injectSprintHistory` ~24953), the Kanban
`?sprint=` scope + Active-Sprint board + Sprint filter chip, the List Sprint column
(`_frzSprintLabel` ~10135) + side-panel Sprint row, the CSV Sprint column, the dashboard
Maintenance & Operations duty section, the rail count pill, and ALL sprint/planning/release server
endpoints (`/api/sprints`, `/api/planning-sessions/*`, `/api/releases`, the `sprint` query filter) -
none of which are gated. There are also TWO planning systems (classic session board in block-0 +
the beta tabbed UI); the classic board is gated by ROLE only, not `enabledViews` (roadmap.html:9250).

### The model - a cascading capability tree (supersedes bare view toggles)
Capabilities form a hierarchy. A parent toggle includes all children; children can be toggled
individually; the UI shows the nesting so ties are visible. Stored per-team as a capability set
(the successor to `enabledViews`; `enabledViews` becomes a derived/back-compat projection).

Proposed tree (draft):
- Core (always on, cannot disable): My Home, Admin, Gantt, List.
- Kanban
- Planning & Delivery (parent)
  - Sprints (sprint planning + board scope + item Sprint field/history + dashboard duty)
  - Planning Sessions (Review/Sprint/Release session board + commit)
  - Releases  <- now its OWN view too (decision Q1=b); can run without Sprints
- Reports
- Calendar
- Integrations (parent)
  - Jira
  - AssetHub
  - AI assist (the Claude text-box feature, when built)
- Intake portal (public ticket intake)

"Pick the top" = enable the parent (all children on). Granular = toggle a child. A LICENSING TIER
is just a named preset of this set (e.g. Starter / Team / Enterprise pick which parents+children are
on), which is why the tree is the right primitive - views were too coarse and didn't cascade.

### Enforcement (two layers - both required)
1. Client cascade: derive `cap(x)` from the capability set; every cross-surface reference consults
   it and REMOVES the affordance when off (per Q2). The exact per-surface checklist:
   - Item page: hide Sprint field + Sprint-history card (`_frzWireSprintField`, `injectSprintHistory`).
   - Kanban: drop `?sprint=` scope, the Active-Sprint pseudo-board, and the Sprint filter chip.
   - List: drop the Sprint column + the side-panel Sprint row.
   - CSV export: drop the Sprint column.
   - Dashboard: hide the Maintenance & Operations sprint-duty section; rail pill stops counting sprint.
   - Scenario/What-if: hide sprintAdd/sprintRemove ops.
   - Classic block-0 planning board: gate on the capability, not just role (closes the 9250 gap).
2. Server enforcement: the sprint/planning/release endpoints return 403 when the capability is off.
   Closes the deferred-API caveat (today it is hidden-UI-over-open-endpoints). This is the part that
   makes a licensing boundary real rather than cosmetic.

### Releases as its own view (decision Q1=b)
Today Releases is a sub-tab of the Planning view (`renderBetaPlanning` tab bar; `/planning/releases`).
To let a team have Releases without sprint planning, promote it to a first-class view/capability:
its own rail entry + route (`/releases`), its own capability toggle, reusing `renderReleasesTab`.
Small relocation; the render code is already self-contained.

---

## Part C - AssetHub API setup per team (DESIGN + decision)

### Where it is now
The mapping (`providerEnvironment` + `assethubTeam`) is DB config, editable in the admin card. The
CREDENTIAL is env-based: `ASSETHUB_API_KEY_<TEAM>` in the server `.env` (`_assethub_api_key`,
server.py:5719), read only inside the outbound client, requires a restart; the page shows only
presence (`assethubCredentialPresent`), never the value. So a team admin CANNOT self-serve the key.

### Options
- Option 1 - keep `.env` (status quo, low effort). Fine for IT + a few teams. Win = clearer setup
  guidance + the existing "Test connection" health check surfaced in the new Integrations tab. The
  page already tells the admin the exact env var name to add.
- Option 2 - per-team encrypted DB credential (the self-service unlock). Move the key into per-team
  encrypted storage, a WRITE-ONLY admin input (show "present / rotate", never read back), a
  key-encryption key from env, per-call audit, primary-admin gate. The outbound client reads from
  there instead of `.env`. Real security change; needs a security sign-off (holding a provider API
  key in the app DB, even encrypted).

### Recommendation
If broad self-service across many teams is the goal, do Option 2 (it is the actual unlock, and it
fits the licensing model - a team enables the AssetHub capability AND provides its own key). If it is
just IT + a couple of teams, Option 1 + the health/test UX in the Integrations tab is enough now.

Decision needed: how many teams will self-configure AssetHub, and is an encrypted provider key in
the app DB acceptable to security?

---

## Sequencing
1. Part A Phase 1 - tabbed admin shell (this build). Houses everything below.
2. Part B - capability tree in the "Views & Access" tab + client cascade + server 403 gates +
   Releases-as-own-view + the classic-board gap. The highest-value correctness work; makes toggles
   trustworthy and is the foundation for licensing.
3. Licensing presets (named tiers over the capability set) - once Part B lands.
4. Part C Option 2 (AssetHub self-service credential) - only if broad self-service is needed.
5. Admin Phase 2 - hoist classic sub-areas to first-class tabs + admin sub-tab in the URL.
