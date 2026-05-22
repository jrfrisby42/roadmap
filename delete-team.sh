#!/bin/bash
# delete-team.sh — remove a team environment from Frazil Roadmap
# Usage: sudo /opt/roadmap/delete-team.sh <teamname>
# Example: sudo /opt/roadmap/delete-team.sh acme
#
# This moves the team's data directory to a dated backup folder
# under /data/tenants/_deleted/ rather than permanently deleting it.
# To permanently delete: rm -rf /data/tenants/_deleted/acme.YYYYMMDD

set -e

TEAM="${1:-}"
DATA_DIR="/data/tenants"
DELETED_DIR="$DATA_DIR/_deleted"

# ── Validate ──────────────────────────────────────────────────────────────────
if [ -z "$TEAM" ]; then
  echo "Usage: $0 <teamname>"
  echo "Example: $0 acme"
  exit 1
fi

# Sanitize
TEAM=$(echo "$TEAM" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9')

if [ -z "$TEAM" ]; then
  echo "Error: Invalid team name."
  exit 1
fi

# Prevent deleting development (the default team)
if [ "$TEAM" = "development" ]; then
  echo "Error: Cannot delete the 'development' team."
  exit 1
fi

TEAM_DIR="$DATA_DIR/$TEAM"

if [ ! -d "$TEAM_DIR" ]; then
  echo "Team '$TEAM' not found at $TEAM_DIR"
  exit 1
fi

# ── Confirm ───────────────────────────────────────────────────────────────────
echo ""
echo "⚠  This will archive team '$TEAM' and remove it from the login dropdown."
echo "   Data will be moved to: $DELETED_DIR/$TEAM.$(date +%Y%m%d)"
echo "   It can be restored by moving it back to $DATA_DIR/$TEAM"
echo ""
read -p "Type the team name to confirm deletion: " CONFIRM

if [ "$CONFIRM" != "$TEAM" ]; then
  echo "Cancelled — team name did not match."
  exit 1
fi

# ── Archive ───────────────────────────────────────────────────────────────────
mkdir -p "$DELETED_DIR"
ARCHIVE_NAME="$TEAM.$(date +%Y%m%d_%H%M%S)"
mv "$TEAM_DIR" "$DELETED_DIR/$ARCHIVE_NAME"

echo ""
echo "✅ Team '$TEAM' has been archived."
echo "   Location: $DELETED_DIR/$ARCHIVE_NAME"
echo ""
echo "To restore:  mv $DELETED_DIR/$ARCHIVE_NAME $DATA_DIR/$TEAM"
echo "To permanently delete:  rm -rf $DELETED_DIR/$ARCHIVE_NAME"
