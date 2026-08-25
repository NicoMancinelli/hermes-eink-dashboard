#!/bin/sh
# simulate_kindle.sh — Run the interactive client against a local host using
# the fbink stub, so you can develop without an e-ink device.
#
#   scripts/simulate_kindle.sh            # token+port auto/defaults
#   PORT=9200 scripts/simulate_kindle.sh  # custom port
#
# Watch frames:   ls -t /tmp/fbink-stub/frame_*.png | head
# Full FBInk log: cat /tmp/fbink-stub/fbink.log
# Stop:           Ctrl+C (both processes are cleaned up)

set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-9120}"
TOKEN="${TOKEN:-sim-token}"
STUB_DIR="${FBINK_STUB_DIR:-/tmp/fbink-stub}"

if [ -x "$ROOT/.venv/bin/python" ]; then
    PYTHON="$ROOT/.venv/bin/python"
else
    PYTHON="python3"
fi

rm -rf "$STUB_DIR"
mkdir -p "$STUB_DIR"

# Host: real API app with a synthetic Hermes panel (same as tests).
"$PYTHON" - <<EOF &
import sys
sys.path.insert(0, "$ROOT")
sys.path.insert(0, "$ROOT/tests")
import uvicorn
from hermes_kindle_dashboard.api import ApiSettings, create_app
from hermes_kindle_dashboard.aggregators.hermes import snapshot_to_panel
from test_render import sample_snapshot


class StaticPanelAggregator:
    name = "hermes"
    interval_seconds = 60.0

    def __init__(self):
        self._panel = snapshot_to_panel(sample_snapshot())

    async def collect(self):
        return self._panel


app = create_app(
    settings=ApiSettings(token="$TOKEN", control_token=""),
    aggregators=[StaticPanelAggregator()],
)
uvicorn.run(app, host="127.0.0.1", port=$PORT, log_level="warning")
EOF
HOST_PID=$!

cleanup() {
    kill "$HOST_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

sleep 1

# Client: the exact binary that ships to the Kindle (via the stub fbink).
FBINK_STUB_DIR="$STUB_DIR" PYTHONPATH="$ROOT" exec "$PYTHON" \
    -m kindle.client.interactive \
    --host 127.0.0.1 --port "$PORT" --read-token "$TOKEN" \
    --image /tmp/hermes-sim-dash.png \
    --refresh-seconds "${REFRESH_SECONDS:-5}" \
    --fbink-path "$ROOT/scripts/fbink_stub"
