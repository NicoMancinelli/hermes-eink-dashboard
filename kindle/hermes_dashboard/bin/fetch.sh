#!/bin/sh

ROOT="/mnt/us/extensions/hermes_dashboard"
CONFIG="$ROOT/config.sh"
STATE_DIR="/tmp/hermes_dashboard"
IMAGE="/tmp/hermes_dash.png"
PART="/tmp/hermes_dash.png.part"
ERROR_MARKER="$STATE_DIR/offline"
COUNT_FILE="$STATE_DIR/refresh.count"
LOCKDIR="$STATE_DIR/fetch.lock"
LOG="/mnt/us/documents/hermes-dashboard.log"

mkdir -p "$STATE_DIR"
# shellcheck disable=SC1090
[ -f "$CONFIG" ] && . "$CONFIG"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"
}

find_fbink() {
  if [ -n "${FBINK:-}" ] && [ -x "$FBINK" ]; then
    FBINK_CMD="$FBINK"
    return 0
  fi
  for candidate in \
    "$ROOT/bin/fbink" \
    "/mnt/us/extensions/FBInk/bin/fbink" \
    "/mnt/us/extensions/fbink/bin/fbink" \
    "/usr/bin/fbink"
  do
    if [ -x "$candidate" ]; then
      FBINK_CMD="$candidate"
      return 0
    fi
  done
  if command -v fbink >/dev/null 2>&1; then
    FBINK_CMD="$(command -v fbink)"
    return 0
  fi
  return 1
}

show_error() {
  message="HERMES DASHBOARD OFFLINE
Cannot reach host. Retrying automatically."
  formatted_message="**HERMES DASHBOARD OFFLINE**
Cannot reach host. Retrying automatically."
  regular="/usr/java/lib/fonts/Caecilia_LT_65_Medium.ttf"
  bold="/usr/java/lib/fonts/Caecilia_LT_75_Bold.ttf"
  if [ -f "$regular" ]; then
    font_spec="regular=$regular"
    [ -f "$bold" ] && font_spec="$font_spec,bold=$bold"
    if "$FBINK_CMD" -q -c -f -m -M -t "$font_spec,size=24,left=35,right=35,format" \
      "$formatted_message" >/dev/null 2>&1; then
      return 0
    fi
  fi
  "$FBINK_CMD" -q -c -f -m -M -S 2 "$message" >/dev/null 2>&1 || true
}

download_image() {
  rm -f "$PART"
  if command -v wget >/dev/null 2>&1; then
    wget -q -T "${DOWNLOAD_TIMEOUT:-12}" -t 1 -O "$PART" "$DASHBOARD_URL"
  elif command -v curl >/dev/null 2>&1; then
    curl -fsS --connect-timeout "${DOWNLOAD_TIMEOUT:-12}" --max-time "${DOWNLOAD_TIMEOUT:-12}" \
      -o "$PART" "$DASHBOARD_URL"
  else
    return 127
  fi
}

display_image() {
  count=0
  [ -f "$COUNT_FILE" ] && count="$(cat "$COUNT_FILE" 2>/dev/null)"
  case "$count" in ''|*[!0-9]*) count=0 ;; esac
  count=$((count + 1))
  echo "$count" > "$COUNT_FILE"

  flash=""
  every="${FULL_REFRESH_EVERY:-10}"
  case "$every" in ''|*[!0-9]*|0) every=10 ;; esac
  if [ "$count" -eq 1 ] || [ $((count % every)) -eq 0 ]; then
    flash="-f"
  fi
  # shellcheck disable=SC2086
  "$FBINK_CMD" -q -c $flash -g "file=$IMAGE,halign=CENTER,valign=CENTER,w=-1,h=-1" -W GC16 >/dev/null 2>&1
}

fetch_once_unlocked() {
  if [ -z "${DASHBOARD_URL:-}" ]; then
    log "DASHBOARD_URL is empty"
    return 1
  fi
  if ! find_fbink; then
    log "FBInk not found"
    return 1
  fi
  if download_image && [ -s "$PART" ]; then
    mv -f "$PART" "$IMAGE"
    if display_image; then
      rm -f "$ERROR_MARKER"
      log "dashboard refreshed"
      return 0
    fi
    log "FBInk could not display downloaded image"
  else
    rm -f "$PART"
    log "host unreachable: dashboard fetch failed"
  fi

  # Avoid flashing the same error every loop; redraw it only on the online->offline transition.
  if [ ! -f "$ERROR_MARKER" ]; then
    : > "$ERROR_MARKER"
    show_error
  fi
  return 1
}

fetch_once() {
  if ! mkdir "$LOCKDIR" 2>/dev/null; then
    log "refresh already in progress; skipping duplicate request"
    return 0
  fi
  fetch_once_unlocked
  result=$?
  rmdir "$LOCKDIR" >/dev/null 2>&1 || true
  return "$result"
}

trap 'rm -f "$PART"; rmdir "$LOCKDIR" >/dev/null 2>&1 || true; exit 0' INT TERM HUP

case "${1:-once}" in
  once)
    fetch_once
    ;;
  loop)
    while :; do
      fetch_once || true
      sleep "${REFRESH_INTERVAL:-45}"
    done
    ;;
  *)
    log "unknown fetch mode: $1"
    exit 2
    ;;
esac
