#!/bin/sh
set -eu

ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd)
BIND_HOST="127.0.0.1"
PORT="9120"
WIDTH="1072"
HEIGHT="1448"
CONTEXT_LIMIT="262144"
START_SERVICE="1"

usage() {
  cat <<'EOF'
Usage: scripts/install_host.sh [options]
  --bind ADDRESS        Bind address (default: 127.0.0.1)
  --port PORT           HTTP port (default: 9120)
  --width PIXELS        Dashboard width (default: 1072)
  --height PIXELS       Dashboard height (default: 1448)
  --context-limit N     Model context window (default: 262144)
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
    --no-start) START_SERVICE="0"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$BIND_HOST" in ""|*[!A-Za-z0-9._:-]*) echo "Bind address contains invalid characters" >&2; exit 2 ;; esac
case "$PORT:$WIDTH:$HEIGHT:$CONTEXT_LIMIT" in *[!0-9:]*) echo "Numeric options contain invalid characters" >&2; exit 2 ;; esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then echo "Port must be between 1 and 65535" >&2; exit 2; fi
if [ "$WIDTH" -lt 320 ] || [ "$HEIGHT" -lt 480 ]; then echo "Display must be at least 320x480" >&2; exit 2; fi
if [ "$CONTEXT_LIMIT" -lt 1 ]; then echo "Context limit must be positive" >&2; exit 2; fi

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/hermes-kindle-dashboard"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/hermes-kindle-dashboard"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
TOKEN_FILE="$CONFIG_DIR/token"
ENV_FILE="$CONFIG_DIR/host.env"
VENV="$DATA_DIR/venv"

mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$UNIT_DIR"
chmod 700 "$CONFIG_DIR"
if [ ! -s "$TOKEN_FILE" ]; then
  umask 077
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"

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
HERMES_DASHBOARD_CACHE_SECONDS=10
HERMES_DASHBOARD_TOKEN_FILE=$TOKEN_FILE
HERMES_HOME=$HOME/.hermes
EOF
chmod 600 "$ENV_FILE"
install -m 0644 "$ROOT/systemd/hermes-kindle-dashboard.service" "$UNIT_DIR/hermes-kindle-dashboard.service"

systemctl --user daemon-reload
if [ "$START_SERVICE" = "1" ]; then
  systemctl --user enable hermes-kindle-dashboard.service >/dev/null
  systemctl --user restart hermes-kindle-dashboard.service
fi

echo "Host installed."
echo "Endpoint: http://$BIND_HOST:$PORT/dashboard.png"
echo "Token: $TOKEN_FILE (not printed)"
echo "Next: build the KUAL ZIP with scripts/build_kual_bundle.py --host <Kindle-reachable-host>"
