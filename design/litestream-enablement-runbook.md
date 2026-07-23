# Litestream enablement runbook (Frazil Flow per-team DBs)

**Status:** code shipped in **4.50.0** (server.py generator + `--sync-litestream` /
`--new-team` hook); **NOT yet enabled on prod.** This is the on-host checklist to turn
it on when ready. Until then the feature is a silent no-op (`LITESTREAM_FLOW_CONFIG`
unset).

**What it gives you:** continuous streaming of every `/data/tenants/<team>/roadmap.db`
to S3 with point-in-time restore (PITR). This is **disaster recovery**, not high
availability (a box loss still means restore + redeploy downtime). Keep the existing
6-hourly snapshot cron (`tools/backup-dbs.sh` → `db-backups/`) running alongside as an
independent backstop.

**Host:** `ubuntu@52.35.224.183`, app at `/opt/roadmap/`, DBs at `/data/tenants/`.
Sharebox already runs Litestream on this box, so the binary + S3 + instance-role
tooling exist. Flow runs as its **own** `litestream-flow` service and never touches
sharebox's config.

---

## Pre-flight (do these first)

1. **Confirm the binary + path:**
   ```bash
   ssh -i ~/.ssh/frazil-app.pem ubuntu@52.35.224.183 'which litestream && litestream version'
   ```
   If it is not at `/usr/bin/litestream`, adjust `ExecStart` in the unit below.

2. **⚠ Check for an explicit `Deny` on `s3:ListBucket`.** DEPLOYMENT.md notes the
   instance role is "denied ListBucket" (so the snapshot script can't prune). Litestream
   **requires** `ListBucket` on its prefix. **An explicit `Deny` always beats an `Allow`
   in IAM** - so if that denial is an explicit `Deny` statement covering
   `frazil-flow-backups`, the Allow in step 2 below will NOT take effect until the Deny
   is reconciled. Inspect the current role policy:
   ```bash
   aws iam list-role-policies --role-name <flow-ec2-role>
   aws iam get-role-policy --role-name <flow-ec2-role> --policy-name <policy>
   # also check attached managed policies:
   aws iam list-attached-role-policies --role-name <flow-ec2-role>
   ```
   - If the "denial" is merely the **absence** of an Allow (implicit deny) → the additive
     Allow below is sufficient.
   - If there is an **explicit `"Effect":"Deny"` on `s3:ListBucket`** for this bucket →
     scope that Deny to exclude the `litestream/*` prefix (add an
     `s3:prefix` condition, or a `NotResource`), or the replication will fail with
     `AccessDenied` on list. Do not just add the Allow and assume it works.

---

## 1. IAM: grant Litestream S3 access (prefix-scoped, instance role)

Add this **additive** inline policy to the EC2 **instance role** (the same role the app
uses for SES + the attachments/backups buckets). It is scoped to the `litestream/`
prefix only - it does not widen access to attachments (`items/*`, `intake/*`) or the
snapshot backups (`db-backups/*`).

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "LitestreamFlowObjects",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::frazil-flow-backups/litestream/*"
    },
    {
      "Sid": "LitestreamFlowList",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::frazil-flow-backups",
      "Condition": {
        "StringLike": { "s3:prefix": "litestream/*" }
      }
    }
  ]
}
```

Apply (adjust role/policy names):
```bash
aws iam put-role-policy \
  --role-name <flow-ec2-role> \
  --policy-name litestream-flow \
  --policy-document file://litestream-flow-iam.json
```

**Retention:** add an S3 lifecycle rule on the `litestream/` prefix if you want to cap
history (Litestream manages its own generations, but a lifecycle floor is cheap
insurance). Do not expire aggressively - PITR depends on the retained WAL segments.

---

## 2. `.env` (append to `/opt/roadmap/.env`, then restart the app)

```
LITESTREAM_FLOW_CONFIG=/opt/roadmap/litestream-flow.yml
LITESTREAM_S3_BUCKET=frazil-flow-backups
LITESTREAM_S3_PREFIX=litestream
# AWS_REGION is already set for SES (us-west-2); reused automatically.
# LITESTREAM_RELOAD_CMD defaults to: systemctl restart litestream-flow
```

- `LITESTREAM_FLOW_CONFIG` points at an **app-owned** path (`/opt/roadmap`, owned by
  `ubuntu`) so `--new-team` / `--sync-litestream` can write it without root.
- Prefix `litestream` keeps this separate from the cron snapshots' `db-backups/` prefix.

```bash
sudo systemctl restart roadmap    # picks up the new .env (server.py reads it at boot)
```

---

## 3. systemd unit `/etc/systemd/system/litestream-flow.service`

```ini
[Unit]
Description=Litestream (Frazil Flow per-team DBs)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
ExecStart=/usr/bin/litestream replicate -config /opt/roadmap/litestream-flow.yml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## 4. Allow `ubuntu` to restart the unit without a password

So `--new-team`'s auto-reload works non-interactively. Create
`/etc/sudoers.d/litestream-flow` (via `visudo -f`):

```
ubuntu ALL=(root) NOPASSWD: /usr/bin/systemctl restart litestream-flow, /usr/bin/systemctl reload litestream-flow
```

Then set the reload command to use sudo (append to `/opt/roadmap/.env`):
```
LITESTREAM_RELOAD_CMD=sudo systemctl restart litestream-flow
```

(Alternative if you prefer not to touch sudoers: just run `--new-team` with `sudo`; the
config is written either way, and a failed reload prints the exact command to run.)

---

## 5. Generate the config + enable the service

```bash
cd /opt/roadmap
# writes litestream-flow.yml enumerating EVERY existing team DB (incl. development/technology)
sudo -u ubuntu /opt/roadmap/venv/bin/python server.py --sync-litestream

sudo systemctl daemon-reload
sudo systemctl enable --now litestream-flow
sudo systemctl status litestream-flow --no-pager
```

---

## 6. Verify

```bash
# service healthy + streaming
sudo systemctl status litestream-flow --no-pager
journalctl -u litestream-flow -n 50 --no-pager

# Litestream sees each DB + has generations
sudo -u ubuntu litestream databases -config /opt/roadmap/litestream-flow.yml
sudo -u ubuntu litestream snapshots  -config /opt/roadmap/litestream-flow.yml /data/tenants/development/roadmap.db

# objects landing in S3 under the litestream/ prefix
aws s3 ls s3://frazil-flow-backups/litestream/ --recursive | head
```

Then prove a restore works (see below) before trusting it.

---

## 7. New-team behaviour (after enablement)

`sudo -u ubuntu /opt/roadmap/venv/bin/python server.py --new-team acme` now:
1. creates the team DB, and
2. regenerates `litestream-flow.yml` + restarts `litestream-flow`, printing
   `Litestream backup now covers this team.`

If the reload lacks permission it still writes the config and prints the command to run.
You can always reconcile config with disk by re-running `--sync-litestream`.

---

## 8. Restore a single team (PITR)

```bash
sudo systemctl stop roadmap
# restore the latest replicated state into place:
sudo -u ubuntu litestream restore -config /opt/roadmap/litestream-flow.yml /data/tenants/<team>/roadmap.db
#   or restore to a scratch path first to inspect (safer):
#   sudo -u ubuntu litestream restore -o /tmp/<team>-restore.db s3://frazil-flow-backups/litestream/<team>
# point-in-time: add  -timestamp 2026-07-23T15:04:05Z
sudo systemctl start roadmap
```

For a full-box rebuild: provision a new instance, restore `/opt/roadmap` (scp the two
files) + `.env` + this service, then `litestream restore` each team's DB from S3.

---

## 9. Disable / rollback

```bash
sudo systemctl disable --now litestream-flow
# and remove LITESTREAM_FLOW_CONFIG from /opt/roadmap/.env + restart roadmap
```
Removing `LITESTREAM_FLOW_CONFIG` returns `--new-team` / `--sync-litestream` to a no-op.
Replicated data in S3 is untouched by disabling the service.

---

## Notes

- Auth is via the **EC2 instance role** - there are never AWS keys in the config file
  or `.env` (matches the SES pattern).
- Runs as its own service; **sharebox's Litestream is untouched.**
- Reference: DEPLOYMENT.md "Continuous replication (Litestream, PITR)"; generator lives
  in `server.py` (`sync_litestream_config` / `_litestream_flow_yaml`); tests in
  `tests/test_litestream.py`.
