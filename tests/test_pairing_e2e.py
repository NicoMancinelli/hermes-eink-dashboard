"""End-to-end pairing over real sockets: UDP beacon -> wizard poll -> approve.

This is the zero-typing story proven on localhost: a real uvicorn server
exposes /pair/*, a real UDP beacon advertises it, the setup wizard discovers
the host, registers itself, gets approved through the admin API, receives its
tokens and writes them into config.sh.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn

from hermes_kindle_dashboard.api import ApiSettings, create_app
from hermes_kindle_dashboard.beacon import broadcast_once, build_beacon_payload
from hermes_kindle_dashboard.pairing import DeviceStore, PairingService
from kindle.client.setup_wizard import run_wizard

CONTROL_TOKEN = "control-token-e2e"


def _free_port(kind: int) -> int:
    with socket.socket(socket.AF_INET, kind) as sock:
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
    raise RuntimeError(f"server not ready on {port}")


def _direct_opener():
    """urllib opener that ignores proxy environment variables."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _api(opener, method: str, url: str, token: str | None = None, payload: dict | None = None):
    request = urllib.request.Request(url, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if payload is not None:
        request.add_header("Content-Type", "application/json")
        request.data = json.dumps(payload).encode()
    with opener.open(request, timeout=5) as response:
        return json.loads(response.read().decode())


def _approve_when_pending(
    opener,
    base_url: str,
    stop: threading.Event,
    result: dict,
) -> None:
    """Poll /pair/devices until a pending device appears, then approve it."""
    deadline = time.monotonic() + 15.0
    while not stop.is_set() and time.monotonic() < deadline:
        try:
            listing = _api(opener, "GET", f"{base_url}/pair/devices", token=CONTROL_TOKEN)
            pending = [d for d in listing.get("devices", []) if d["status"] == "pending"]
            if pending:
                code = pending[0]["display_code"]
                _api(opener, "POST", f"{base_url}/pair/approve", token=CONTROL_TOKEN, payload={"device": code})
                result["approved_code"] = code
                return
        except Exception:
            pass
        time.sleep(0.1)


def _broadcast_forever(payload: bytes, port: int, stop: threading.Event) -> None:
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        while not stop.is_set():
            broadcast_once(sender, payload, ("127.0.0.1",), port)
            stop.wait(0.1)
    finally:
        sender.close()


def test_full_pairing_flow_over_real_sockets(tmp_path: Path) -> None:
    tcp_port = _free_port(socket.SOCK_STREAM)
    udp_port = _free_port(socket.SOCK_DGRAM)
    base_url = f"http://127.0.0.1:{tcp_port}"

    store = DeviceStore(tmp_path / "devices.json")
    settings = ApiSettings(token="read-token-e2e", control_token=CONTROL_TOKEN, pairing=PairingService(store))
    app = create_app(settings=settings, aggregators=[])

    config = uvicorn.Config(app, host="127.0.0.1", port=tcp_port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_ready(tcp_port)

    beacon_stop = threading.Event()
    payload = build_beacon_payload(service_port=tcp_port, hostname="e2e-host")
    beacon_thread = threading.Thread(target=_broadcast_forever, args=(payload, udp_port, beacon_stop), daemon=True)
    beacon_thread.start()

    opener = _direct_opener()
    approval_result: dict = {}
    stop_approval = threading.Event()
    approver = threading.Thread(
        target=_approve_when_pending,
        args=(opener, base_url, stop_approval, approval_result),
        daemon=True,
    )
    approver.start()

    config_path = tmp_path / "config.sh"
    config_path.write_text(
        'HOST_IP="HOST_IP"\nHOST_PORT="9120"\nDASHBOARD_TOKEN="CHANGE_ME"\nCONTROL_TOKEN=""\n'
    )

    try:
        exit_code = run_wizard(
            config_path=str(config_path),
            example_path=str(tmp_path / "absent.example"),
            discovery_port=udp_port,
            listen_seconds=2.0,
            poll_interval=0.05,
            poll_timeout=15.0,
            device_name="kindle-e2e",
            sink=lambda *lines: None,
            urlopen=opener.open,
        )
    finally:
        beacon_stop.set()
        stop_approval.set()
        server.should_exit = True
        thread.join(timeout=5)

    assert approval_result.get("approved_code"), "admin API never saw a pending device"
    assert exit_code == 0

    content = config_path.read_text()
    devices = store.list_devices()
    assert len(devices) == 1 and devices[0]["status"] == "approved"
    record = store.find_by_display_code(devices[0]["display_code"])
    assert 'HOST_IP="127.0.0.1"' in content
    assert f'HOST_PORT="{tcp_port}"' in content
    assert f'DASHBOARD_TOKEN="{record.read_token}"' in content
    assert f'CONTROL_TOKEN="{record.control_token}"' in content


def test_wizard_times_out_when_never_approved(tmp_path: Path) -> None:
    tcp_port = _free_port(socket.SOCK_STREAM)
    udp_port = _free_port(socket.SOCK_DGRAM)
    base_url = f"http://127.0.0.1:{tcp_port}"

    settings = ApiSettings(token="r", control_token=CONTROL_TOKEN, pairing=PairingService(DeviceStore(tmp_path / "d.json")))
    app = create_app(settings=settings, aggregators=[])
    config = uvicorn.Config(app, host="127.0.0.1", port=tcp_port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_ready(tcp_port)

    beacon_stop = threading.Event()
    payload = build_beacon_payload(service_port=tcp_port, hostname="e2e-host")
    beacon_thread = threading.Thread(target=_broadcast_forever, args=(payload, udp_port, beacon_stop), daemon=True)
    beacon_thread.start()

    opener = _direct_opener()
    config_path = tmp_path / "config.sh"
    config_path.write_text('DASHBOARD_TOKEN="CHANGE_ME"\n')
    original = config_path.read_text()

    try:
        exit_code = run_wizard(
            config_path=str(config_path),
            example_path="",
            discovery_port=udp_port,
            listen_seconds=1.0,
            poll_interval=0.05,
            poll_timeout=1.0,
            device_name="kindle-e2e",
            sink=lambda *lines: None,
            urlopen=opener.open,
        )
    finally:
        beacon_stop.set()
        server.should_exit = True
        thread.join(timeout=5)

    # Poll window elapsed with no approval: wizard reports failure, config untouched.
    assert exit_code == 3
    assert config_path.read_text() == original
