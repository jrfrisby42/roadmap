/* COLDLOAD-2 regression check - copy/paste browser-console snippet (behavioural layer).
 *
 * Guards the fix for the cold-load deep-link hijack (COLDLOAD-1). Before the fix, EVERY cold load of a
 * deep link was redirected to the default saved filter's view+params (typically /gantt?status=<AllOpen>),
 * because the post-hydration default-apply won an async race and navigated against the stale state.view.
 * The fix records the requested route at mount (state.bootReq) and declines the default when a destination
 * is specified. state.bootReq / _frzBootSpecifiesDest / the router are closure-scoped, so this snippet does
 * NOT call them - it inspects where a cold load LANDED, which is what the user experiences.
 *
 * HOW TO RUN: for each case, COLD-LOAD the URL (full navigation / reload, not in-app clicking), then paste
 * and run coldloadLandingCheck(expected). pass=true means the load landed where requested, not on the
 * default's gantt. Reverting the fix makes cases 1-5 fail (they land on /gantt).
 *
 * expected: { view?: 'reports'|'list'|..., item?: <id>, panel?: <id>, status?: 'New' }
 *   - view: the pathname's view segment must match
 *   - item: the item page must be open for that id (path /item/<id>)
 *   - panel: the List detail panel must be open (?panel=<id> present / panel visible)
 *   - status: the Status chip must carry that value (Part A's suppression-path proxy)
 *
 * FLAKINESS CAVEAT (COLDLOAD-2 2.1): a landing check can pass by winning the race on broken code whenever
 * the router happens to run first. This snippet therefore reports the observed landing only; it CANNOT
 * assert the decision (that the default DECLINED) because the deciding function and state are closure-scoped
 * and unreadable from the console. Treat a single green run as necessary, not sufficient; re-run a few times,
 * and if the tooling allows, throttle the network (case 4.4 in the prompt) to sample the other ordering.
 */
function coldloadLandingCheck(expected){
  expected = expected || {};
  var path = location.pathname.replace(/\/+$/,'');
  var sp = new URLSearchParams(location.search);
  var out = { url: location.pathname + location.search, pass: true, checks: {} };
  function want(name, cond){ out.checks[name]=cond; if(!cond) out.pass=false; }

  if(expected.item != null){
    want('itemPageOpen', /^\/item\/\d+/.test(path) && String(path.split('/')[2])===String(expected.item));
  }
  if(expected.view){
    var vm = path.match(/^\/(gantt|kanban|list|planning|dashboard|reports|my-home|team-calendar|admin)/);
    want('view=' + expected.view, !!vm && vm[1]===expected.view);
    // the tell-tale of the bug: a non-gantt request that landed on gantt
    if(expected.view!=='gantt') want('notHijackedToGantt', !(vm && vm[1]==='gantt'));
  }
  if(expected.panel != null){
    var panelOpen = !!document.querySelector('.frz-list-panel:not([hidden])') || sp.get('panel')===String(expected.panel);
    want('panelOpen=' + expected.panel, panelOpen);
  }
  if(expected.status){
    // the Status chip carries the value (Part A proxy: a filter param suppresses the default)
    var chip = [].slice.call(document.querySelectorAll('.frz-chip')).filter(function(c){ return /Status/i.test(c.textContent); })[0];
    want('statusChip=' + expected.status, !!chip && chip.textContent.indexOf(expected.status)>=0);
  }
  console.log('[coldloadLandingCheck] ' + JSON.stringify(out));
  return out;
}

/* THE EIGHT CASES (COLDLOAD-2 2.3). Cold-load each, then run the paired check.
 *   1. /reports                         -> coldloadLandingCheck({view:'reports'})
 *   2. /item/<id>                        -> coldloadLandingCheck({item:<id>})
 *   3. /?item=<id>                       -> coldloadLandingCheck({item:<id>})   // canonicalized to /item/<id>
 *   4. /list?panel=<id>                  -> coldloadLandingCheck({view:'list', panel:<id>})   // secondary fault
 *   5. /item/<id>?team=<team>            -> coldloadLandingCheck({item:<id>})   // notification/email link form
 *   6. /  (default set)                  -> lands on the default's view WITH its filter (feature intact)
 *   7. /  (NO default set)               -> fallback view, no filter        // BLOCKED: no no-default account
 *   8. /list?status=New                  -> coldloadLandingCheck({view:'list', status:'New'})   // suppression-path unregressed
 *
 * Case 6 has no single assertion here (it is "the default DID apply"): verify the default's view loaded
 * with its Status chip filled - the opposite of cases 1-5. Case 7 is UNVERIFIABLE until a no-default dev
 * account exists (do NOT unset J.R.'s default - it is a server-mirrored config write on the daily-use account).
 *
 * COLDLOAD-2 ADDENDUM A - CASE 9 (the case the 8-case table structurally could not see, and the one that
 * MOST NEEDS OBSERVING): specified-ness must be account-INDEPENDENT. Before the addendum, it was decided by
 * comparing the requested view to lastView(), so a user whose lastView equalled the requested view was
 * hijacked. lastView is a localStorage key, so this IS drivable live (unlike the closure-scoped predicate):
 *
 *   9.  coldloadSetLastView('reports'); then COLD-LOAD /reports -> coldloadLandingCheck({view:'reports'})
 *   9b. coldloadSetLastView('list');    then COLD-LOAD /list    -> coldloadLandingCheck({view:'list'})   // mirror
 *
 * Both must land on the requested view (default declined). On the PRE-ADDENDUM build these land on the
 * default's Gantt - that is the failing-then-passing demonstration for this addendum. Cases 1-8 are
 * account-INDEPENDENT after the fix: re-running any of them under a non-gantt lastView changes no outcome
 * except case 6's LANDING view (bare / still applies the default, now landing on lastView's view - feature
 * intact, by design). coldloadSetLastView restores nothing; set it back or ignore (it only affects where a
 * bare / lands, and the user's next navigation overwrites it).
 */
function coldloadSetLastView(view){
  var u=''; try { u = localStorage.getItem('frazil_rm_user')||''; } catch(e){}
  if(!u){ console.log('[coldloadSetLastView] no frazil_rm_user - log in first'); return false; }
  try { localStorage.setItem('frazil_beta_lastview_'+u, view); } catch(e){ return false; }
  console.log('[coldloadSetLastView] frazil_beta_lastview_'+u+' = '+view+' (now cold-load the URL for the case)');
  return true;
}
