#!/bin/sh

ROOT="/mnt/us/extensions/hermes_dashboard"
CONFIG="$ROOT/config.sh"
STATE_DIR="/tmp/hermes_dashboard"
PIDFILE="$STATE_DIR/fetch.pid"
FRAMEWORK_MARKER="$STATE_DIR/framework.stopped"
LOG="/mnt/us/documents/hermes-dashboard.log"

mkdir -p "$STATE_DIR"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"
}

if [ ! -f "$CONFIG" ]; then
  log "missing $CONFIG; copy config.sh.example and edit it"
  eips 2 2 "Hermes Dashboard: config.sh missing" >/dev/null 2>&1 || true
  exit 1
fi
# shellcheck disable=SC1090
. "$CONFIG"

if [ -z "${DASHBOARD_URL:-}" ] || echo "$DASHBOARD_URL" | grep -qE 'HOST_IP|CHANGE_ME|PLACEHOLDER'; then
  log "DASHBOARD_URL is not configured (run bin/post_install.sh first)"
  eips 2 2 "Hermes Dashboard: edit config.sh" >/dev/null 2>&1 || true
  exit 1
fi

if [ -s "$PIDFILE" ]; then
  old_pid="$(cat "$PIDFILE" 2>/dev/null)"
  if [ -n "$old_pid" ] && kill -0 "$old_pid" >/dev/null 2>&1; then
    log "already running pid=$old_pid; refreshing once"
    /bin/sh "$ROOT/bin/fetch.sh" once >> "$LOG" 2>&1
    exit $?
  fi
  rm -f "$PIDFILE"
fi
rmdir "$STATE_DIR/fetch.lock" >/dev/null 2>&1 || true

# Keep the network available and suppress the stock screensaver while the dashboard owns the display.
lipc-set-prop com.lab126.cmd wirelessEnable 1 >/dev/null 2>&1 || true
if [ "${KEEP_AWAKE:-1}" = "1" ]; then
  lipc-set-prop com.lab126.powerd preventScreenSaver 1 >/dev/null 2>&1 || true
fi

: > "$FRAMEWORK_MARKER"
if [ "${STOP_FRAMEWORK:-1}" = "1" ]; then
  if /usr/bin/stop framework >/dev/null 2>&1; then
    echo "framework" > "$FRAMEWORK_MARKER"
  elif /usr/bin/stop lab126_gui >/dev/null 2>&1; then
    echo "lab126_gui" > "$FRAMEWORK_MARKER"
  fi
fi

nohup /bin/sh "$ROOT/bin/fetch.sh" loop >> "$LOG" 2>&1 &
pid=$!
echo "$pid" > "$PIDFILE"
log "started pid=$pid interval=${REFRESH_INTERVAL:-45}s"
exit 0
