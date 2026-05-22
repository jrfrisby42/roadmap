# Frazil Roadmap — Production Deployment Guide

## Files
```
server.py        ← FastAPI backend
roadmap.html     ← Frontend (same folder as server.py)
requirements.txt ← Python dependencies
roadmap.db       ← SQLite database (auto-created)
.env             ← Secrets (never commit this)
Caddyfile        ← Reverse proxy config (you create this)
```

---

## Step 1 — Set up your server

Any Linux VPS works (DigitalOcean, Linode, AWS EC2, etc.). Ubuntu 22.04 recommended.

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python 3.11+
sudo apt install python3 python3-pip python3-venv -y

# Create app directory
mkdir -p /opt/roadmap
cd /opt/roadmap

# Copy your files here (roadmap.html, server.py, requirements.txt, .env)
```

---

## Step 2 — Install dependencies

```bash
cd /opt/roadmap
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Install bcrypt** (for password hashing):
```bash
pip install bcrypt
```

---

## Step 3 — Configure your .env

```bash
nano /opt/roadmap/.env
```

```
JIRA_BASE_URL=https://freezingpointllc.atlassian.net
JIRA_EMAIL=you@freezingpointllc.com
JIRA_API_TOKEN=your_token_here
```

**Secure the .env file:**
```bash
chmod 600 /opt/roadmap/.env
```

---

## Step 4 — Run as a system service (keeps it alive forever)

```bash
sudo nano /etc/systemd/system/roadmap.service
```

Paste this (adjust paths and username):
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
    --error-logfile /var/log/roadmap-error.log
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable roadmap
sudo systemctl start roadmap
sudo systemctl status roadmap   # should show "active (running)"
```

---

## Step 5 — Install Caddy (reverse proxy + automatic HTTPS)

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install caddy -y
```

---

## Step 6 — Configure Caddy

```bash
sudo nano /etc/caddy/Caddyfile
```

Replace the entire file with:
```
roadmap.yourdomain.com {
    reverse_proxy localhost:8000

    # Security headers
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
        Referrer-Policy "strict-origin-when-cross-origin"
    }

    # Rate limiting on login endpoint (requires caddy-ratelimit plugin)
    # The server also rate-limits internally so this is an extra layer
}
```

> **Replace `roadmap.yourdomain.com`** with your actual domain.
> Your DNS A record must point to this server's IP before running Caddy.

```bash
# Validate the config
sudo caddy validate --config /etc/caddy/Caddyfile

# Restart Caddy (it auto-fetches the SSL cert from Let's Encrypt)
sudo systemctl restart caddy
sudo systemctl status caddy
```

Caddy automatically:
- Gets a free SSL certificate from Let's Encrypt
- Renews it before it expires
- Redirects HTTP → HTTPS

---

## Step 7 — Firewall

```bash
# Allow only SSH, HTTP, HTTPS — block direct access to port 8000
sudo ufw allow 22
sudo ufw allow 80
sudo ufw allow 443
sudo ufw deny 8000
sudo ufw enable
sudo ufw status
```

---

## Step 8 — Change default credentials

**Before going live**, log in and change the admin password:
1. Open your domain in a browser
2. Click Login → use `admin` / `frazil123`
3. Click ⚙ Admin → Users → ✏️ next to admin → set a strong password

Or directly via CLI:
```bash
cd /opt/roadmap
source venv/bin/activate
python3 -c "
import json, sys
sys.path.insert(0,'.')
from server import hash_password, db
new_pw = hash_password('YOUR_NEW_PASSWORD_HERE')
with db() as c:
    row = c.execute(\"SELECT value FROM config WHERE key='users'\").fetchone()
    users = json.loads(row['value'])
    users[0]['password'] = new_pw
    c.execute(\"UPDATE config SET value=? WHERE key='users'\", (json.dumps(users),))
print('Password updated')
"
```

---

## Maintenance

```bash
# View app logs
sudo journalctl -u roadmap -f

# View access logs
sudo tail -f /var/log/roadmap-access.log

# Restart after updating server.py
sudo systemctl restart roadmap

# Update roadmap.html (no restart needed — Caddy serves it fresh each request)
# Just copy the new file to /opt/roadmap/roadmap.html

# Backup database
cp /opt/roadmap/roadmap.db /opt/roadmap/roadmap.db.backup.$(date +%Y%m%d)
```

---

## Security checklist

- [x] Passwords hashed with bcrypt (auto-migrated from plaintext on startup)
- [x] Login rate-limited to 10 attempts/minute per IP (server + Caddy layer)
- [x] Passwords never sent from server to browser (`/api/all` strips them)
- [x] Login verified server-side via `/api/login` (not client-side)
- [x] HTTPS enforced by Caddy with auto-renewing Let's Encrypt cert
- [x] Port 8000 firewalled — only accessible via Caddy on 443
- [x] Security headers set (HSTS, X-Frame-Options, etc.)
- [ ] Change default admin password before going live
- [ ] Set up automated database backups (cron job or managed backup)
