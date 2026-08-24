#!/bin/sh
# start_wizard.sh — First-run pairing wizard for the Hermes Dashboard.
#
# Discovers the dashboard host on the LAN and pairs this Kindle with it so
# config.sh gets real tokens without any manual copying. Requires Python 3
# (same runtime as the interactive client). Progress is drawn via eips.

set -eu

ROOT="/mnt/us/extensions/hermes_dashboard"
CONFIG="$ROOT/config.sh"
WIZARD="$ROOT/bin/setup_wizard.py"
LOG="/mnt/us/documents/hermes-dashboard.log"

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"
}

if [ ! -f "$WIZARD" ]; then
    log "wizard missing: $WIZARD"
    exit 1
fi

lipc-set-prop com.lab126.cmd wirelessEnable 1 >/dev/null 2>&1 || true

# Locate Python 3 (same candidates as start_interactive.sh).
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
# Auto-bootstrap Python 3 via mrpackage when missing and allowed.
if [ -z "$PYTHON_BIN" ] && [ "${AUTO_INSTALL_PYTHON:-1}" = "1" ] && command -v mrpackage >/dev/null 2>&1; then
    log "python3 not found; attempting: mrpackage install kindle-python3"
    eips 2 4 "Installing Python 3 (mrpackage)..." >/dev/null 2>&1 || true
    if mrpackage install kindle-python3 >> "$LOG" 2>&1; then
        if [ -x "/mnt/us/python3/bin/python3" ]; then
            PYTHON_BIN="/mnt/us/python3/bin/python3"
        elif command -v python3 >/dev/null 2>&1; then
            PYTHON_BIN="$(command -v python3)"
        fi
        log "mrpackage install finished; python=$PYTHON_BIN"
    else
        log "mrpackage install failed"
    fi
fi

if [ -z "$PYTHON_BIN" ]; then
    log "python3 not found; install via mrpackage (kindle-python3)"
    eips 2 4 "Python 3 not found." >/dev/null 2>&1 || true
    eips 2 6 "Run: mrpackage install kindle-python3" >/dev/null 2>&1 || true
    exit 1
fi

# If a previous bundle shipped an example only, seed config.sh from it so the
# wizard has a file to fill in.
if [ ! -f "$CONFIG" ] && [ -f "$ROOT/config.sh.example" ]; then
    cp "$ROOT/config.sh.example" "$CONFIG"
fi

log "starting setup wizard with $PYTHON_BIN"
"$PYTHON_BIN" "$WIZARD" >> "$LOG" 2>&1
status=$?
log "setup wizard exited status=$status"
exit "$status"
