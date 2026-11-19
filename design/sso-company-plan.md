# Microsoft sign-in + Company level (SSO-COMPANY-1) - plan

Status: PLAN, nothing built. Written 2026-11-19 for J.R. to review.

## What J.R. decided (2026-11-19)

- Microsoft Entra, the same tenant ShareBox uses.
- A **Company** level above Organizations holds the Entra settings, so every Org in a Company signs in the
  same way and a later customer Company brings its own. Today: one Company, Frazil, with every Org on the box
  (the Marketing demo included).
- Some people stay outside Entra (contributors, outside users). They are still created by an admin and keep
  passwords exactly as today.
- Flow gets its **own** app registration (not ShareBox's). The tenant does not allow client secrets, so it uses a
  **certificate** (J.R., 2026-11-19), like ShareBox.
- No account is created by signing in: an admin adds people as now, and the first Microsoft sign-in links them
  (ShareBox's rule).

This matches the earlier note in `design/assethub-integration-and-entra-identity.md` (Entra tenant id `tid` =
the customer anchor, generic OIDC with Entra as the first provider).

## Stage 0 - J.R. in the Entra admin center (about 10 minutes)

1. App registrations > New registration: name "Frazil Flow", **single tenant** (this directory only).
2. Redirect URI (Web): `https://flow.frazil.app/api/auth/sso/microsoft/callback`
3. Certificates & secrets > Certificates > Upload certificate: the public `.cer` Flow generates (below).
4. API permissions: the default `User.Read` (delegated) is enough; we only read the sign-in result
   (`openid profile email`). No admin consent needed.
5. Send me the **tenant ID** and **client ID** (neither is secret).

Certificate: a self-signed RSA-2048 certificate (2 years) is generated ON the server; the private key stays
there (`/opt/roadmap/sso/frazil-key.pem`, owner ubuntu, mode 600, never in the repo, the chat or a backup that
leaves the box). Only the public `.cer` comes to J.R. to upload. Flow proves itself to Microsoft by signing a
5-minute "client assertion" with the key (RS256, `x5t` = the certificate thumbprint), the same as ShareBox
(`app/services/m365_cert.py`). Signing needs the `cryptography` library (not on the box today; lazily
imported like python-pptx, so without it only Microsoft sign-in is off).
6. Email domains: `freezingpointllc.com` (main) and `frazil.com`, both in the same Entra tenant (J.R., 2026-11-19).

## Stage 1 - the Company layer (server, no visible change)

- One small file on the data volume, `/data/flow-companies.json` (outside the Org databases):
  `{"companies": [{"id": "frazil", "name": "Frazil", "domains": ["freezingpointllc.com", "frazil.com"],
  "entra": {"tenantId": "...", "clientId": "...", "certKey": "sso/frazil-key.pem", "certThumbprint": "...", "certExpires": "2028-11-19"},
  "orgs": ["development", "it", ...]}]}`. Read with an mtime cache; a missing file = no Companies = no SSO
  anywhere (fail closed, today's behaviour).
- An Org belongs to at most one Company. An Org in no Company keeps passwords only.
- Operator-only (the existing `SYS_STATUS_USERS` rule), never a plain Org admin, because it reaches every Org
  in the Company. A read-only "Company" card on Admin > System: name, Orgs, domains, SSO on/off, and the
  certificate's expiry (amber 30 days before). Edited as the file for now; a screen only when a second Company is real.
- Backed up with the box (add the file to the backup set).

## Stage 2 - "Sign in with Microsoft"

Sign-in page: a "Sign in with Microsoft" button above the password form. It asks for the work email (pre-filled
if they typed one); the domain picks the Company. Unknown domain: "Use your Flow password".

Flow (stdlib HTTP; `cryptography` only to sign the certificate assertion):
1. `GET /api/auth/sso/microsoft/start?email=` -> Company by domain -> redirect to
   `login.microsoftonline.com/<tenantId>/oauth2/v2.0/authorize` (code flow + PKCE, `login_hint`, a signed
   one-time `state` + `nonce` in a short-lived httpOnly cookie).
2. `GET /api/auth/sso/microsoft/callback` -> check `state`, exchange the code at the token endpoint (urllib,
   the signed certificate assertion + PKCE), read the ID token. Checked: issuer = that Company's tenant (`tid`), audience = our
   client ID, `nonce`, expiry. The signature check is not needed because the token comes straight from
   Microsoft's token endpoint over TLS (OpenID Connect Core 3.1.3.7), so no JWT library is needed to read it.
3. Find the person in **every Org of that Company**: a user already linked (`entraOid` = the token's `oid`),
   else an unrevoked user whose email matches ANY of the token's addresses (`preferred_username` = the
   sign-in name, and `email`), so a Flow user stored as @frazil.com still matches someone Microsoft signs in as
   @freezingpointllc.com (and the reverse). An admin can link the rest by hand. First match links them (`entraOid` saved, audited). A
   Microsoft account can never reach another Company's Orgs, even with the same email.
4. No match: "You don't have a Flow account yet - ask your Flow admin to add you." Nothing created.
5. One Org: signed in. Several: an Organization picker (signed, 5-minute list), then signed in.
6. Same session as a password sign-in (same token, `localStorage` keys, httpOnly audit cookie), audited as
   `login` with `via=microsoft`.

`entraOid` is server-owned on the user record: the admin user save keeps it (like `builtin` and the avatar).

## Stage 3 - passwords and leavers

- A user **linked** to Microsoft signs in with Microsoft only: the password form answers "Use Sign in with
  Microsoft", Forgot password ignores them, and their old password is dropped. Exception: each Org's built-in
  admin keeps a password (the way back in if Microsoft or the setup breaks).
- **Outside people are untouched.** Contributors, people on other domains or without an Entra account keep
  passwords, invites and resets as today. Linking only happens on a Microsoft sign-in, so someone who never
  does keeps their password.
- Add User: an email on a Company domain offers "They'll sign in with Microsoft" (no password, the welcome
  email says to use Microsoft) next to today's two choices.
- **Leavers:** Flow sessions last 7 days and slide. A Microsoft-linked user is sent back through Microsoft once
  a day (`prompt=none`, invisible while their Microsoft 365 sign-in is live). If Entra refuses (disabled,
  deleted, password reset), they land on the sign-in page. So someone disabled in Entra loses Flow within a
  day. Revoke in Flow still works at once, as today.

## Stage 4 - Company People (the NEXT build, after SSO is live; J.R. agreed the order 2026-11-19)

- One person record per Company (name, emails, Microsoft link, avatar, one password for outside people) and a
  Company People page (operator) to add someone once and tick their Orgs, with role + Team per Org.
- Each Org keeps its own `users` list; the Company page writes into it when membership changes, so assignees,
  mentions, notifications and Teams work unchanged (no rewrite).
- First step is a read-only report: who is in which Orgs, under which usernames and emails. Merging duplicates
  (different usernames/emails for one person across Orgs) is a one-time cleanup with a dry run J.R. checks.

## Stage 5 - later, only when needed (not in this build)

- A second Company: its admin approves Flow's app (registration switched to multi-tenant), or gives us their
  own app registration (Stage 1 already supports that).
- Company screen (add a Company, move Orgs).
- Entra groups -> Org membership/roles; a Graph sync that pre-creates people (needs admin consent).

## Tests and checks

- pytest: Company file parsing + fail closed; domain -> Company; the callback with a faked token endpoint
  (wrong tenant, wrong audience, bad nonce, expired, replayed state); match by oid, then email; never across
  Companies; revoked users refused; picker; `entraOid` survives a user save; linked users refused at the
  password form except the built-in admin; the daily re-check.
- Live: J.R. signs in with Microsoft on one Org, then two, then a contributor with a password.

## Rollout

Stage 1 + 2 + 3 in one build, deployed with the Company file listing **one** Org first (development), so the
other Orgs keep passwords until J.R. has signed in for real; then add the rest to the file (no deploy needed).
