# TODO-EMAIL-1 - morning To-do email (scope, not built)

Status: BUILT 2026-10-02 (branch todo-email-1). J.R.'s calls: default ON, 07:00 MT, weekdays only, no "due tomorrow"; Slack DM added as an Org-wide admin option. The CLI is `--send-todo-reminders` (covers email + Slack). Companion to TODO-DUE-1
(due To-dos pinned in the bell, 6.58.8), which only helps people who open Flow.

## Why

Reminders only exist inside Flow. On `development`, 14 of 15 reminders were never read and 16 To-dos sat
overdue, including two since Aug 20 for a user who has never read a notification. An email reaches people
who don't open the app, or who open it and never look at the bell.

## What it does

Once a day, each opted-in user with something due gets one email:

- **Subject:** "2 To-dos overdue, 1 due today" (or "1 To-do due today"). Nothing due = no email.
- **Body:** overdue first (oldest first, "Overdue - 24 days"), then due today. Each line: item key (if
  linked) + title, linking to the item, or to My Home To-dos when unlinked. Footer: how to turn it off.
- **Only the owner's own To-dos.** To-dos are private; the email goes to the To-do owner's address only.
  No notes, no URLs from the To-do itself (user-entered links are not re-emitted), titles HTML-escaped.

## How it works

| Piece | Approach |
|---|---|
| Send | Existing SES path (`send_email`, `MAIL_FROM`), same as password reset / invites. No new dependency. |
| Trigger | New `python server.py --send-todo-emails` CLI, same shape as `--send-digests`. A systemd timer runs it daily at 07:00 America/Denver (`OnCalendar=*-*-* 07:00:00 America/Denver`). |
| Selection | Per team, per user with an email + the preference on: open To-dos (`status != 'Done'`) with `due_date <= today` in MT (`_today_mt_key`). |
| Once per day | New column `todo_email_sent_on` (MT date) per user in a small per-team `user_prefs` table, set in the same transaction as the send decision, so a re-run or a second timer fire sends nothing. |
| Preference | Settings -> Notifications tab (currently a stub) gets "Email me a morning summary of To-dos due". Stored server-side per user (`user_prefs.todo_email`), new `GET/PUT /api/my/prefs`. |
| Failure | Best-effort per user; one bad address never stops the run; counts logged. |

## Also fix while here

The **IT/Ops weekly digest timer was never installed**: `--send-digests` exists, but no systemd timer runs
it on prod, so the digest has never sent. Install both timers in the same deploy step (units checked into
the repo under `tools/systemd/`, installed by hand per DEPLOYMENT.md).

## Open questions for J.R.

1. **Default on or off?** Recommendation: **on** for users who have at least one dated To-do (the
   people who'd benefit), with the opt-out in the footer and in Settings. Off-by-default is safer but in
   practice nobody turns it on.
2. **Time:** 07:00 MT?
3. **Weekends:** skip Saturday/Sunday (a Monday email then covers the weekend)?
4. **Include "due tomorrow"?** Recommendation: no - keep it to what needs action today.

## Tests

pytest: selection (Done / future / no-date excluded; MT day boundary), once-per-day idempotence, opt-out,
no-email users skipped, privacy (only the owner's To-dos, owner's address), subject/body escaping. The send
itself is mocked.

## Size

About a day: server CLI + prefs table/endpoint + email template + Settings toggle + tests, plus installing
two systemd timers on deploy (needs a server restart and `systemctl enable --now`).
