/* JIRA-COL-1 check - copy/paste browser-console snippet (behavioural layer).
 *
 * The Jira List column is optional (default hidden, _optin7) and per-Space GATED: it appears only when
 * Jira sync is enabled AND the current Space scope has a project mapping (specific Space -> that Space
 * maps; All Spaces -> any Space maps). Gate off => it's absent from the picker, header, rows AND export -
 * not merely default-hidden. The first key links via the per-issue cached URL (it.jiraCache[key].url);
 * an uncached key is plain text (there is no client-visible Jira base URL). +N is plain text.
 *
 * HOW TO RUN (on the List, in the Flow shell): paste and run jiraColCheck(). Then switch Space scope to a
 * mapped vs an unmapped Space and re-run to watch `available` and `inOrder` flip together, while
 * `optinPreserved` stays true (the opt-in must survive a hiding scope change).
 */
function jiraColCheck(){
  var avail = (typeof _frzJiraColAvailable==='function') ? _frzJiraColAvailable() : 'n/a';
  var order = (typeof _listColOrderAll==='function') ? _listColOrderAll() : [];
  var eff   = (typeof _listEffectiveCols==='function') ? _listEffectiveCols() : [];
  var scope = (typeof window._frzListScope==='function') ? window._frzListScope() : 'n/a';
  var hidden = (typeof window._frzHiddenListCols==='function') ? window._frzHiddenListCols() : new Set();
  var out = {
    scope: scope,
    syncEnabled: !!(window._jiraSyncConfigData||{}).enabled,
    mappedSpaces: Object.keys(window._jiraProjectMappingData||{}).filter(function(k){ var v=(window._jiraProjectMappingData||{})[k]; return v && String(v).trim(); }),
    available: avail,                          // the gate result
    inOrder: order.indexOf('jira')>=0,         // gate on => present in the full order (picker/header source)
    inRendered: eff.indexOf('jira')>=0,        // present AND un-hidden => actually rendered
    optinHidden: hidden.has('jira'),           // true until the user un-hides it (then persists)
    // available <=> inOrder must always agree (the gate drives the order filter)
    gateConsistent: (avail===true) === (order.indexOf('jira')>=0),
  };
  console.log('[jiraColCheck] '+JSON.stringify(out, function(k,v){ return v instanceof Set ? Array.from(v) : v; }));
  return out;
}

/* Cell-render spot check: find the first List row that HAS jira keys and confirm the cell shape
 * (first key linked only when cached, +N plain, full list in title). Run jiraCellSample() after
 * enabling the column. Reports {key, linked, hasPlusN, titleHasAll} or 'no jira rows on this page'. */
function jiraCellSample(){
  var tds = document.querySelectorAll('#listBody td[data-col="jira"]');
  for(var i=0;i<tds.length;i++){
    var td=tds[i]; if((td.textContent||'').trim()==='-'||!(td.textContent||'').trim()) continue;
    var a=td.querySelector('a[href]');
    return { key:(td.textContent||'').trim(), linked:!!a, href:a?a.getAttribute('href'):null,
             hasPlusN:/\+\d+/.test(td.textContent||''), titleHasAll: !!td.querySelector('[title]') };
  }
  return 'no jira rows on this page';
}
