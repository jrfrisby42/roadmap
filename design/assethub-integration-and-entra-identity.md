# Flow ↔ AssetHub integration + Entra identity (future enhancement)

**Status:** DEFERRED / design-only. No code yet. Interim: AssetHub Persons are
**created manually**; this document is the plan for automating the handoff and, later,
syncing identity from Entra. Written 2026-07 from a review of both codebases.

## Goal

A reporter on Flow's intake portal picks a type like **"IT - HW Request"**, fills out
the Flow ticket, and Flow **auto-creates a matching request in AssetHub**, storing a link
to the AssetHub request on the Flow item (and, ideally, the Flow ticket link on the
AssetHub request - bidirectional). Longer term: expose this as a first-class capability if
Flow and/or AssetHub are sold as SaaS.

## What exists today (as of this review)

**Flow (this repo, `server.py`):**
- Public intake portal at `/report` (static page) + `GET /api/intake/projects`
  (cross-team project picker), `GET /api/intake/config/{team}` (types/departments/projects),
  `POST /api/intake/{team}` (`intake_submit`) which creates the Item.
- Portal exposure is per-team config: **`intakeEnabled`** (master switch - team only
  appears when true), **`intakeProjects`** (empty = all), **`intakeTypes`** (empty = all
  team types), `intakeNotifyEmail` / `intakeProjectEmails`, `intakeDomains`, `departmentMeta`.
  All in `VALID_KEYS`, set via Admin.
- Deep-link prefill params: `/report?team=&product=&type=&dept=` (the hook a mapping would use).
- The natural place to trigger a handoff: **inside `intake_submit`, right after the Item is
  created** (same spot the confirmation emails fire - best-effort, must never fail the ticket).
- Items already have a typed-links concept + a public `/ticket` status page to surface an
  external link on.

**AssetHub (`/opt/assethub`, separate FastAPI app):**
- FastAPI + SQLAlchemy 2.x + Alembic, SQLite (Postgres-ready), multi-team via `team_id` +
  active-team selector. Runs on **`127.0.0.1:8005`** (localhost only, behind Caddy at
  `assethub.frazil.app`; 8005 is never public).
- Public submit: `GET/POST /r/{token}` → `public_request_service.submit(db, team_id, email, data)`
  → `request_service.create_public_request(db, team_id, person_id, {title, description,
  category_id, quantity, estimated_cost})`. The `/r/{token}` link is a **per-team**,
  long-lived, revocable (nonce-rotated) itsdangerous-signed token `{tid, n}` signed with
  AssetHub's `SECRET_KEY` (`app/core/tokens.py`). (The link reviewed in discussion was team
  4's public link.)
- **THE KEY CONSTRAINT - the roster gate:** `submit` requires the email to match an
  **active `Person` on that team's roster** (`match_person`); there is **no auto-create of
  people**. It's the anti-spam design. New requests enter the normal manager-approval queue.
- Request detail page: `GET /requests/{id}` → `https://assethub.frazil.app/requests/{id}`.
- Auth today is human-oriented (session cookies + itsdangerous magic links). **No first-class
  programmatic/machine API auth yet.** Entra SSO is planned (`docs/plan-entra-sso.md`) as
  **JIT `oid`-binding on first login**; Intune device sync via Graph is planned
  (`docs/plan-intune-device-sync.md`).

## Integration design (when built)

**Recommended: Path B - a small authenticated JSON endpoint on AssetHub** (the `/r/{token}`
form returns HTML, so it can't give Flow the new request's id/URL for a back-link).

- **AssetHub adds** `POST /api/integrations/requests` (or `/v1/requests`): localhost-only +
  shared-secret (HMAC-signed body + timestamp) initially; calls `create_public_request`;
  returns JSON `{id, url, number}`. Add a "source"/back-reference field on the request to
  store the Flow ticket link. (Optionally relax/parameterize the roster gate for trusted
  internal calls - see Entra section; until identity is synced, the reporter's email must
  already be a rostered Person.)
- **Flow adds** a per-team connector config, e.g. `intakeExternalTargets` =
  `{ "IT - HW Request": {system:"assethub", team_id:4, category_id:<n>} }`, and a
  **best-effort hook in `intake_submit`** (after the Item is created): if the type maps to
  AssetHub, POST to it over `127.0.0.1:8005`, passing reporter email/name, title, description,
  category, quantity, cost, and the Flow `/ticket` URL + item key. Store the returned AssetHub
  URL in a structured `externalRefs` field on the item; render it on the item page + `/ticket`.
- **Failure handling:** never block the Flow ticket; log + retry. Send an **idempotency key**
  (`flow:{team}:{item_id}`) so retries don't duplicate the AssetHub request.
- **Model it as a generic connector**, not AssetHub-specific code (mirrors Flow's Jira sync),
  so more targets (ServiceNow, etc.) don't need bespoke code.

**Path A (fallback, no AssetHub change):** Flow server-side POSTs AssetHub's `/r/{token}` form.
Reuses everything but the response is HTML → no captured request id → **no back-link**. Weaker;
use only if AssetHub can't take an endpoint.

## Same-box vs. separate-box (if they ever split)

The Path B contract is transport-agnostic; only these change when not co-located:
- **Transport:** `127.0.0.1:8005` → a **private VPC path** (security-group-scoped private IP)
  preferred, or public HTTPS through Caddy (a deliberate exposure - 8005 is private today), or
  a VPN tunnel.
- **Auth hardens:** HMAC-signed + timestamped requests (short expiry, replay-protected) + IP
  allow-list + TLS. Static bearer alone is not enough once network-reachable.
- **Reliability:** move from synchronous best-effort to an **outbox/retry** pattern (Flow
  records a pending handoff; a background retry back-fills the link). Keep the idempotency key.
- **Secrets** live on two hosts → use AWS SSM/Secrets Manager, not per-box `.env`.
- **Reverse callbacks** (AssetHub → Flow status updates) need the same network+auth+retry
  treatment in mirror.
- Unchanged: the create contract, the roster gate, team/category mapping. (CORS is N/A -
  server-to-server.)

## SaaS productization (bigger, later)

If sold as SaaS, build the foundation once so Flow↔AssetHub is the first consumer and third
parties can integrate too:
1. **Programmatic API auth** (the real gap on both): per-tenant **API keys / OAuth2
   client-credentials**, scoped + revocable, separate from user login. Both are FastAPI →
   OpenAPI docs come free; add versioning (`/v1`).
2. **Webhooks / events** (neither emits today): `request.status_changed`, `ticket.created`,
   etc. Turns the integration real-time (no polling) and opens it to third parties.
3. **Connector framework** in Flow (config-driven per tenant) so integrations aren't N×M
   bespoke code.
4. **Tenant linking:** an OAuth-style "connect your AssetHub org to your Flow workspace" flow
   establishing the tenant↔tenant mapping + issuing scoped credentials.
5. Flow's **Postgres migration + retiring `/api/all`** (see the architecture discussion /
   `wipe-class` review) - SaaS scale is the trigger. AssetHub is already Postgres-ready.

## Entra identity (the intended end-state; dissolves the roster gate)

**Decision:** identity/people come from **Microsoft Entra ID**, shared across the Frazil suite.
This makes "is this reporter a known person?" answer identically in both apps (same source of
truth), so the Flow→AssetHub handoff resolves the AssetHub Person deterministically by
email/UPN/`oid`.

Key points captured for later:
- **JIT-on-login is NOT enough for the handoff.** AssetHub's planned Entra binding creates the
  Person on first login; a Flow reporter may never have logged into AssetHub. Need **proactive
  directory provisioning** so Persons exist beforehand:
  - **Graph API pull** (scheduled sync of users/groups) - start here; reuses the Intune/Graph
    groundwork.
  - **SCIM** (Entra pushes provisioning + deprovisioning) - add later; the enterprise-standard.
- **Entra tenant id (`tid` claim) = the SaaS org/tenant anchor.** A customer's Azure AD tenant
  maps to their Flow workspace and AssetHub org; org mapping falls out of shared identity.
- **Register the app multi-tenant** in Entra (customer admin-consents in their tenant); scope
  data by `tid`.
- **Flow should also do Entra SSO** so both products derive users from the same directory →
  the integration identity join is essentially free. If Flow stays on its own login, you need
  an email-based identity bridge instead.
- **Drive team rosters/roles from Entra groups** so joiners/movers/leavers propagate with no
  manual admin.
- **Cautions:** (a) build against generic **OIDC/SAML** with Entra as the first provider so
  you're not locked to Microsoft-only customers; (b) Graph `User.Read.All` + group reads need
  **admin consent** and give you a directory copy - sync only what's needed (email, name,
  `oid`, groups) and honor deprovisioning.

Once Entra sync is in: the roster gate is satisfied automatically for staff, the "external
requester" create-person fallback narrows to true non-employees only, and tenant mapping is
inherited from identity.

## Interim (now)

- AssetHub Persons are **created manually.** No Flow→AssetHub automation yet.
- If we want a quick manual bridge before building anything: Flow's intake deep-link
  (`/report?...`) and AssetHub's `/r/{token}` public link can be shared as plain links; a
  reporter fills both. Not automated, but zero code.

## Open decisions to settle before building

1. **Does Flow also authenticate via Entra?** (If yes, the integration identity story is
   basically free; if no, build an email bridge.)
2. **Graph pull vs. SCIM** for provisioning people into AssetHub (recommend Graph first).
3. **Path A vs. B** (recommend B - the JSON endpoint - for the back-link).
4. **Roster policy** until Entra sync lands: require the reporter already be a Person (fail
   gracefully, Flow ticket still lands) vs. allow the integration to create/link a Person.
5. **Category + team mapping:** which AssetHub category "IT - HW Request" maps to, and the
   Flow-team → AssetHub-team_id map (AssetHub's category catalog can be read to propose it).
6. **SaaS or internal?** Decides whether to build the API-auth + webhook + connector
   foundation now or a simpler point integration.

## Concrete next steps when picked up

1. Read AssetHub's category catalog for the target team → propose the type→category map.
2. Add AssetHub `POST /api/integrations/requests` (shared-secret, localhost) returning
   `{id, url}` + a Flow-link back-ref field; ship via AssetHub's own repo/deploy.
3. Add Flow's `intakeExternalTargets` config + the best-effort `intake_submit` hook +
   `externalRefs` on the item + UI on the item page / `/ticket`.
4. (Later) Entra: Graph user/group sync into AssetHub Persons; Entra SSO on Flow; multi-tenant
   app registration; groups→rosters.
