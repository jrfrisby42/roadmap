# AI in the text boxes ("Copilot") - feasibility + plan + tiny proof

Status: EXPLORATION. Nothing built or wired yet.
Prompted by: "my company all have Copilot licenses - what would this take?"

## Decisions (2026-08-03)
- Backend = ANTHROPIC CLAUDE API (this stack's default), NOT Azure OpenAI. Note: this means the
  M365 Copilot seats are not used by the app at all (see licensing reality below); the "Copilot"
  framing was the prompt, the implementation is Claude.
- Access = ADMINS ONLY. The proxy endpoint is gated `require_role("admin")` and the toolbar button
  renders only for admins. (Widen to editors later if wanted.)
- Data-governance sign-off is owned by the admins per this decision; item content (which can carry
  customer / PII data) would be sent to the Anthropic API (a third party) - confirm that boundary
  is acceptable before enabling on a real team.
- Still pending to run a LIVE proof: an `ANTHROPIC_API_KEY` provisioned + `aiEnabled` set on a team.

---

## The licensing reality (the crux)

Microsoft 365 Copilot seats are END-USER licenses for Microsoft's own surfaces (Copilot chat,
Word/Excel/Teams/Outlook, the Edge sidebar). They do NOT come with an API a third-party web app
like Flow can call. So "everyone has a Copilot license" does not translate into "Flow can use
Copilot in its text boxes." Putting AI INSIDE Flow's Description / Notes / Resolution / comment
editors requires a programmatic LLM API, provisioned and BILLED SEPARATELY from those seats.

VERIFY before budgeting (Microsoft licensing + pricing change): confirm this distinction against
current Microsoft docs, and pull current Azure OpenAI model SKUs / regions / per-token rates.

Realistic backends:
- Azure OpenAI Service - natural fit to stay in the Microsoft tenant (data stays in your Azure
  compliance boundary; SSO/governance you already run). Separate Azure subscription, per-token
  usage billing (NOT covered by Copilot seats).
- Anthropic Claude API - what this app's stack already leans toward; also per-token.

Either way: a metered API + a key held server-side. The Copilot seats stay useful for people,
just not for the app.

### Two alternatives worth knowing
- Zero-build, uses the seats today: people draft in the Edge Copilot sidebar / Copilot chat and
  paste into Flow. Free, no integration - just not inline.
- Reverse direction (DOES use the M365 seats): a Copilot declarative agent / Graph connector so
  people can ask about Flow items from inside Teams/Copilot. Surfaces Flow data in Copilot rather
  than putting AI in Flow's text boxes - a different UX for a different need. Separate effort.

---

## What it takes in Flow (the app is well-positioned)

The app already has every pattern this needs:

1. Backend proxy endpoint in `server.py` - e.g. `POST /api/ai/complete` that HOLDS THE KEY and
   forwards a prompt to the model over `urllib` (exactly like the hand-rolled Jira HTTP). Role-gated
   (admin/editor), rate-limited, key in `.env` (like `JIRA_*` / `ASSETHUB_*`), behind a per-team
   `aiEnabled` config flag that DEGRADES GRACEFULLY when unset (same as Jira/AssetHub). The key is
   NEVER exposed client-side.
2. Editor UI - the Tiptap toolbar is config-driven (`_frzBuildToolbar` + custom nodes), so a small
   "AI" menu is a clean extension. High-value actions: Draft from a title, Summarize, Improve
   writing, and the IT/Ops win - Suggest a resolution from the item's activity + comments.
   Insert-or-replace UX; streaming optional.
3. Config + governance (the real gating items):
   - Recurring TOKEN COST (usage-based; roughly pennies per short-text action - verify current
     Azure rates before budgeting).
   - DATA GOVERNANCE sign-off: item content (which can include customer / PII data) would be sent
     to the model. Confirm the compliance boundary (Azure OpenAI keeps it in-tenant; Anthropic API
     is a third party). Add an audit line per call (reuse `write_audit`).
   - A master `aiEnabled` per-team flag + who can use it (role gate).

Effort for a solid v1 (the full action set): ~a few days - roughly a day for the proxy endpoint +
config/flag, a day or two for the toolbar actions + insert/replace UX + streaming, plus prompt
tuning and degrade-when-disabled tests. No new framework, no build step; fits the two-file model.

---

## The tiny proof (drafted here - implement on greenlight)

Goal: ONE "Draft description" button -> proxy -> model -> inserts text into the item-page
Description editor. Backend-agnostic; the two sketches below show the shape. NOT yet wired into
server.py / roadmap.html - illustrative, to be implemented once a backend is chosen.

### Server sketch (server.py) - Anthropic Claude variant (chosen), admin-gated
```python
# .env:  ANTHROPIC_API_KEY=...   (per-team aiEnabled is a config flag, not .env)
# Model: default to the latest capable Claude model id at build time (verify current id).
import os, json, urllib.request

@app.post("/api/ai/complete")
def ai_complete(body: dict = Body(...), auth: dict = Depends(require_role("admin"))):   # admins only
    team = auth["team"]
    if not _cfg_val(team, "aiEnabled", False):
        raise HTTPException(400, "AI features are not enabled for this team.")
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise HTTPException(503, "AI backend is not configured.")
    # TODO: rate-limit per user/team (reuse the login limiter shape).
    action = (body.get("action") or "draft")[:40]
    prompt = (body.get("prompt") or "")[:8000]     # cap input; server owns the system prompt
    system = {
        "draft":     "Draft a concise work-item description from the user's title/notes. Plain, no preamble.",
        "summarize": "Summarize the text in 2-4 sentences. No preamble.",
        "improve":   "Improve clarity and grammar. Keep meaning and length similar. Return only the text.",
        "resolve":   "Given the activity and comments, suggest a short resolution summary.",
    }.get(action, "Assist with the user's text. Return only the result.")
    payload = json.dumps({
        "model": "claude-sonnet-4-5",              # verify the current model id at build time
        "max_tokens": 600, "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=payload, method="POST",
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        text = "".join(b.get("text", "") for b in (data.get("content") or []) if b.get("type") == "text").strip()
    except Exception as e:
        raise HTTPException(502, f"AI request failed: {e}")
    write_audit(team, "ai:complete", auth["username"], changes={"action": action})
    return {"text": text}
```
Note: the exact current Claude model id + `anthropic-version` should be confirmed at build time
(the claude-api reference). Key is server-side only; never shipped to the client.

### Client sketch (roadmap.html, beta module) - one Draft button on the Description editor
```js
// Added to the description toolbar (via _frzBuildToolbar opts.buttons) when aiEnabled.
async function _frzAiDraftDescription(ed, p){
  var title = (p && p.name) || '';
  var seed  = ed ? ed.getText().slice(0, 4000) : '';       // current text as seed, if any
  try {
    var r = await API.post('/api/ai/complete',
      { action:'draft', prompt:('Title: '+title+'\n\nNotes so far:\n'+seed) });
    if (r && r.text){
      ed.chain().focus().insertContent(_call('esc', r.text).replace(/\n/g,'<br>')).run();  // insert at cursor
      _call('showToast','Draft inserted - review before saving');
    }
  } catch(e){ _call('showToast','AI draft failed: '+((e&&e.message)||e),'error'); }
}
```
Gate: render the button only when `aiEnabled` (loaded from `/api/all` config) is true AND the
user is an admin (`isAdmin`). Degrades to nothing when disabled - identical to the Jira/AssetHub
gating. Server still enforces `require_role("admin")` regardless of the client.

### Proof acceptance
Enable `aiEnabled` + set the `.env` backend on one local team; click "Draft description" on an
item with a title; confirm text is fetched via the proxy (key never in the client), inserted into
the editor, and NOT auto-saved (user reviews first). Confirm the button is absent when disabled.

---

## Open decisions

1. Backend - RESOLVED: Anthropic Claude API.
2. Access - RESOLVED: admins only.
3. Go/no-go on wiring the tiny proof into the app - PENDING: needs an `ANTHROPIC_API_KEY`
   provisioned + `aiEnabled` set on a team so it can be verified end to end.

## Cost / governance checklist (before enabling on a real team)
- [ ] Pull current Anthropic model id + per-token rates for budgeting (claude-api reference).
- [ ] Confirm the compliance boundary is acceptable: item content (possible PII) is sent to the
      Anthropic API, a third party. (Admins own this sign-off per the 2026-08-03 decision.)
- [ ] Provision `ANTHROPIC_API_KEY` (server .env) + set the per-team `aiEnabled` flag.
- [ ] Add per-user/team rate limiting + the per-call `write_audit` (in the sketch).
