/* ITEM-FETCH-2 regression check - copy/paste browser-console procedure (behavioural layer).
 *
 * Fault (ITEM-FETCH-1): openItem resolved the id against the boot-loaded in-memory `projects`; the bell
 * poll never refreshes it, so an item created mid-session read "Item not found" until a manual refresh.
 * Fix: on a miss, openItem re-fetches /api/all ONCE, re-syncs `projects`, and retries.
 *
 * openItem is closure-scoped, but the beta wraps window.openItemPage to call it, so window.openItemPage(id)
 * drives the SAME in-app path a bell/crumb/cross-link click takes (NOT a cold load). API is a top-level
 * const reachable from the console.
 *
 * CASE 1 (headline - the reported defect). In an already-loaded Flow tab:
 *   1. Create an item on the server WITHOUT refreshing the client, e.g. in a second tab/curl, or from this
 *      console: `var r = await API.post('/api/projects', {name:'ITEMFETCH2 '+Date.now(), status:<a valid
 *      default status>});  var id = r.id;`  (it is now on the server but absent from this tab's `projects`).
 *   2. `window.openItemPage(id)` - the in-app open. Watch the Network tab: exactly ONE /api/all fires.
 *   3. After ~1s the item page opens (location = /item/<id>, #itemPageOverlay visible). pass.
 *   On the PRE-FIX build step 2 shows "Item not found" and no /api/all - the revert demonstration.
 *   Clean up the throwaway item afterwards.
 *
 * CASE 2 + 3 (bad id reports not-found, and only ONE /api/all):
 *   Pick an id that does not exist. `window.openItemPage(<badId>)` -> after one /api/all, "Item not found".
 *   Call it AGAIN -> immediate "Item not found", NO second /api/all (the per-id guard). Assert the call
 *   COUNT in the Network tab, not just the toast.
 *
 * CASE 4 (refresh failure reads as load failure, not not-found): if you can force /api/all to fail
 *   (offline, or DevTools request-block on /api/all), `window.openItemPage(<any missing id>)` shows
 *   "Could not load item ... check your connection", NOT "Item not found". If you cannot force it, say so.
 *
 * CASE 4b (a failed fetch is RETRYABLE - the follow-up fix): with /api/all still blocked, call
 *   `window.openItemPage(<same missing id>)` AGAIN. A SECOND /api/all must fire (the guard was cleared on
 *   the failed attempt) and the message must still be the load-failure one, NOT "Item not found". On the
 *   pre-fix build the second click fires NO fetch and wrongly reports not-found - the revert demonstration.
 *   Then UNBLOCK /api/all and click once more: it now succeeds and (if the id exists) opens, or reports a
 *   genuine not-found (and THAT keeps the guard, so a further click does not re-fetch).
 *
 * CASE 5 (out-of-scope / Contributor - non-negotiable): as `contributor.demo` (development), an item id
 *   outside that user's scope must still report "Item not found" after the re-fetch (scope-safe by
 *   construction: /api/all returns only readable items, so it stays absent). If no Contributor login is
 *   available, MARK UNVERIFIED - do not reason it away.
 *
 * CASE 6 (re-sync sticks): after CASE 1 opens the item, `window.openItemPage(id)` again -> opens with NO
 *   /api/all (the item is now in the in-memory set).
 *
 * CASE 7 (cold-load unregressed - NO redundant refresh): cold-load /item/<existing-id>?team=<team>. It
 *   opens directly, and the Network tab shows only boot's /api/all - NO extra fallback fetch, because
 *   `projects` is already fresh on the cold-load path.
 *
 * This stage does NOT need the collapsed rail, so it should be fully verifiable without the rail-toggle
 * tooling limitation - unlike the last several stages.
 */
