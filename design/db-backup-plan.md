# DB backup plan (off-box) — decision parked 2026-07-16

**Goal:** off-box backup of the 4 per-team SQLite DBs (`/data/tenants/<team>/roadmap.db`),
beyond EBS snapshots. Complements the in-app **Admin → Data → Full Backup**
(`GET /api/export`, shipped 4.25.0 — manual, per-team, complete JSON).

## Context discovered
- Litestream v0.3.13 is already running on the same EC2 box for **sharebox**
  (`/etc/litestream.yml`, systemd `litestream.service`), replicating to
  `s3://sharebox-frazil-backups-471112788549-us-west-2/sharebox.db`.
- Credentials come from the **EC2 instance role** (no keys in config). Replica is
  **SSE-KMS** (`arn:aws:kms:us-west-2:471112788549:key/3f115ef2-15e6-4db7-8a54-3e823a9ae06e`).
- **The existing service can't be reused as-is:** it runs `User=sharebox` with
  `ProtectSystem=strict` + `ReadWritePaths=/var/lib/litestream /opt/sharebox/data`,
  so `/data/tenants` is invisible/read-only to it → `unable to open database file`.
  (Confirmed by trial: added roadmap DBs to the shared config, got per-DB sync
  errors, **reverted** — sharebox never affected.)
- Roadmap app runs as **`User=ubuntu`**; the team DBs are owned by `ubuntu:ubuntu`.
- Reuse target chosen: the **sharebox bucket under a `roadmap/` prefix** (needs to
  confirm the bucket accepts writes to a new prefix under the same SSE-KMS key —
  first run will tell).

## Ruled out
- **Shared data-group** (let the sharebox litestream service reach roadmap data via
  a common group): still requires editing the **working shared unit**
  (`ReadWritePaths`) + per-team stanzas in the shared config → couples the apps and
  risks sharebox backups. Avoid.

## The real choice (both isolated from sharebox; both reuse binary + bucket + KMS + IAM)

### Option A — separate Litestream service (RECOMMENDED)
A second unit `litestream-roadmap.service`, `User=ubuntu`,
`ReadWritePaths=/var/lib/litestream-roadmap /data/tenants`,
`StateDirectory=litestream-roadmap`, `-config /etc/litestream-roadmap.yml`,
metrics on `:9091` (sharebox owns `:9090`). Config: 4 team DBs →
`s3://…/roadmap/<team>.db`, same 60s sync / 168h retention / SSE-KMS as sharebox.
- **Pros:** ~60s RPO, point-in-time restore, isolated from sharebox.
- **Cons:** a 2nd daemon; **per-team config stanza** must be added + `systemctl
  restart litestream-roadmap` when a new team is created (rare — add to the
  "add a team" runbook). No glob in Litestream 0.3.x.

### Option B — cron snapshot (`tools/backup-dbs.sh`, already committed)
WAL-safe `sqlite3 .backup` of every `/data/tenants/*/roadmap.db` → gzip →
`aws s3 cp` (add `--sse aws:kms --sse-kms-key-id <key>` to match the bucket), via
cron as `ubuntu`.
- **Pros:** dead simple, no daemon, **auto-discovers teams** (glob, zero
  maintenance), trivial restore (gunzip + drop in).
- **Cons:** coarser RPO (the interval, e.g. 1h), no PITR.

## Recommendation
Go with **Option A** (separate Litestream service) for PITR — the only ongoing cost
is the per-team stanza on team creation, documented in the runbook. If zero-
maintenance + hourly RPO is preferred, **Option B** is the simpler path and is
already in the repo.

## Next actions when resumed
1. Confirm bucket accepts `roadmap/` prefix writes under the shared SSE-KMS key.
2. Create `/etc/litestream-roadmap.service` + `/etc/litestream-roadmap.yml` (mirror
   sharebox, `User=ubuntu`, `:9091`), `daemon-reload`, `enable --now`.
3. Verify generations + S3 objects under `roadmap/`; run a restore drill.
4. Add the per-team-stanza step to the "add a team" runbook (CLAUDE.md).
5. Update DEPLOYMENT.md "Automated backups" to reflect the chosen approach
   (currently documents the cron script).
