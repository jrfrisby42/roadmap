# Flow — Road Off Jira (Adoption-Critical Plan)

This is DISTINCT from the feature backlog (frazil-jira-parity-notes.md).
That doc tracks nice-to-haves. THIS doc tracks the things that actually
stand between "a great tool" and "teams have left Jira for real." Most of
these are about TRUST and OPERATIONAL READINESS, not features — they're the
reason people stay on Jira even when a better tool exists.

Status as of this writing: the staged build (shell, boards, planning,
sprints, releases, Stages 1–4) is complete and verified. Attachments (2a)
pending KMS. Everything below is what's left for real adoption.

---

## Tier 0 — Operational readiness (do first; mostly housekeeping)

These make Flow something an organization can trust and maintain, vs. "a
branch one person runs in prod."

1. **Reconcile beta-shell → main — DONE.** Merged mid-June 2026. Verified
   from the running app: beta-born features (Activity Center, Saved/Manage
   Views, project color dots, notifications) are now live on the PRODUCTION
   surface (roadmap.frazil.app/), /beta still resolves from the same
   codebase, and both routes serve identical data — i.e. one reconciled
   codebase serving both the classic top-bar shell and the Flow left-rail
   shell. NOTE (verify from repo, not browser-visible): confirm main holds
   the merge, the version is bumped off 3.10.0 to a real tag (e.g. Flow
   1.0), and prod is deployed FROM main (not still from the branch).
2. **Backups + data export.** Full export of items + history (JSON/CSV) and
   a scheduled dump to S3. Partly a feature, mostly insurance: nobody
   migrates ONTO a tool they can't get data OUT of — especially a
   single-file + SQLite app maintained by one person. Also lets you tell
   another team "your data isn't trapped here." Non-negotiable before other
   teams depend on it.
3. **Attachments (2a) — DONE.** Working as of mid-June 2026. Resolved the
   full chain: us-west-2 endpoint + SigV4 signing, then SSE-KMS via a
   dedicated KMS key (frazil-flow-attachments) with the Flow backend role
   granted kms:GenerateDataKey/Decrypt on both role policy and key policy,
   bucket default encryption pointed at it, and SSE-KMS headers on the
   presign + PUT. Bucket frazil-flow-attachments, us-west-2, key
   items/{itemId}/{uuid}/{sanitized-filename}, 50 MB max, presigned PUT
   direct-to-S3, private + Block Public Access on. (Recommend a final
   end-to-end verify of upload + clipboard-paste + download if not already
   done.)
4. **Admin → its own page, not a modal panel.** Settings stays a modal
   (small fields). Admin needs real estate — user management, permissions,
   status config, export, Jira/data tools are tables and multi-step actions,
   not dialog content. Build a real /beta/admin route with a tab shell NOW
   (even if tabs start as stubs) so the Tier-1/2 admin features below have a
   home to land in. CAUTION: reconcile with the existing rail "Admin" entry
   so there's ONE admin destination, not two competing surfaces.

## Tier 1 — Trust & multi-team blockers (the real "off Jira" work)

5. **The migration plan itself.** THE big one, barely touched. "Move off
   Jira" means one of two things — decide which:
   (a) Import Jira history (issues, comments, attachments, links, closed
       archive) so Flow becomes the system of record and Jira goes
       read-only; OR
   (b) Run parallel via existing sync indefinitely (Flow as a nicer
       front-end, Jira still source of truth — which isn't really moving
       off).
   A real plan needs: what comes over, what gets archived, how keys map
   (preserve FRAZ↔Jira mapping), the cutover-day sequence, and a rollback.
   Use the existing Jira sync as the bridge: import open issues with keys
   preserved → two-way sync for a few sprints → flip Jira read-only. Until
   this exists, "off Jira" is aspirational.
6. **Permissions / access control.** Today (as observed) anyone in can edit
   anything. Fine for a trusted dev pod; NOT fine for "other teams" and
   stakeholders — which is the stated goal. Minimum viable: Viewer /
   Member / Admin. Becomes urgent the moment the intake funnel invites
   non-dev people in. A blocker for the multi-team part specifically. (Lands
   in the new Admin page.)
7. **External notification delivery (email + Teams/chat).** The in-app bell
   only works if people are IN Flow. Jira retains people by emailing and
   pinging chat. MS shop → Teams webhook ideally, email at minimum. This is
   what makes notifications TRUSTWORTHY, and trust is what makes people stop
   checking Jira "just in case." Was deliberately scoped out of Stage 3
   (in-app only) — correct for the build, real gap for adoption.

## Tier 2 — Smooths multi-team rollout (soon after cutover)

8. **Intake funnel + duplicate detection.** A "Report an issue" form (or
   email-to-item) so non-dev people can file without edit access. Pairs
   with permissions (#6) and dup detection (real dups already exist in the
   data). This is how the whole company feeds Flow without chaos.
9. **Bulk edit (beyond release-linking).** List checkboxes already exist
   and now do status + release; generalize to assignee/sprint/priority.
   Triage at multi-team scale needs it.
10. **My Work depends on Assignee actually being used.** Cultural, not
    technical: the team tracks pods, not people; Assignee is mostly empty.
    My Work (and personal accountability generally) is useless until
    assignment becomes habit. Worth a deliberate "we assign items now" push.
    - **DEPENDENCY — fix the count-pill bug as part of this push (shipped
      4.10.1, latent today).** The filter-aware PROJECTS count pills (All /
      Fraznet / HubSpot) compute via a client-side predicate
      (`_frzScopeCount`→`_frzCountMatching` in roadmap.html) that only
      *approximates* assignee and full-text (`q`) filters — assignee isn't
      reliably carried on the in-memory item blobs. The List view pill is
      unaffected (it uses the exact `/api/items` server total). Correct today
      for status/type/owner (verified live). Inert only because Assignee is
      empty and the UI rejects a non-matching assignee value — so the moment
      assignment becomes habit, filtering List by assignee makes the project
      pills quietly wrong (silently-wrong-number, no error) and disagree with
      the server-accurate List pill. **Fix:** when the active filter includes
      an approximate dimension (assignee, `q` — audit for others), have the
      project pills fall back to per-project server counts via
      `/api/items?product=<p>&…&page_size=1` reading `total` (one light query
      per project — the same server-total source the List pill trusts, so
      they agree by construction); keep the client fast path for
      status/type/owner. **Verify:** set assignee on one Fraznet + one HubSpot
      item (use a NON-released item — a released-item PUT can trigger the Jira
      FF-pull hook), filter List by that assignee, confirm the List pill and
      all three project pills match `/api/items` totals. Do NOT re-open this
      pre-emptively — it's correctly deferred until Assignee is in real use.

## Explicitly NOT blockers (don't let these crowd out the above)
Cmd+K, keyboard shortcuts, templates, prompt-on-drop boards, watch-breadth
tuning, the Watching access point, automation rules, API/webhooks. All nice,
none are why anyone stays on Jira. They live in the feature backlog.

## Suggested sequence
Tier 0 (reconcile → backups/export → attachments → admin page shell) →
Tier 1 (migration plan → permissions → external notifications) →
Tier 2 as multi-team rollout proceeds.

## The meta-point
The hardest part of leaving Jira isn't technical — people trust Jira
because it's been the source of truth for years, and trust transfers
slowly. The tool being better isn't enough; people must believe nothing
falls through the cracks during and after the switch. Reliable
notifications, a clean migration with nothing lost, and a clearly-maintained
tool (not a branch) are what build that trust. That's why migration,
permissions, and notification delivery rank above any remaining feature.
