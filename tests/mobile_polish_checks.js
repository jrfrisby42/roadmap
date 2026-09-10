/* MOBILE-POLISH-1 checks - paste in the browser console. Run at 430px (mobile / flag-off) AND at
 * 1920px (desktop regression). A CSS change's real proof is what J.R. sees; these assert the shape.
 * Light mode only. Do NOT print filenames/keys - these report booleans and counts.
 */

/* D1: no card-header action label wraps. A wrap = the control is taller than a single line.
 * Run on the item page with the Attachments & Links + Linked Assets cards visible. */
function d1_noWrap(){
  const ids = ['frzLinkAdd','frzAttUp','frzReqCreate'];
  const out = {};
  ids.forEach(id=>{ const el=document.getElementById(id); if(!el){ out[id]='(not present)'; return; }
    const lh = parseFloat(getComputedStyle(el).lineHeight)||18;
    out[id] = { visible: el.offsetParent!==null, wraps: el.clientHeight > lh*1.6,
                shownLabel: (el.textContent||'').trim() || (getComputedStyle(el,'::after').content) };
  });
  const title = document.querySelector('.frz-cardh-title');
  if(title){ const lh=parseFloat(getComputedStyle(title).lineHeight)||16; out['heading']={ wraps: title.clientHeight > lh*1.6 }; }
  return out; // want wraps:false everywhere at 430px
}

/* D2: the comment toolbar image control is a FLAT MARK (svg), not a colour emoji, and still inserts.
 * Open a comment composer first so the toolbar exists. */
function d2_flatImageIcon(){
  const btns = [...document.querySelectorAll('.frz-rte-tb-comment [title="Insert image"], .frz-rte-tb-modal [title="Insert image"]')];
  if(!btns.length) return 'open a comment/description composer first';
  return btns.map(b=>({ hasSvg: !!b.querySelector('svg'),
                        noEmoji: !/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/u.test(b.textContent||'') }));
}

/* D3: the List card meta line - a card WITH a type has 2 aligned segments + 1 separator; a card
 * WITHOUT a type has 1 segment and no separator (same shape). Run on the List at 430px. */
function d3_metaShape(){
  const cards = [...document.querySelectorAll('.list-card')];
  return cards.slice(0,8).map(c=>{
    const left = c.querySelector('.lc-meta-left');
    if(!left) return { hasMeta:false };
    const segs = left.querySelectorAll('.lc-mi').length;
    const seps = left.querySelectorAll('.lc-sep').length;
    // baseline check: all segments share the same offsetTop within the meta-left row
    const tops = [...left.querySelectorAll('.lc-mi, .lc-sep')].map(x=>x.offsetTop);
    const aligned = tops.length ? tops.every(t=>Math.abs(t-tops[0])<=1) : true;
    return { segments:segs, separators:seps, baselinesAligned:aligned }; // sep === max(segments-1,0)
  });
}
