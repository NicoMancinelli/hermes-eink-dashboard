#!/bin/sh
# post_install.sh — Configure the Hermes Dashboard KUAL extension after copy.
#
# Runs on the Kindle. Replaces the placeholder values in config.sh with the
# real host address and tokens. The user provides these by either:
#   1. Running this script interactively (Kindle terminal via KUAL helper).
#   2. Editing /mnt/us/extensions/hermes_dashboard/config.sh manually.
#   3. Calling this script non-interactively with --host, --read-token, and
#      (optionally) --control-token arguments.
#
# Idempotent: re-running with the same values is safe.

set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$ROOT/config.sh"

if [ ! -f "$CONFIG" ]; then
    printf 'post_install: %s not found\n' "$CONFIG" >&2
    exit 1
fi

usage() {
    cat >&2 <<'EOF'
Usage: post_install.sh [--host HOST] [--port PORT] [--read-token TOKEN] [--control-token TOKEN]

  --host            LAN/tailnet address of the host running hermes-kindle-dashboard
  --port            HTTP port on the host (default 9120)
  --read-token      Bearer token for /dashboard.png and /dashboard.json
  --control-token   Bearer token for /control and /control/events (optional)
  --help            show this help

Without arguments, the script prompts interactively.
EOF
}

HOST=""
PORT="9120"
READ_TOKEN=""
CONTROL_TOKEN=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --host) HOST="${2:-}"; shift 2 ;;
        --port) PORT="${2:-}"; shift 2 ;;
        --read-token) READ_TOKEN="${2:-}"; shift 2 ;;
        --control-token) CONTROL_TOKEN="${2:-}"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) printf 'post_install: unknown argument %s\n' "$1" >&2; usage; exit 2 ;;
    esac
done

# Fall back to interactive prompt if any value is missing.
if [ -z "$HOST" ]; then
    printf 'Host (e.g. 192.168.1.50 or host.lan): '
    read -r HOST
fi
if [ -z "$READ_TOKEN" ]; then
    printf 'Read token: '
    read -r READ_TOKEN
fi
if [ -z "$CONTROL_TOKEN" ]; then
    printf 'Control token (optional, Enter to skip): '
    read -r CONTROL_TOKEN || CONTROL_TOKEN=""
fi

# Validate (BusyBox sed supports -i on recent versions; we use a tempfile
# so this works on every Kindle BusyBox).
TMP="$(mktemp)"
sed \
    -e "s|^HOST_IP=.*|HOST_IP=\"$HOST\"|" \
    -e "s|^HOST_PORT=.*|HOST_PORT=\"$PORT\"|" \
    -e "s|^DASHBOARD_TOKEN=.*|DASHBOARD_TOKEN=\"$READ_TOKEN\"|" \
    -e "s|^CONTROL_TOKEN=.*|CONTROL_TOKEN=\"$CONTROL_TOKEN\"|" \
    "$CONFIG" > "$TMP"
mv "$TMP" "$CONFIG"
chmod 600 "$CONFIG"

printf 'post_install: config.sh updated.\n'
printf 'Verify by running bin/start.sh or via KUAL.\n'