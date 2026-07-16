# DEPLOYMENT.md

Production deployment and operations reference for Frazil Roadmap.

---

## Production environment

| | |
|---|---|
| Host | `ubuntu@52.35.224.183` (Elastic IP, doesn't change) |
| Domain | `https://roadmap.frazil.app` |
| Instance type | `t4g.small` (ARM64, 2 vCPU, 2 GB RAM) |
| OS | Ubuntu 24.04 LTS |
| Reverse proxy | Caddy 2.x (auto-SSL via Let's Encrypt) |
| App server | gunicorn + uvicorn workers |
| Process manager | systemd |
| App path | `/opt/roadmap/` |
| Data path | `/data/tenants/<team>/roadmap.db` (separate EBS volume mounted at `/data`) |
| Python | system python3 + venv at `/opt/roadmap/venv/` |
| SSH key | `~/.ssh/frazil-app.pem` (kept on the maintainer's local machine) |

The data lives on its own EBS volume so it survives instance replacement. Daily EBS snapshots via AWS Backup.

---

## Deploying changes

The repository has two deployable files: `server.py` and `roadmap.html`.

### Deploy via Claude

The standard workflow is to ask Claude to deploy. Variables Claude uses:

```
KEY=C:\Users\JRFrisby\.ssh\frazil-app.pem     (Windows) or ~/.ssh/frazil-app.pem (mac/Linux)
HOST=ubuntu@52.35.224.183
```

**Always back up the current file on the server before overwriting** (the prior `.bat` scripts did this — keep doing it):

```bash
ssh -i "$KEY" "$HOST" "sudo mkdir -p /opt/roadmap/bkup && sudo cp /opt/roadmap/server.py /opt/roadmap/bkup/server-$(date +%Y%m%d-%H%M%S).py"
```

### Full deploy (server.py + roadmap.html)

```bash
ssh -i "$KEY" "$HOST" "sudo mkdir -p /opt/roadmap/bkup && sudo cp /opt/roadmap/server.py /opt/roadmap/bkup/server-$(date +%Y%m%d-%H%M%S).py && sudo cp /opt/roadmap/roadmap.html /opt/roadmap/bkup/roadmap-$(date +%Y%m%d-%H%M%S).html"
scp -i "$KEY" server.py roadmap.html "$HOST:/opt/roadmap/"
ssh -i "$KEY" "$HOST" "sudo systemctl restart roadmap"
```

### Selective deploys

- **HTML only** — no restart needed. Caddy reads the file fresh on each request.

  ```bash
  ssh -i "$KEY" "$HOST" "sudo mkdir -p /opt/roadmap/bkup && sudo cp /opt/roadmap/roadmap.html /opt/roadmap/bkup/roadmap-$(date +%Y%m%d-%H%M%S).html"
  scp -i "$KEY" roadmap.html "$HOST:/opt/roadmap/"
  ```

- **server.py only** — restart required.

  ```bash
  ssh -i "$KEY" "$HOST" "sudo mkdir -p /opt/roadmap/bkup && sudo cp /opt/roadmap/server.py /opt/roadmap/bkup/server-$(date +%Y%m%d-%H%M%S).py"
  scp -i "$KEY" server.py "$HOST:/opt/roadmap/"
  ssh -i "$KEY" "$HOST" 'sudo systemctl restart roadmap'
  ```

---

## On-host operations

SSH in: `ssh -i ~/.ssh/frazil-app.pem ubuntu@52.35.224.183`

### Service control

```bash
sudo systemctl status roadmap
sudo systemctl restart roadmap
sudo systemctl stop roadmap
sudo systemctl start roadmap
sudo systemctl enable roadmap     # already enabled on boot
```

### Logs

```bash
# Application access/errors (gunicorn)
sudo tail -f /var/log/roadmap-access.log
sudo tail -f /var/log/roadmap-error.log

# systemd journal (includes startup output, crashes)
sudo journalctl -u roadmap -n 200
sudo journalctl -u roadmap -f

# Caddy
sudo journalctl -u caddy -f
```

### Reload Caddy after editing the Caddyfile

```bash
sudo nano /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

### Database

```bash
sudo sqlite3 /data/tenants/development/roadmap.db
# .tables
# .schema projects
# SELECT id, json_extract(data, '$.name') FROM projects LIMIT 10;
# .quit
```

The DB uses WAL mode. To copy it safely while the app is running, use `.backup`:

```bash
sudo sqlite3 /data/tenants/development/roadmap.db ".backup /tmp/snapshot.db"
```

### Add a new team

```bash
cd /opt/roadmap
sudo -u ubuntu /opt/roadmap/venv/bin/python server.py --new-team acme
# prints the initial admin password — capture it
```

Then share `https://roadmap.frazil.app?team=acme` with the team.

---

## Reference: systemd unit

Lives at `/etc/systemd/system/roadmap.service`:

```ini
[Unit]
Description=Frazil Roadmap
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/roadmap
ExecStart=/opt/roadmap/venv/bin/gunicorn server:app \
    -w 2 \
    -k uvicorn.workers.UvicornWorker \
    --bind 127.0.0.1:8000 \
    --access-logfile /var/log/roadmap-access.log \
    --error-logfile /var/log/roadmap-error.log \
    --timeout 60
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

If the service fails right after a deploy with permission errors on the log files:

```bash
sudo touch /var/log/roadmap-access.log /var/log/roadmap-error.log
sudo chown ubuntu:ubuntu /var/log/roadmap-access.log /var/log/roadmap-error.log
sudo systemctl restart roadmap
```

---

## Reference: Caddyfile

`/etc/caddy/Caddyfile`:

```
roadmap.frazil.app {
    reverse_proxy localhost:8000
}
```

Caddy automatically obtains and renews the Let's Encrypt cert. Multiple apps on the same host get additional blocks pointing at additional ports:

```
otherapp.frazil.app {
    reverse_proxy localhost:8001
}
```

---

## Adding a second app on the same host

Each app gets its own `/opt/<name>/` directory, its own venv, its own systemd unit on a unique port (8001, 8002, …), and a corresponding Caddy block. The pattern:

```bash
sudo mkdir /opt/app2 && sudo chown ubuntu:ubuntu /opt/app2
cd /opt/app2
python3 -m venv venv
source venv/bin/activate
pip install fastapi "uvicorn[standard]" gunicorn
# scp the files in, copy the systemd template, change port + service name
sudo systemctl enable app2 && sudo systemctl start app2
```

Then add a Caddy block and `sudo systemctl reload caddy`.

---

## Backups & data safety

- **EBS snapshots** of the `/data` volume run daily via AWS Backup.
- The app supports a full **Export** (JSON dump of projects + config) and **Import** from the admin UI — keep periodic exports as belt-and-suspenders.
- SQLite WAL files (`*.wal`, `*.shm`) live next to the DB; don't copy the DB without checkpointing or using `.backup`.

---

## Configuration via `.env`

`.env` lives at `/opt/roadmap/.env`. Read on startup by the inline parser in `server.py`. Variables:

| Var | Purpose |
|---|---|
| `JIRA_BASE_URL` | Atlassian site, e.g. `https://freezingpointllc.atlassian.net` |
| `JIRA_EMAIL` | Account used as the Jira API identity |
| `JIRA_API_TOKEN` | Generated at id.atlassian.com → Security → API tokens |
| `TOKEN_SECRET` | 64-char hex used to sign auth tokens. Generate with `python3 -c "import secrets; print(secrets.token_hex(32))"`. |
| `CORS_ORIGINS` | Comma-separated list of allowed origins. Set to `https://roadmap.frazil.app` in prod. |

Changing `.env` requires a service restart.

---

## Cost estimate (reference)

| Item | Monthly |
|---|---|
| `t4g.small` on-demand | ~$12 |
| 20 GB gp3 root | ~$1.60 |
| Separate /data EBS (8 GB gp3) | ~$0.65 |
| Elastic IP (attached) | $0 |
| EBS snapshots | ~$0.10 |
| Data transfer | $1–2 |
| **Total** | **~$15–17/mo** |

Reserved instance (1yr) brings the compute to ~$7.50/mo.

---

## Automated database backups (off-box → S3)

`tools/backup-dbs.sh` takes a WAL-safe hot backup (`sqlite3 .backup`) of every
`/data/tenants/*/roadmap.db`, gzips it, and uploads to
`s3://$BUCKET/db-backups/<team>/<UTC-timestamp>.db.gz`. Auto-discovers teams, so
new teams need no config change. This is the off-box insurance beyond EBS
snapshots (and complements the in-app **Admin → Data → Full Backup**, which is a
manual, per-team JSON via `GET /api/export`).

**One-time setup:**
1. **Bucket + IAM.** Use a dedicated bucket (recommended), e.g. `frazil-flow-backups`,
   and grant the EC2 instance role `s3:PutObject` on `arn:aws:s3:::frazil-flow-backups/db-backups/*`.
   (The attachments-bucket grant is prefix-scoped to `items/*`+`intake/*`, and the
   role is denied `ListBucket` — don't assume backups can reuse it; the first run
   surfaces `AccessDenied` if the policy is missing.)
2. **Install the script:**
   ```bash
   scp tools/backup-dbs.sh ubuntu@52.35.224.183:/tmp/
   ssh ubuntu@52.35.224.183 'sudo mv /tmp/backup-dbs.sh /opt/roadmap/ && sudo chmod +x /opt/roadmap/backup-dbs.sh'
   ```
3. **Verify a first run** (surfaces any IAM issue immediately):
   ```bash
   ssh ubuntu@52.35.224.183 'sudo BUCKET=frazil-flow-backups /opt/roadmap/backup-dbs.sh'
   ```
4. **Schedule (cron, every 6h):** `sudo crontab -e` →
   ```
   0 */6 * * * BUCKET=frazil-flow-backups /opt/roadmap/backup-dbs.sh >> /var/log/roadmap-backup.log 2>&1
   ```
5. **Retention:** add an S3 lifecycle rule on the `db-backups/` prefix (e.g. expire
   after 30 days). The role is denied `ListBucket`, so the script cannot prune.

**Restore:** `aws s3 cp s3://…/<team>/<ts>.db.gz .`, `gunzip`, stop the service,
replace `/data/tenants/<team>/roadmap.db`, start the service. (Test restores
periodically — an untested backup is a hope, not a backup.)

**Upgrade path (continuous / PITR):** for near-zero-loss recovery, Litestream can
stream each DB to S3 with point-in-time restore. It needs a per-DB config entry
(regenerated when a team is added), so the cron snapshot above is the simpler
baseline; add Litestream if the recovery-window requirement tightens.
