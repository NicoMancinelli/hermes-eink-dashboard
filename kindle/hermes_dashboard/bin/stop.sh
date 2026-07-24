#!/bin/sh

STATE_DIR="/tmp/hermes_dashboard"
PIDFILE="$STATE_DIR/fetch.pid"
FRAMEWORK_MARKER="$STATE_DIR/framework.stopped"
LOG="/mnt/us/documents/hermes-dashboard.log"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"
}

if [ -s "$PIDFILE" ]; then
  pid="$(cat "$PIDFILE" 2>/dev/null)"
  if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then
    kill "$pid" >/dev/null 2>&1 || true
    for _ in 1 2 3 4 5; do
      kill -0 "$pid" >/dev/null 2>&1 || break
      sleep 1
    done
    if kill -0 "$pid" >/dev/null 2>&1; then
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
  fi
  rm -f "$PIDFILE"
fi

lipc-set-prop com.lab126.powerd preventScreenSaver 0 >/dev/null 2>&1 || true

framework="$(cat "$FRAMEWORK_MARKER" 2>/dev/null)"
case "$framework" in
  lab126_gui) /usr/bin/start lab126_gui >/dev/null 2>&1 || true ;;
  framework) /usr/bin/start framework >/dev/null 2>&1 || true ;;
  *) /usr/bin/start framework >/dev/null 2>&1 || /usr/bin/start lab126_gui >/dev/null 2>&1 || true ;;
esac
rm -f "$FRAMEWORK_MARKER" "$STATE_DIR/offline" "$STATE_DIR/refresh.count"
rmdir "$STATE_DIR/fetch.lock" >/dev/null 2>&1 || true
log "stopped; Amazon framework restored"
exit 0
