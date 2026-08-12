/* SLA client-side checks - copy/paste browser-console snippets.
 *
 * WHY THIS EXISTS, AND WHY IT IS NOT A NODE SCRIPT
 * ------------------------------------------------
 * The paused-elapsed FREEZE (SLA-3 Stage 2 / addendum A2 item A1-2) is a client-only display rule:
 * the server digest returns only the `paused` KIND, never an elapsed figure, so there is nothing to
 * assert in the Python suite. A2 preferred a dependency-free Node assertion that runtime-extracts the
 * real SLA functions from roadmap.html. That extraction IS clean (the SLA cluster - _slaDur,
 * _slaBasisMs, _slaEffectiveFromMs, _slaBeforeEffective, _slaPausedState, slaState, firstTouchState -
 * reads only a handful of stubbable globals), so option (c) is feasible in an environment that has
 * node. This repo's build environment does not (node is absent from PATH, Program Files, nvm and npm),
 * so the mandatory "demonstrate it fails when deliberately broken, report both outputs" step cannot be
 * performed against a Node artifact here. Per A2 ("do not force (c); a wrong reuse is worse than a
 * documented manual procedure") the guard is delivered as option (b): a copy-paste console snippet run
 * against the REAL loaded functions - no stub, no drift - which is strictly better evidence than a
 * stubbed node run. It is re-runnable by anyone: open the app on an SLA-enabled team, paste, read.
 *
 * The same mechanism could also carry the SLA-2 parity fixture's client half (documented as prose in
 * tests/test_sla_parity.py). That path exists; it is intentionally NOT built here (A2 scope).
 */

// ── A1-2: the paused clock is FROZEN (elapsed does not read now - basis) ──────────────────────────
// Returns {v1, v2, pass}. v1/v2 are the paused remaining in MILLISECONDS captured under two very
// different injected `now` values. A correct freeze reads pausedSince (a fixed point), so v1===v2.
// A broken freeze that reads Date.now() would make v1 !== v2. Millisecond granularity on purpose:
// the original 4-second wall-clock check compared a DAY-granular rendered string and could not tell a
// frozen clock from a running one.
function slaFreezeCheck(){
  // A paused item: priority 1 (needs slaTargets.resolution["1"] > 0 and slaTargets.enabled), sitting
  // in a statusIsWaiting status, with a fixed basis + pausedSince.
  var p = {priority:'1', status:'__frzWait__',
           createdAt:'2026-08-01T00:00:00Z', basis:'2026-08-01T00:00:00Z', pausedSince:'2026-08-01T06:00:00Z'};
  var savedWait = statusIsWaiting, savedDur = _slaDur, savedNow = Date.now;
  statusIsWaiting = Object.assign({}, statusIsWaiting, {'__frzWait__':true});
  _slaDur = function(ms){ return 'MS'+ms; };   // expose raw ms instead of the day/hour-rounded string
  function paused(nowMs){ Date.now = function(){ return nowMs; }; var s = slaState(p); return s && s.full; }
  var T1 = Date.parse('2026-08-02T00:00:00Z');
  var T2 = Date.parse('2026-08-05T12:00:00Z');   // 3.5 days later - a running clock would drift hugely
  function ms(full){ var m = /MS(-?\d+)/.exec(full||''); return m ? +m[1] : NaN; }
  var v1 = ms(paused(T1)), v2 = ms(paused(T2));
  _slaDur = savedDur; Date.now = savedNow; statusIsWaiting = savedWait;
  var pass = (v1 === v2 && !isNaN(v1));
  console.log('[slaFreezeCheck] v1='+v1+'ms  v2='+v2+'ms  frozen='+pass);
  return {v1:v1, v2:v2, pass:pass};
}

// ── Demonstrate the guard can FAIL: break the freeze, re-run, restore ─────────────────────────────
// Temporarily rewrites _slaPausedState to read Date.now() (the exact bug the guard protects against),
// runs slaFreezeCheck (expect pass=false), then restores. Proves the check is not vacuous.
function slaFreezeCheckBroken(){
  var orig = _slaPausedState;
  _slaPausedState = function(targetMs, basisMs, p){
    var rem = targetMs - Date.now();   // BUG: reads now instead of the frozen pausedSince
    return {kind:'paused', cls:'sla-paused', short:'Paused', full:'Paused, '+_slaDur(rem)};
  };
  var r;
  try { r = slaFreezeCheck(); } finally { _slaPausedState = orig; }
  console.log('[slaFreezeCheckBroken] with the freeze deliberately broken -> frozen='+r.pass+' (expected false)');
  return r;
}
