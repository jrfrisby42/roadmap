/* DARK-ACTIVE-1 check - copy/paste browser-console snippet (Part 3: active vs inactive, cold load per theme).
 *
 * The defect passed every "the dark rule exists" check. The trustworthy test is: each active state must
 * differ MEASURABLY from its inactive sibling, in BOTH themes, on a COLD LOAD (not a live toggle). Run
 * darkActiveDiff() after cold-loading each theme (set frazil_dark_mode then hard reload), on /list with a
 * Space scoped and a Status filter applied. Expect ok:true in both themes.
 *
 * Threshold: sRGB Euclidean distance > 12 between active and inactive backgroundColor (a value too small to
 * see is the same defect with extra steps). Active fill vs the SURROUNDING SURFACE is the one that failed.
 */
function _rgb(s){ var m=(s||'').match(/-?\d+(\.\d+)?/g); return m?m.slice(0,3).map(Number):[0,0,0]; }
function _dist(a,b){ var x=_rgb(a),y=_rgb(b); return Math.round(Math.sqrt((x[0]-y[0])**2+(x[1]-y[1])**2+(x[2]-y[2])**2)); }
function darkActiveDiff(){
  var dark=document.body.classList.contains('dark-mode');
  function pair(activeSel, inactiveSel, label){
    var a=document.querySelector(activeSel), i=document.querySelector(inactiveSel);
    if(!a) return {label, note:'active element not found ('+activeSel+')'};
    var ab=getComputedStyle(a).backgroundColor, af=getComputedStyle(a).color;
    var ib=i?getComputedStyle(i).backgroundColor:null, iff=i?getComputedStyle(i).color:null;
    var bgD=ib?_dist(ab,ib):null, fgD=iff?_dist(af,iff):null;
    return {label, active_bg:ab, inactive_bg:ib, bgDist:bgD, active_fg:af, inactive_fg:iff, fgDist:fgD,
            differentiated: (bgD!=null && bgD>12) || (fgD!=null && fgD>12)};
  }
  var rows=[
    pair('.frz-tab.is-active', '.frz-tab:not(.is-active):not(.frz-tab--more)', 'view tab'),
    pair('.frz-chip.is-active', '.frz-chip:not(.is-active):not(.frz-chip--scope)', 'filter chip'),
    pair('.frz-item[aria-current="true"]', '.frz-item:not([aria-current="true"])', 'rail nav'),
  ];
  // scope chip: compare its fill to the top-bar surface it sits on (it should be a Space-colour tint, not flat surface)
  var scope=document.querySelector('.frz-chip--scope'), bar=document.querySelector('.frz-topbar');
  if(scope&&bar){ var sb=getComputedStyle(scope).backgroundColor, brb=getComputedStyle(bar).backgroundColor;
    rows.push({label:'scope chip vs surface', scope_bg:sb, surface_bg:brb, dist:_dist(sb,brb), tinted:_dist(sb,brb)>8}); }
  var ok=rows.every(function(r){ return r.differentiated || r.tinted; });
  var out={theme: dark?'dark':'light', ok:ok, rows:rows};
  console.log('[darkActiveDiff] '+JSON.stringify(out));
  return out;
}
