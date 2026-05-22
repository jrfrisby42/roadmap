#!/bin/bash
# new-team.sh — provision a new team environment for Frazil Roadmap
# Usage: sudo /opt/roadmap/new-team.sh <teamname>
# Example: sudo /opt/roadmap/new-team.sh acme

set -e

TEAM="${1:-}"
DATA_DIR="/data/tenants"
DOMAIN="roadmap.frazil.app"

# ── Validate ──────────────────────────────────────────────────────────────────
if [ -z "$TEAM" ]; then
  echo "Usage: $0 <teamname>"
  echo "Example: $0 acme"
  exit 1
fi

# Sanitize: lowercase alphanumeric only
TEAM=$(echo "$TEAM" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9')

if [ -z "$TEAM" ]; then
  echo "Error: Invalid team name. Use lowercase letters and numbers only."
  exit 1
fi

if [ -d "$DATA_DIR/$TEAM" ]; then
  echo "Team '$TEAM' already exists at $DATA_DIR/$TEAM"
  exit 0
fi

# ── Generate a random initial password ────────────────────────────────────────
INIT_PW=$(python3 -c "import secrets; print(secrets.token_urlsafe(12))")

# ── Create team directory and seed initial password ───────────────────────────
mkdir -p "$DATA_DIR/$TEAM"
chown ubuntu:ubuntu "$DATA_DIR/$TEAM" 2>/dev/null || true

# Write the initial password so server picks it up on first /api/all for this team
echo "$INIT_PW" > "$DATA_DIR/$TEAM/.init_password"
chown ubuntu:ubuntu "$DATA_DIR/$TEAM/.init_password" 2>/dev/null || true

# Trigger DB provisioning by hitting /api/all with the new team header
# (gives server a moment to start responding if it just restarted)
sleep 1
curl -s -o /dev/null -w "" \
  -H "X-Team: $TEAM" \
  "http://localhost:8000/api/all" || true

echo ""
echo "✅ Team '$TEAM' provisioned at $DATA_DIR/$TEAM"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Team Login Details"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  URL:       https://$DOMAIN?team=$TEAM"
echo "  Team:      $TEAM"
echo "  Username:  admin"
echo "  Password:  $INIT_PW"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "⚠  Admin will be prompted to change password on first login."
echo ""
