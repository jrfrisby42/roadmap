# Slack (and later Teams) notifications (plan + Tier 1 build)

Status: Tier 1 (Slack channel webhook) BUILDING. Tier 2 (per-user DMs) + Teams = later.
Frontend + server change. Version bump is J.R.'s.

## Architecture: one choke point
Every notification funnels through `_notify(team, recipients, ntype, item_id, item_name, message,
actor)` (server.py) - it dedupes, drops the actor, and inserts one row per recipient into the
per-team `notifications` table (the in-app bell inbox). Types: mention, assigned, reply,
watch_status, watch_comment. To add outbound Slack we append a best-effort dispatch at this ONE
function - not the ~8 call sites.

## Tier 1 (this build) - Slack Incoming Webhook, channel post
- Secret: `SLACK_WEBHOOK_<TEAM>` in server .env (mirrors ASSETHUB_API_KEY_<TEAM> - slug uppercased,
  non-alphanumerics stripped). The URL is a bearer capability; NEVER stored in config / returned by
  /api/all; only the outbound dispatch reads it; never logged.
- Config (per-team, non-secret): `slackNotify` = `{enabled: bool, types: [ntype...]}`. Default `{}`
  (OFF). When `types` is absent, all five types post.
- Dispatch: `_slack_dispatch(team, ntype, item_id, item_name, message, actor)` appended to `_notify`,
  fired ONCE per notification event (only when there is >=1 in-app recipient, so self-only actions
  never post). Gated by webhook-present AND slackNotify.enabled AND ntype in the type allowlist.
  Best-effort and NON-BLOCKING: the HTTP POST runs on a daemon thread so it adds no latency to the
  request; any failure is logged and never affects the in-app insert or the triggering mutation.
  Default OFF -> fully inert (tests + existing teams unaffected).
- Message: Slack mrkdwn `*<item name>*: <message>  <APP_BASE_URL/item/<id>|open>` (dynamic text
  escaped for Slack). One line per event.
- get_all: returns `slackNotify` (non-secret) + `slackWebhookPresent` (bool, presence only - never
  the URL), so the admin card can show state and which-half-missing.
- Admin UI: a "Slack notifications" card in the new Integrations tab - state line, an enable
  toggle, per-type checkboxes (Mentions / Assignments / Comment replies / Status changes (watched) /
  Comments (watched)), a "Send test message" button, and .env guidance. Admin-only.
- Test endpoint: `POST /api/slack/test` (admin) posts a synchronous test message and returns
  ok/error so the button gives immediate feedback.
- Config wired at all five sites (VALID_KEYS, init defaults, presence_only migration, get_all) +
  the two frontend boot loads + top-level lets.

## Config-key checklist (done in this build)
VALID_KEYS += slackNotify ; init_team_db defaults += slackNotify:{} ; presence_only_keys +=
slackNotify ; get_all += slackNotify + slackWebhookPresent ; roadmap.html: let slackNotify={},
let slackWebhookPresent=false + both boot loads.

## Tests
- slackNotify config round-trips through PUT /api/config + GET /api/all (top-level), and
  slackWebhookPresent is False with no env.
- `_slack_dispatch` is inert (returns None, no raise, no network) with no webhook configured.

## Tier 2 (later) - per-user DMs
Slack app + bot token + `users.lookupByEmail` (Flow email -> Slack user) to DM individual
recipients, matching the in-app per-recipient model. Needs the app installed in the customer
workspace + per-user opt-in. Route the same `_notify` recipients list through DMs instead of / in
addition to the channel post.

## Teams (later)
Same dispatch shape, second transport: a Power Automate "Workflow" HTTP trigger URL per team
(the legacy Office 365 webhook connector is being retired by Microsoft - use Workflows). Per-user
Teams DMs need an Azure Bot + Graph and are a separate, larger effort.

## Async note
Tier 1 uses a daemon thread per post (simple, non-blocking). If volume grows, Tier 2 should move
the dispatch to a shared queue/worker rather than a thread per event.
