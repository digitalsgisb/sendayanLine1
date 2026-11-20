#!/usr/bin/env bash
# Safely update the Sendayan Line 1 tracker: fetch, wait for a safe moment,
# fast-forward, install, test, restart, and roll back if anything fails.
#
# Usage (as root, normally via sdy1-update@<user>.service):
#   /usr/local/sbin/sdy1-update.sh <linux-user>
#
# A "safe moment" is: no active shift, or the line is in REST / PLANNED STOP.
# While the line is producing the script waits and checks again every minute.
#
# This file is installed as a COPY in /usr/local/sbin so that "git pull" never
# rewrites the script while it is running.
set -euo pipefail

APP_USER="${1:?usage: sdy1-update.sh <linux-user>}"
APP_DIR="/home/$APP_USER/sdy1"
SERVICE="sdy1@$APP_USER.service"
VENV_PY="$APP_DIR/.venv/bin/python"
BRANCH="${SDY1_UPDATE_BRANCH:-main}"
MAX_WAIT_MINUTES="${SDY1_UPDATE_MAX_WAIT_MINUTES:-720}"
LOG_FILE="${SDY1_UPDATE_LOG:-/var/log/sdy1-update.log}"

exec > >(tee -a "$LOG_FILE") 2>&1
log() { echo "$(date '+%F %T') [sdy1-update] $*"; }
as_app() { runuser -u "$APP_USER" -- env HOME="/home/$APP_USER" "$@"; }

DB_PATH="$APP_DIR/runtime/sdy1_state.db"
if [ -f "$APP_DIR/.env" ]; then
  env_db="$(grep -E '^SDY1_SQLITE_PATH=' "$APP_DIR/.env" | tail -n 1 | cut -d= -f2- | tr -d "\"'" || true)"
  if [ -n "$env_db" ]; then DB_PATH="$env_db"; fi
fi

SAFE_CHECK='
import json, os, sqlite3, sys
path = sys.argv[1]
if not os.path.exists(path):
    print("no saved state yet"); sys.exit(0)
try:
    db = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=5)
    row = db.execute("SELECT state_status, state_json FROM runtime_state WHERE line_code = ?", ("SDY1",)).fetchone()
except Exception as exc:
    print("cannot read tracker state: %s" % exc); sys.exit(2)
if row is None:
    print("no saved state yet"); sys.exit(0)
status, mode = row[0], json.loads(row[1]).get("current_mode")
if status == "NO_PROD":
    print("no active shift"); sys.exit(0)
if mode in ("REST", "PLANNED STOP"):
    print("shift %s is in %s" % (status, mode)); sys.exit(0)
print("line busy: shift %s, mode %s" % (status, mode)); sys.exit(1)
'

wait_for_safe_moment() {
  local deadline=$(( $(date +%s) + MAX_WAIT_MINUTES * 60 ))
  local reason
  while true; do
    if reason="$(python3 -c "$SAFE_CHECK" "$DB_PATH")"; then
      log "Safe to restart: $reason"
      return 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      log "Gave up after $MAX_WAIT_MINUTES minutes: $reason. Nothing was changed."
      return 1
    fi
    log "Waiting: $reason. Checking again in 60 s."
    sleep 60
  done
}

install_service_unit() {
  install -m 0644 "$APP_DIR/deploy/sdy1@.service" /etc/systemd/system/sdy1@.service
  systemctl daemon-reload
}

restart_count() { systemctl show -p NRestarts --value "$SERVICE" 2>/dev/null || echo 0; }

# Healthy = still active 20 s after the restart, and systemd did not have to
# auto-restart it (a crash loop) in the meantime.
restart_and_check() {
  systemctl restart "$SERVICE"
  local before
  before="$(restart_count)"
  sleep 20
  systemctl is-active --quiet "$SERVICE" && [ "$(restart_count)" = "$before" ]
}

rollback() {
  log "ROLLING BACK to $OLD_COMMIT: $1"
  as_app git -C "$APP_DIR" reset --hard "$OLD_COMMIT"
  as_app "$VENV_PY" -m pip install --quiet -r "$APP_DIR/requirements.txt" || true
  if [ "$UNIT_CHANGED" = "yes" ]; then install_service_unit; fi
  if [ "$RESTARTED" = "yes" ]; then
    if restart_and_check; then log "Old version is running again."; else log "WARNING: old version is not healthy either. Check: journalctl -u $SERVICE -n 100"; fi
  fi
  exit 1
}

log "=== Update check for $SERVICE (branch $BRANCH) ==="
cd "$APP_DIR"
UNIT_CHANGED="no"
RESTARTED="no"

if [ -n "$(as_app git -C "$APP_DIR" status --porcelain --untracked-files=no)" ]; then
  log "ABORT: the Pi has local code edits. Review them with 'git status' first. Nothing was changed."
  exit 1
fi

as_app git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
OLD_COMMIT="$(as_app git -C "$APP_DIR" rev-parse HEAD)"
NEW_COMMIT="$(as_app git -C "$APP_DIR" rev-parse "origin/$BRANCH")"
if [ "$OLD_COMMIT" = "$NEW_COMMIT" ]; then
  log "Already up to date at $OLD_COMMIT. Nothing to do."
  exit 0
fi
if ! as_app git -C "$APP_DIR" merge-base --is-ancestor "$OLD_COMMIT" "$NEW_COMMIT"; then
  log "ABORT: origin/$BRANCH is not a fast-forward of the Pi's commit. Nothing was changed."
  exit 1
fi
log "Update available: $OLD_COMMIT -> $NEW_COMMIT"

wait_for_safe_moment || exit 1

as_app git -C "$APP_DIR" merge --ff-only --quiet "$NEW_COMMIT"
as_app "$VENV_PY" -m pip install --quiet -r "$APP_DIR/requirements.txt" || rollback "pip install failed"

log "Running tests..."
if ! (cd "$APP_DIR" && as_app "$VENV_PY" -m unittest discover -s tests -p 'test_*.py' > /tmp/sdy1-update-tests.log 2>&1); then
  tail -n 30 /tmp/sdy1-update-tests.log
  rollback "tests failed"
fi
log "Tests passed."

if ! git -C "$APP_DIR" diff --quiet "$OLD_COMMIT" "$NEW_COMMIT" -- deploy/sdy1@.service; then
  UNIT_CHANGED="yes"
  install_service_unit
  log "Installed the updated sdy1@.service."
fi

# Tests take a little while: confirm the moment is still safe right before restarting.
wait_for_safe_moment || rollback "no safe moment to restart"

RESTARTED="yes"
if restart_and_check; then
  log "SUCCESS: $SERVICE is running $NEW_COMMIT."
else
  rollback "service did not stay running after restart"
fi
