#!/bin/sh
# start_interactive.sh — Launch the interactive Python client on a Kindle.
#
# Prerequisite: Python 3 must be installed on the Kindle. The standard
# community path is via the `mrpackage` package manager:
#
#   mrpackage install kindle-python3
#
# After installation, this script locates the interpreter via:
#   1. /mnt/us/python3/bin/python3   (mrpackage install location)
#   2. python3 in PATH
#   3. python in PATH (rare; only on developer firmwares)
#
# Once Python 3 is found, this script sources config.sh, picks the right
# input devices (5-way on /dev/input/event0, touch on /dev/input/event1),
# and exec()s the interactive client.

set -eu

ROOT="/mnt/us/extensions/hermes_dashboard"
CONFIG="$ROOT/config.sh"
CLIENT="$ROOT/bin/interactive_client.py"
STATE_DIR="/tmp/hermes_dashboard"
PIDFILE="$STATE_DIR/interactive.pid"
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

if [ -z "${HOST_IP:-}" ] || [ "${HOST_IP}" = "PLACEHOLDER.lan" ] || [ -z "${DASHBOARD_TOKEN:-}" ] || [ "${DASHBOARD_TOKEN}" = "PLACEHOLDER_TOKEN" ]; then
    log "config.sh has placeholders; run bin/post_install.sh first"
    eips 2 2 "Hermes Dashboard: run post_install.sh" >/dev/null 2>&1 || true
    exit 1
fi

# Locate Python 3.
PYTHON_BIN=""
for candidate in \
    "/mnt/us/python3/bin/python3" \
    "/mnt/us/python/bin/python3" \
    "/usr/bin/python3" \
    "/usr/local/bin/python3"
do
    if [ -x "$candidate" ]; then
        PYTHON_BIN="$candidate"
        break
    fi
done
if [ -z "$PYTHON_BIN" ] && command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
fi
if [ -z "$PYTHON_BIN" ]; then
    log "python3 not found; install via mrpackage (kindle-python3)"
    eips 2 2 "Python 3 not found. Run: mrpackage install kindle-python3" >/dev/null 2>&1 || true
    exit 1
fi
log "using $PYTHON_BIN"

# Kill any previous interactive session.
if [ -s "$PIDFILE" ]; then
    old_pid="$(cat "$PIDFILE" 2>/dev/null)"
    if [ -n "$old_pid" ] && kill -0 "$old_pid" >/dev/null 2>&1; then
        kill "$old_pid" >/dev/null 2>&1 || true
    fi
    rm -f "$PIDFILE"
fi

# Keep the network available and suppress the stock screensaver while the
# dashboard owns the display.
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

# Build the client args.
args=""
args="$args --host $HOST_IP"
args="$args --port ${HOST_PORT:-9120}"
args="$args --read-token $DASHBOARD_TOKEN"
if [ -n "${CONTROL_TOKEN:-}" ]; then
    args="$args --control-token $CONTROL_TOKEN"
fi
args="$args --input-device /dev/input/event0"
args="$args --touch-device /dev/input/event1"
args="$args --image /tmp/hermes-dashboard-interactive.png"
args="$args --refresh-seconds ${REFRESH_INTERVAL:-15}"

log "starting interactive client: $PYTHON_BIN $CLIENT $args"
nohup "$PYTHON_BIN" "$CLIENT" $args >> "$LOG" 2>&1 &
pid=$!
echo "$pid" > "$PIDFILE"
log "started interactive pid=$pid"
exit 0