# Public Ticket Portal — Integration & Testing Guide

The public intake portal lets **external, non-logged-in users** file tickets into a
team's roadmap. It is served at:

```
https://flow.frazil.app/report
```

No login. Submissions are protected by a CAPTCHA (Cloudflare Turnstile), a per-IP
rate limit, an optional per-team email-domain allowlist, and file-type/size limits
on attachments. A submitted ticket becomes a normal item (`source="portal"`) with
the reporter's email captured for follow-up.

---

## Part 1 — Passing the reporter's email (and other fields) via the link

When you launch the portal **from another internal tool where the user is already
signed in**, you can pre-fill the form by adding query parameters to the `/report`
URL. This is how we satisfy "the email should come from the URL when the ticket is
launched from another tool."

### Quick reference — all parameters are optional and must be URL-encoded

| Param      | Prefills           | Notes |
|------------|--------------------|-------|
| `email`    | Your email + the "Email my tickets" box | Prefilled but **editable** by the user (not locked). |
| `name`     | Your name          | Free text. |
| `product`  | Project dropdown   | Must **exactly match** (case-sensitive) a project the team exposes, or it won't preselect. |
| `team`     | (disambiguates `product`) | Team slug. Only needed if the same project name is exposed by more than one team. |
| `type`     | Type dropdown      | Must match one of the team's offered types. |
| `dept`     | Department dropdown | Must match one of the team/project's departments. |
| `priority` | Priority dropdown  | `2` = High, `3` = Medium, `4` = Low. Anything else is ignored (defaults to Medium). |

### Example

Launch pre-filled for a logged-in user filing a Fraznet bug:

```
https://flow.frazil.app/report?team=development&product=Fraznet&type=Bug&priority=2&email=jane.doe%40frazil.com&name=Jane%20Doe
```

Minimal — just carry the email through:

```
https://flow.frazil.app/report?email=jane.doe%40frazil.com
```

### How to build the link (any language)

Always URL-encode the values (an `@` becomes `%40`, spaces become `%20`, etc.).

JavaScript:
```js
const url = 'https://flow.frazil.app/report?' + new URLSearchParams({
  email: currentUser.email,      // e.g. "jane.doe@frazil.com"
  name:  currentUser.fullName,   // optional
  product: 'Fraznet',            // optional — must match an exposed project
  priority: '2',                 // optional — 2=High, 3=Medium, 4=Low
}).toString();
// → open in a new tab, or use as an <a href>
```

Python:
```python
from urllib.parse import urlencode
url = "https://flow.frazil.app/report?" + urlencode({
    "email": user_email,
    "name": user_name,
    "product": "Fraznet",
    "priority": "2",
})
```

### Behavior & security notes (important)

- **The email is a convenience prefill, not proof of identity.** The user can edit
  it, and the server does **not** verify ownership of the address. Do not treat a
  portal ticket's `reporterEmail` as an authenticated identity.
- The **domain allowlist still applies.** If the team restricts domains (Team
  Settings → Public Ticket Portal → *Allowed email domains*), a prefilled email on a
  non-approved domain will be rejected on submit with a clear message.
- **CAPTCHA still required.** Prefilling fields does not skip the Turnstile check.
- Ordering: `product` (and `team`) are applied on load; `type` and `dept` are applied
  **after** the project's config loads (they depend on the selected project), so an
  invalid `type`/`dept` for that project is silently ignored.
- The reporter can always return to **all** their tickets: the confirmation email and
  the portal's "Open the full list of tickets you've submitted" link go to a private,
  token-signed `/my-tickets` view (they request it by email; the link is emailed to
  them — it is never guessable).

---

## Part 2 — Testing the portal

### Local setup

```bash
python server.py           # serves http://localhost:8000
# open http://localhost:8000/report
```

**Turnstile in local/dev:** use Cloudflare's official test keys in your local `.env`
so the widget always renders without a real Cloudflare site. Set both and restart:

```
# .env  (LOCAL/DEV ONLY — never use these in production)
TURNSTILE_SITE_KEY=1x00000000000000000000AA        # widget always PASSES
TURNSTILE_SECRET_KEY=1x0000000000000000000000000000000AA
```

Other Cloudflare test site keys (pair with the always-pass secret above for the
`siteverify` side, or the matching test secret):
- `2x00000000000000000000AB` — widget always **blocks** (test the failure path)
- `3x00000000000000000000FF` — **forces** an interactive challenge

To test the **portal with CAPTCHA disabled**, simply leave `TURNSTILE_SITE_KEY` /
`TURNSTILE_SECRET_KEY` unset — the widget and the server check both switch off.

**Email in local/dev:** without AWS/SES configured, email sending is a no-op (the app
logs and continues). To watch what *would* send, check the server log for
`[Intake] reporter email sent …`. Ticket creation still works fully without email.

### Test checklist

**A. Submission happy path**
- [ ] Open `/report`, pick a Project, fill Summary + a valid email, complete the
      CAPTCHA, Submit → success panel shows a reference key + a "View your ticket →"
      button that opens the status page.
- [ ] The new item appears in the app for that team with `source=portal`, the chosen
      Project/Type/Priority/Department, and the reporter's name/email.

**B. CAPTCHA (Turnstile)**
- [ ] With Turnstile enabled, Submit **without** completing the widget → blocked with
      "please complete the verification."
- [ ] Direct API abuse: `POST /api/intake/{team}` with no `turnstileToken` → HTTP 403
      (server enforces it even if the page is bypassed).
- [ ] With the always-blocks test key, a completed-looking widget still fails server
      verification → 403.

**C. Domain allowlist** (Team Settings → Public Ticket Portal → Allowed email domains)
- [ ] Set e.g. `frazil.com`. Submit with `someone@gmail.com` → rejected (422, clear
      message). Submit with `someone@frazil.com` → accepted. `x@mail.frazil.com`
      (subdomain) → accepted.
- [ ] Clear the field (blank) → any email domain is accepted again.

**D. Deep-link prefill** (Part 1)
- [ ] Open `/report?email=jane%40frazil.com&name=Jane%20Doe&product=Fraznet&priority=2`
      → email, name, project, and priority are pre-selected.
- [ ] An invalid `product`/`type` in the URL is ignored (no crash, nothing preselected).

**E. Attachments**
- [ ] Attach a PNG/PDF (under the size limit) → uploads; the file is recorded on the
      ticket. A disallowed type (e.g. `.exe`) → rejected (415). Oversized → rejected (413).

**F. Confirmation & notification emails** (needs SES configured)
- [ ] Reporter receives a "We've received your ticket" email with the Flow logo and a
      working "View ticket status" link.
- [ ] The team inbox (Team Settings → notification email, or a per-project override)
      receives a copy. Per-project override wins when set.
- [ ] Mark the ticket **complete** → reporter emailed. **Defer** it → reporter emailed.
- [ ] Add an internal comment mentioning `@reporter` → reporter emailed the note.
      (Type `@` in the comment box on a portal ticket → "reporter" appears in the
      menu and inserts a blue chip.)

**G. Reporter status page & reply thread** (`/ticket?...`)
- [ ] The confirmation link opens a read-only status page (no internal fields like
      owner leak). The reporter can post a reply → it appears in the ticket's
      conversation in-app, and the ticket's **owner/assignee** get an in-app bell
      notification (plus the team email).

**H. "My Tickets" self-service** (`/my-tickets`)
- [ ] From the report page, use "Email my tickets" (or open `/my-tickets`) → enter an
      email → a private link is emailed. The link lists only that email's tickets.
- [ ] A bad/expired token → the friendly "request a link" landing, never someone
      else's tickets.

**I. Admin config persistence**
- [ ] In Team Settings → Public Ticket Portal, toggle the portal on/off, set the
      notify email, choose offered Projects/Types, set per-project emails and the
      domain allowlist → **refresh the browser** → every setting persists.

### Production smoke test (after a deploy)

```bash
curl -s https://flow.frazil.app/api/version                 # expected app version
curl -s https://flow.frazil.app/report | grep -o turnstile  # widget present when CAPTCHA is on
```
Then file one real test ticket end-to-end and confirm the confirmation email lands
(check spam if domain email auth isn't fully set up yet).

---

## Reference — related endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /report` | The public submission page. |
| `POST /api/intake/{team}` | Create a ticket (CAPTCHA + domain + type/priority validated). |
| `POST /api/intake/{team}/attach` | Presigned S3 URL for an attachment. |
| `GET /ticket?team=&id=&t=` | Token-gated reporter status page. |
| `POST /api/ticket-reply` | Reporter posts a reply (token-gated). |
| `GET /my-tickets?email=&t=` | Token-gated list of a reporter's tickets. |
| `POST /api/intake-track` | Email a reporter their private `/my-tickets` link. |

Config (Team Settings → Public Ticket Portal): `intakeEnabled`, `intakeProjects`,
`intakeTypes`, `intakeNotifyEmail`, `intakeProjectEmails`, `intakeDomains`.
Server env for CAPTCHA: `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY`.
