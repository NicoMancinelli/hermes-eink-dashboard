"""Device-simulation tests: the REAL interactive client binary, end to end.

No full Kindle system emulator exists that we could rely on, so this harness
simulates the two device interfaces the client touches:

- the framebuffer, via scripts/fbink_stub placed on PATH as ``fbink``
  (capturing every paint as a viewable PNG plus an invocation log), and
- input, which stays inside the client as mock events (evdev parsing is
  covered separately in unit tests).

What runs for real: the ``python -m kindle.client.interactive`` process, its
arg parsing, HTTP stack, retry/offline state machine and display calls,
against a live uvicorn host over real sockets.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn

from hermes_kindle_dashboard.aggregators.hermes import snapshot_to_panel
from hermes_kindle_dashboard.api import ApiSettings, create_app
from test_render import sample_snapshot

ROOT = Path(__file__).resolve().parents[1]


class StaticPanelAggregator:
    """Serves a fixed, realistic Hermes panel so legacy routes render."""

    name = "hermes"
    interval_seconds = 60.0

    def __init__(self) -> None:
        self._panel = snapshot_to_panel(sample_snapshot())

    async def collect(self):
        return self._panel


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_ready(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"host not ready on {port}")


def _start_host(port: int):
    settings = ApiSettings(token="read-token-sim", control_token="")
    app = create_app(settings=settings, aggregators=[StaticPanelAggregator()])
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_ready(port)
    return server, thread


def _launch_client(port: int, tmp_path: Path, stub_bin: Path, stub_dir: Path) -> subprocess.Popen:
    command = [
        sys.executable,
        "-m",
        "kindle.client.interactive",
        "--host", "127.0.0.1",
        "--port", str(port),
        "--read-token", "read-token-sim",
        "--image", str(tmp_path / "dash.png"),
        "--refresh-seconds", "0.3",
        "--retry-attempts", "2",
        "--retry-backoff", "0.05",
        "--full-refresh-every", "3",
        "--fbink-path", str(stub_bin / "fbink"),
    ]
    env = dict(os.environ)
    env["FBINK_STUB_DIR"] = str(stub_dir)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.Popen(
        command,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for(predicate, timeout: float = 8.0, message: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {message}")


def test_real_client_paints_survives_outage_and_exits_cleanly(tmp_path: Path) -> None:
    port = _free_port()
    server, host_thread = _start_host(port)

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    os.symlink(ROOT / "scripts" / "fbink_stub", stub_bin / "fbink")
    stub_dir = tmp_path / "frames"

    image_path = tmp_path / "dash.png"
    process = _launch_client(port, tmp_path, stub_bin, stub_dir)
    try:
        # 1. Client paints real dashboard frames through the fbink contract.
        _wait_for(
            lambda: len(list(stub_dir.glob("frame_*.png"))) >= 2 and image_path.exists(),
            message="client to paint at least two frames",
        )
        first_frames = sorted(stub_dir.glob("frame_*.png"))
        assert all(frame.stat().st_size > 500 for frame in first_frames)

        log_text = (stub_dir / "fbink.log").read_text()
        assert "paint flash=True" in log_text  # first paint forces full flash
        assert "GC16" in log_text

        # 2. Host dies -> client must stay alive and draw the offline screen.
        server.should_exit = True
        host_thread.join(timeout=5)
        _wait_for(lambda: "text" in (stub_dir / "fbink.log").read_text(), message="offline text screen")
        assert process.poll() is None, "client crashed during outage"

        # Offline screen must be drawn once, not repeatedly.
        time.sleep(0.4)
        offline_lines = [
            line for line in (stub_dir / "fbink.log").read_text().splitlines() if "text" in line
        ]
        assert len(offline_lines) == 1
    finally:
        # 3. Clean shutdown on SIGTERM.
        process.send_signal(signal.SIGTERM)
        try:
            code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            raise AssertionError(
                f"client did not exit on SIGTERM; stdout={process.stdout.read()!r} stderr={process.stderr.read()!r}"
            )
    assert code == 0


def test_client_reports_fatal_auth_error(tmp_path: Path) -> None:
    port = _free_port()
    settings = ApiSettings(token="correct-token", control_token="")
    app = create_app(settings=settings, aggregators=[])
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_ready(port)

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    os.symlink(ROOT / "scripts" / "fbink_stub", stub_bin / "fbink")
    command = [
        sys.executable,
        "-m",
        "kindle.client.interactive",
        "--host", "127.0.0.1",
        "--port", str(port),
        "--read-token", "WRONG",
        "--image", str(tmp_path / "x.png"),
        "--no-fbink",
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    try:
        process = subprocess.Popen(command, cwd=str(ROOT), env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        code = process.wait(timeout=15)
        assert code == 1  # fatal config error exits fast instead of retrying forever
    finally:
        server.should_exit = True
        thread.join(timeout=5)
