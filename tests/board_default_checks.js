/* SHELL-DEBT-1 Part 2 (Addendum A4) regression check - copy/paste browser-console snippet.
 *
 * Guards the fix that TEAM-SCOPED the Kanban default-board key. The bug: _defaultBoardKey() was
 * 'frazil_beta_defaultboard_'+uname() - team-blind, so a default board set on one Organization
 * applied on all five (validate-on-read masked it by falling back to '__all__' where the board id
 * was absent, which is why nobody reported it). The fix keys it as
 * 'frazil_beta_defaultboard_'+_teamSlug()+'_'+uname(). It stays PER-DEVICE by design (localStorage;
 * a synced flag is Branch B, its own stage). _defaultBoardKey/_defaultBoard/_setDefaultBoard are
 * closure-scoped (not reachable from globals), so these checks drive the REAL UI + inspect the REAL
 * localStorage the real functions wrote - they need no access to the functions.
 *
 * Automated coverage for the same fix lives in tests/test_board_default_key.py (a source-presence
 * guard: it asserts the team slug is in _defaultBoardKey's source and fails on a straight revert).
 * These console checks are the BEHAVIORAL layer the pytest/backend suite cannot reach.
 *
 * HOW TO RUN CHECK 1 (team-scoped key): open Kanban on a team with >=1 board, set a default board
 * (board chip -> a board's "..." -> Set as default, or the row star), then paste and run
 * boardDefaultKeyCheck(). pass=true means the written key carries THIS team's slug. Reverting the
 * fix (drop _teamSlug()) makes it fail: the written key is 'frazil_beta_defaultboard_<user>' with no
 * slug, so keyHasTeamSlug is false.
 */
function boardDefaultKeyCheck(){
  var team = '';
  try { team = (localStorage.getItem('frazil_rm_team')||'').trim(); } catch(e){}
  if(!team) return {pass:false, err:'no frazil_rm_team in localStorage (log in first)'};
  var keys = [];
  try { for(var i=0;i<localStorage.length;i++){ var k=localStorage.key(i); if(k && k.indexOf('frazil_beta_defaultboard_')===0) keys.push(k); } } catch(e){ return {pass:false, err:'localStorage unreadable'}; }
  // the CURRENT team's scoped key must be present and must embed the slug as '_<team>_'
  var scoped = keys.filter(function(k){ return k.indexOf('frazil_beta_defaultboard_'+team+'_')===0; });
  var legacyOnly = keys.filter(function(k){ return k.indexOf('frazil_beta_defaultboard_'+team+'_')!==0; });
  var keyHasTeamSlug = scoped.length>0;
  var pass = keyHasTeamSlug;
  console.log('[boardDefaultKeyCheck] team='+team+' scopedKeys='+JSON.stringify(scoped)+' otherKeys='+JSON.stringify(legacyOnly)+' pass='+pass);
  return {pass:pass, keyHasTeamSlug:keyHasTeamSlug, scoped:scoped, other:legacyOnly, team:team};
}

/* CHECK 2 (foreign default cannot leak) and CHECK 3 (migration reads once then lapses) are NOT
 * expressible as a single-session console snippet:
 *
 * - CHECK 2 requires being logged into TWO Organizations (Flow has separate logins per team): set a
 *   default board on Org A, then load Kanban on Org B where that board id does not exist, and confirm
 *   the effective default resolves to '__all__' (Open work) rather than the foreign id. The
 *   resolution is what to assert, not merely the absence of a crash - validate-on-read is the exact
 *   line that MASKED the original symptom. This needs J.R.'s two-Organization credentials and cannot
 *   be driven from one browser session.
 *
 * - CHECK 3 requires seeding the pre-fix legacy key ('frazil_beta_defaultboard_<user>'), reloading so
 *   _defaultBoard() reads it once as the migration fallback, then setting a default and asserting (a)
 *   the team-scoped key is what _setDefaultBoard wrote and (b) the legacy key was never rewritten.
 *   _defaultBoard()/_setDefaultBoard() are closure-scoped, so this needs a real cold-load + a UI set,
 *   sequenced across a reload - not a single paste. Do it by hand when verifying, or leave it to the
 *   post-deploy live pass.
 */
