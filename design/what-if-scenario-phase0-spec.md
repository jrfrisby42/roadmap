# What-if scenario planning - Phase 0 spec (substrate + guard + banner)

**Build-ready.** Phase 0 delivers the scenario *substrate* and its *safety spine* - no
levers, no visible data change yet. When it ships you can: enter scenario mode (state is
snapshotted, a banner appears), the app renders normally, **every server write is blocked**
(provably), and exit/discard restores the exact prior state. Phases 1+ (levers, commit
replay) build on this. All code is in **block 0** of `roadmap.html` (where the engine
inputs live); the Flow shell drives the UI via `_call`. Companion: `what-if-scenario-planning.md`.

## The model
A scenario is a snapshot-and-mutate-in-place of the five engine inputs. On enter we
**deep-clone the current state into `_scenarioBackup`**; levers (Phase 1) mutate the live
vars in place; discard restores from the backup. No parallel data structure - the engine
keeps reading the same module vars, so it recomputes for free.

## New state (block 0, near the other engine vars ~`roadmap.html:2306`)
```js
let _scenarioActive = false;
let _scenarioBackup = null;   // { projects, ownerCapacity, _capOverrides, _assignments, _assignmentImpacts } deep clones
let _scenarioOps    = [];     // mutation log (empty in P0; Phase 1 levers push {kind,target,from,to})
let _scenarioPausedTimers = null;   // handles paused on enter, restored on exit
```

## Deep clone helper
```js
function _clone(x){ try { return structuredClone(x); } catch(e){ return JSON.parse(JSON.stringify(x)); } }
```
Items/config/assignments/overrides are all JSON-safe, so this is total.

## enter / discard / commit (block 0)
```js
function enterScenario(){
  if(_scenarioActive) return;
  _scenarioBackup = {
    projects:          _clone(projects),
    ownerCapacity:     _clone(ownerCapacity),
    _capOverrides:     _clone(_capOverrides),
    _assignments:      _clone(_assignments),
    _assignmentImpacts:_clone(_assignmentImpacts),
  };
  _scenarioOps = [];
  _scenarioPauseTimers();     // stop background writers/pollers that would clobber the overlay
  _scenarioActive = true;
  document.body.classList.add('scenario-active');
  _scenarioBanner(true);
  _scenarioRerender();
}
function discardScenario(){ _scenarioExit(/*restore=*/true); }
function commitScenario(){
  // P0: no ops recorded, so commit == discard (nothing to replay). Phase 1 replaces this
  // with: replay _scenarioOps through the real endpoints (temporarily bypassing the guard),
  // then reload from server. For P0 it simply restores + exits (the levers that produce ops
  // don't exist yet).
  _scenarioExit(/*restore=*/true);
}
function _scenarioExit(restore){
  if(!_scenarioActive) return;
  if(restore && _scenarioBackup){
    projects           = _scenarioBackup.projects;
    ownerCapacity      = _scenarioBackup.ownerCapacity;
    _capOverrides      = _scenarioBackup._capOverrides;
    _assignments       = _scenarioBackup._assignments;
    _assignmentImpacts = _scenarioBackup._assignmentImpacts;
  }
  _scenarioActive = false; _scenarioBackup = null; _scenarioOps = [];
  document.body.classList.remove('scenario-active');
  _scenarioBanner(false);
  _scenarioResumeTimers();
  _scenarioRerender();
}
```

## The safety spine: hard write guard on `API` (~`roadmap.html:2219`)
Wrap the three mutating methods so that while a scenario is active they **never issue a
`fetch`**. This is a backstop - Phase-1 levers mutate the overlay directly and never call
`API`, so this only fires if some existing control tries to save. It rejects with a tagged
error + a toast; no partial writes reach the server.
```js
// after the API literal:
['post','put','del'].forEach(function(m){
  var _real = API[m].bind(API);
  API[m] = function(){
    if(_scenarioActive){
      try { if(typeof showToast==='function') showToast("Scenario mode - changes aren't saved. Exit to make real edits.", 'info'); } catch(e){}
      var err = new Error('scenario-blocked'); err.scenarioBlocked = true;
      return Promise.reject(err);
    }
    return _real.apply(API, arguments);
  };
});
```
Notes:
- **GET stays allowed** (reads are safe). The one GET that reassigns `projects`
  (`putItemGuarded`'s reload path, `:2267`) can't run because the preceding `put` is blocked.
- Callers that `await` a blocked write get a rejected promise; existing `.catch`/try-blocks
  surface the toast. Any optimistic local drift is wiped by discard's restore, so the
  session can't end up dirty.

## Pause background writers/pollers (block 0)
These both **write to the server and reassign `projects`**, so they must not run mid-scenario:
- `_planningAutosaveTimer` (`:12468`) - planning autosave.
- `_jiraSyncTimer` (`:15436`) - background Jira sync (`runBackgroundJiraSync` pulls + reassigns `projects`).
```js
function _scenarioPauseTimers(){
  _scenarioPausedTimers = { plan:_planningAutosaveTimer, jira:_jiraSyncTimer };
  if(_planningAutosaveTimer){ clearInterval(_planningAutosaveTimer); _planningAutosaveTimer = null; }
  if(_jiraSyncTimer){ clearInterval(_jiraSyncTimer); _jiraSyncTimer = null; }
}
function _scenarioResumeTimers(){
  // Re-arm via the same setup fns the app uses at boot (don't reuse stale handles).
  try { if(typeof _startPlanningAutosave==='function') _startPlanningAutosave(); } catch(e){}
  try { if(typeof _startBackgroundJiraSync==='function') _startBackgroundJiraSync(); } catch(e){}
  _scenarioPausedTimers = null;
}
```
> Confirm the exact re-arm function names during build (grep the two `setInterval` sites);
> if they're inline, extract them into named starters first. `_notifTimer` (`:17836`) is a
> harmless GET poll and can stay running.

## Re-render hook
```js
function _scenarioRerender(){
  try { if(typeof renderCurrentView==='function') renderCurrentView(); } catch(e){}
  // If the Flow shell is mounted, also refresh its owned surfaces (Planning gauge etc.).
  try { if(document.body.classList.contains('frz-beta-active') && window._frzScenarioRefresh) window._frzScenarioRefresh(); } catch(e){}
}
```
`renderCurrentView` (`:2335`) recomputes the active view against the (possibly mutated)
inputs; conflicts/load/heatmap are computed during render, so no cache to invalidate.

## The Phase-1 lever entry point (skeleton now, used later)
Define it in P0 so levers just call it; it does the mutate + derived-rebuild + rerender +
op-log in one place:
```js
function scenarioMutate(op){
  if(!_scenarioActive) return;
  _scenarioOps.push(op);
  // op.apply(): a small closure the lever supplies that mutates projects/_assignments/etc.
  try { op.apply && op.apply(); } catch(e){}
  if(op.rebuildImpacts) buildAssignmentImpactMap();   // when _assignments changed (:6109)
  _scenarioRerender();
  _scenarioBanner(true);   // refresh the "N changes" count
}
```

## The banner (body-level, like the manual/mention menu - unscoped)
A thin bar pinned to the **bottom** (avoids the topbar), shown only when active.
```js
function _scenarioBanner(show){
  var el = document.getElementById('scenarioBanner');
  if(!show){ if(el) el.remove(); return; }
  if(!el){
    el = document.createElement('div'); el.id='scenarioBanner';
    document.body.appendChild(el);
  }
  el.innerHTML =
    '<span class="sb-dot"></span>'+
    '<span class="sb-msg">Scenario mode - nothing is saved'+
      (_scenarioOps.length ? ' · '+_scenarioOps.length+' change'+(_scenarioOps.length===1?'':'s') : '')+'</span>'+
    '<span class="sb-sp"></span>'+
    '<button class="sb-commit" id="sbCommit">Commit…</button>'+   // hidden/disabled in P0 (no ops)
    '<button class="sb-discard" id="sbDiscard">Discard</button>';
  el.querySelector('#sbDiscard').onclick = function(){ discardScenario(); };
  var cb = el.querySelector('#sbCommit');
  cb.style.display = _scenarioOps.length ? '' : 'none';   // P0: always hidden (no ops)
  cb.onclick = function(){ commitScenario(); };
}
```
CSS (self-contained, theme-aware via existing tokens where present; amber accent so it
reads as "temporary"):
```css
#scenarioBanner{position:fixed;left:0;right:0;bottom:0;z-index:150;display:flex;align-items:center;gap:10px;
  padding:9px 16px;background:#3a2f00;color:#ffe9a8;font:13px 'Lato',sans-serif;box-shadow:0 -2px 10px rgba(0,0,0,.25)}
#scenarioBanner .sb-dot{width:9px;height:9px;border-radius:50%;background:#ffc107;box-shadow:0 0 0 3px rgba(255,193,7,.25)}
#scenarioBanner .sb-msg{font-weight:700}
#scenarioBanner .sb-sp{flex:1}
#scenarioBanner button{border:0;border-radius:7px;padding:6px 13px;font:inherit;font-weight:700;cursor:pointer}
#scenarioBanner .sb-discard{background:#5a4a00;color:#ffe9a8}
#scenarioBanner .sb-commit{background:#ffc107;color:#3a2f00}
body.scenario-active{--scenario-pad:44px}   /* optional: pad content so the bar never covers the last row */
```
Reserve space if needed (e.g. add `padding-bottom:var(--scenario-pad)` to the scroll
container while `body.scenario-active`).

## Entry point
One control that calls `enterScenario()`. Recommended: a **"What-if" button in the Flow
toolbar** (near Group by / Legend / Capacity on the Gantt+Planning bar) so it's next to the
planning surfaces it affects; a rail entry or account-menu item also works. Exit is via the
banner's Discard (P0) / Commit (P1+). Keep placement trivial - the substrate is the point.

## Flow-shell bridges
The Flow shell (block 1) calls `_call('enterScenario')` and reads `_val('_scenarioActive', false)`;
expose `window._frzScenarioRefresh` from the shell if any Flow-owned surface needs a bespoke
refresh beyond `renderCurrentView`.

## Acceptance criteria (Phase 0)
1. Clicking **What-if** enters scenario mode: banner appears; `_scenarioActive===true`;
   `_scenarioBackup` holds deep clones of all five inputs; the current view still renders.
2. **No write escapes:** with a scenario active, `API.put`/`API.post`/`API.del` return a
   rejected promise and issue **no `fetch`** (verify by stubbing `window.fetch` and asserting
   it is not called for a PUT/POST/DELETE; a GET still works). A toast appears.
3. Background writers are paused: `_planningAutosaveTimer` and `_jiraSyncTimer` are cleared
   on enter and re-armed on exit.
4. **Discard restores exactly:** mutate `projects` (e.g. change an item's `dev` directly in
   the console) while active, then Discard - `projects` deep-equals the pre-enter snapshot,
   banner gone, `body.scenario-active` removed, timers resumed.
5. No visible data/behaviour change when NOT in scenario mode (guard is a pass-through;
   `render`/save paths unchanged). `pytest` unaffected (frontend-only).

## Verification (this project's pattern)
Browser (chrome-devtools) on the demo team: enter → assert banner + flag + backup; stub
`fetch` and confirm a PUT is blocked (no fetch, rejects) while a GET passes; mutate + Discard
and deep-compare `projects` to the snapshot; confirm the two timers cleared/re-armed.

## Out of scope for Phase 0 (later phases)
- Any **levers** (mark-out, sprint add/remove, drag, release move) - Phase 1+.
- **Commit replay** of ops through real endpoints - Phase 1 (P0 commit == discard).
- **Rewiring existing controls** to route to the overlay - Phase 2 (P0 blocks them).
- Named/saved scenarios - Phase 3.

## Build notes
- ~120 lines in block 0 + ~20 lines CSS + one entry button. server.py unchanged (frontend
  only; no `APP_VERSION` server-logic change, but bump the shared `APP_VERSION` in both files
  per convention on ship).
- Run the JS syntax check on both `<script>` blocks; `pytest` stays green (no backend change).
