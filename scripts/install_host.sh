#!/bin/sh
set -eu

ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
BIND_HOST="127.0.0.1"
PORT="9120"
WIDTH="1072"
HEIGHT="1448"
CONTEXT_LIMIT="262144"
REFRESH_SECONDS="15"
START_SERVICE="1"

usage() {
  cat <<'EOF'
Usage: scripts/install_host.sh [options]
  --bind ADDRESS        Bind address (default: 127.0.0.1)
  --port PORT           HTTP port (default: 9120)
  --width PIXELS        Dashboard width (default: 1072)
  --height PIXELS       Dashboard height (default: 1448)
  --context-limit N     Model context window (default: 262144)
  --refresh-seconds N   Hermes refresh interval (default: 15)
  --no-start            Install without enabling the service
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --bind) BIND_HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --width) WIDTH="$2"; shift 2 ;;
    --height) HEIGHT="$2"; shift 2 ;;
    --context-limit) CONTEXT_LIMIT="$2"; shift 2 ;;
    --refresh-seconds) REFRESH_SECONDS="$2"; shift 2 ;;
    --no-start) START_SERVICE="0"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$BIND_HOST" in ""|*[!A-Za-z0-9._:-]*) echo "Bind address contains invalid characters" >&2; exit 2 ;; esac
case "$PORT:$WIDTH:$HEIGHT:$CONTEXT_LIMIT:$REFRESH_SECONDS" in *[!0-9:]*) echo "Numeric options contain invalid characters" >&2; exit 2 ;; esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then echo "Port must be between 1 and 65535" >&2; exit 2; fi
if [ "$WIDTH" -lt 320 ] || [ "$HEIGHT" -lt 480 ]; then echo "Display must be at least 320x480" >&2; exit 2; fi
if [ "$CONTEXT_LIMIT" -lt 1 ]; then echo "Context limit must be positive" >&2; exit 2; fi
if [ "$REFRESH_SECONDS" -lt 1 ]; then echo "Refresh interval must be positive" >&2; exit 2; fi

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/hermes-eink-dashboard"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/hermes-eink-dashboard"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
TOKEN_FILE="$CONFIG_DIR/token"
CONTROL_TOKEN_FILE="$CONFIG_DIR/control_token"
ENV_FILE="$CONFIG_DIR/host.env"
VENV="$DATA_DIR/venv"

# Migrate a pre-consolidation install (hermes-kindle-dashboard -> hermes-eink-dashboard).
# The config dir holds the tokens/host.env and is the only precious state; the
# data dir is just a disposable venv that is recreated below at the new path.
OLD_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/hermes-kindle-dashboard"
OLD_DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/hermes-kindle-dashboard"
OLD_UNIT="$UNIT_DIR/hermes-kindle-dashboard.service"
if [ -d "$OLD_CONFIG_DIR" ] && [ ! -e "$CONFIG_DIR" ]; then
  echo "Migrating config: $OLD_CONFIG_DIR -> $CONFIG_DIR"
  mv "$OLD_CONFIG_DIR" "$CONFIG_DIR"
fi
if [ -f "$OLD_UNIT" ]; then
  if systemctl --user >/dev/null 2>&1; then
    systemctl --user disable --now hermes-kindle-dashboard.service >/dev/null 2>&1 || true
  fi
  rm -f "$OLD_UNIT"
fi
if [ -d "$OLD_DATA_DIR" ]; then
  echo "Note: leaving stale venv at $OLD_DATA_DIR (safe to delete)."
fi

mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$UNIT_DIR"
chmod 700 "$CONFIG_DIR"
if [ ! -s "$TOKEN_FILE" ]; then
  umask 077
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"
if [ ! -s "$CONTROL_TOKEN_FILE" ]; then
  umask 077
  python3 -c 'import secrets; print(secrets.token_hex(32))' > "$CONTROL_TOKEN_FILE"
fi
chmod 600 "$CONTROL_TOKEN_FILE"

python3 -m venv "$VENV"
PYTHONPATH='' "$VENV/bin/python" -m pip install --quiet --upgrade pip
PYTHONPATH='' "$VENV/bin/python" -m pip install --quiet --upgrade "$ROOT"

umask 077
cat > "$ENV_FILE" <<EOF
HERMES_DASHBOARD_HOST=$BIND_HOST
HERMES_DASHBOARD_PORT=$PORT
HERMES_DASHBOARD_WIDTH=$WIDTH
HERMES_DASHBOARD_HEIGHT=$HEIGHT
HERMES_DASHBOARD_BIT_DEPTH=1
HERMES_DASHBOARD_CONTEXT_LIMIT=$CONTEXT_LIMIT
HERMES_DASHBOARD_REFRESH_SECONDS=$REFRESH_SECONDS
HERMES_DASHBOARD_TOKEN_FILE=$TOKEN_FILE
HERMES_DASHBOARD_CONTROL_TOKEN_FILE=$CONTROL_TOKEN_FILE
HERMES_HOME=$HOME/.hermes
EOF
chmod 600 "$ENV_FILE"
install -m 0644 "$ROOT/systemd/hermes-eink-dashboard.service" "$UNIT_DIR/hermes-eink-dashboard.service"

# Skip systemd integration when running in a chroot/test environment without
# a user bus. Detected by trying systemctl and falling back gracefully.
if systemctl --user >/dev/null 2>&1; then
  systemctl --user daemon-reload
  if [ "$START_SERVICE" = "1" ]; then
    systemctl --user enable hermes-eink-dashboard.service >/dev/null
    systemctl --user restart hermes-eink-dashboard.service
  fi
else
  echo "(no user systemd bus available; unit installed but not enabled)"
fi

echo "Host installed."
echo "API: http://$BIND_HOST:$PORT/dashboard-data"
echo "Kindle compatibility: http://$BIND_HOST:$PORT/dashboard.png"
echo "Read token:    $TOKEN_FILE (not printed)"
echo "Control token: $CONTROL_TOKEN_FILE (not printed)"
echo "Next: build the KUAL ZIP:"
echo "  python3 scripts/build_kual_bundle.py --host <Kindle-reachable-host> --inject-tokens"
