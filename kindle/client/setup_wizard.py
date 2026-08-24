#!/usr/bin/env python3
"""Hermes E-Ink Dashboard first-run setup wizard for Kindle.

Runs ON the Kindle (stdlib Python 3 only) and performs the complete
zero-typing onboarding:

1. Listens for the host's UDP discovery beacon to find the dashboard URL.
2. Generates a device identity locally and shows its short display code
   (e.g. ``AB12-CD34``) on the Kindle screen.
3. Polls the host until the admin approves it there:
   ``hermes-dashboard-pair approve AB12-CD34``
4. Writes the received tokens into the KUAL extension's ``config.sh``
   (mode 0600). Done — no tokens ever typed by hand.

Progress is shown via ``eips`` so no extra dependencies are required.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import sys
import time
import urllib.error
import urllib.request

DISCOVERY_PORT_DEFAULT = 9121
POLL_INTERVAL_SECONDS = 3.0
POLL_TIMEOUT_SECONDS = 300.0
LISTEN_SECONDS_DEFAULT = 8.0

CONFIG_PATH = "/mnt/us/extensions/hermes_dashboard/config.sh"
CONFIG_EXAMPLE_PATH = "/mnt/us/extensions/hermes_dashboard/config.sh.example"


# --------------------------------------------------------------------- util
def log(message: str) -> None:
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open("/mnt/us/documents/hermes-dashboard.log", "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} wizard: {message}\n")
    except OSError:
        pass


def show(line1: str, line2: str = "", line3: str = "") -> None:
    """Best-effort on-screen status via eips."""
    try:
        os.system(f'eips 2 4 "{line1}" >/dev/null 2>&1')
        if line2:
            os.system(f'eips 2 6 "{line2}" >/dev/null 2>&1')
        if line3:
            os.system(f'eips 2 8 "{line3}" >/dev/null 2>&1')
    except Exception:
        pass


def generate_device_credentials() -> tuple[str, str, str]:
    """Return (device_id, device_secret, display_code)."""
    device_id = secrets.token_hex(8)          # 16 hex chars
    device_secret = secrets.token_hex(32)     # 64 hex chars
    canonical = device_id[:8].upper()
    display_code = f"{canonical[:4]}-{canonical[4:]}"
    return device_id, device_secret, display_code


# ---------------------------------------------------------------- discovery
class BeaconCollector:
    """Collects and deduplicates discovery beacons from a bound socket."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._candidates: dict[str, dict] = {}

    def collect(self, listen_seconds: float) -> list[dict]:
        deadline = time.monotonic() + max(0.0, listen_seconds)
        self._sock.settimeout(0.5)
        while time.monotonic() < deadline:
            try:
                data, address = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            payload = parse_beacon(data)
            if payload is None:
                continue
            payload["address"] = address[0]
            self._candidates.setdefault(payload["instance"], payload)
        return sorted(self._candidates.values(), key=lambda item: item.get("hostname", ""))


def make_discovery_socket(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    return sock


def parse_beacon(data: bytes) -> dict | None:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("service") != "hermes-eink-dashboard":
        return None
    if payload.get("schema") != 1:
        return None
    port = payload.get("port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    if not isinstance(payload.get("hostname"), str) or not isinstance(payload.get("instance"), str):
        return None
    return {"port": port, "hostname": payload["hostname"], "instance": payload["instance"]}


# ------------------------------------------------------------------ pairing
class PairingClient:
    """Polls POST /pair/poll until approved. Stdlib HTTP only."""

    def __init__(self, timeout: float = 10.0, urlopen=None) -> None:
        self._timeout = timeout
        self._urlopen = urlopen or urllib.request.urlopen

    def poll(self, base_url: str, device_id: str, device_secret: str, name: str) -> str:
        """Return one of: pending | approved | forbidden | rate_limited | error."""
        body = json.dumps({
            "device_id": device_id,
            "device_secret": device_secret,
            "name": name,
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/pair/poll",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return {400: "error", 403: "forbidden", 429: "rate_limited"}.get(exc.code, "error")
        except (urllib.error.URLError, OSError, ValueError):
            return "error"
        if not isinstance(payload, dict):
            return "error"
        status = payload.get("status")
        if status == "approved":
            self.tokens = {
                "read_token": str(payload.get("read_token", "")),
                "control_token": str(payload.get("control_token", "")),
                "device_name": str(payload.get("device_name", name)),
            }
            return "approved"
        return status if status in ("pending",) else "error"

    tokens: dict = None  # type: ignore[assignment]


# ------------------------------------------------------------------- config
def render_config(existing: str, host: str, port: int, read_token: str, control_token: str) -> str:
    """Fill connection values into a config.sh, preserving other settings."""
    def replace(pattern: str, replacement: str, text: str) -> str:
        compiled = re.compile(pattern, re.MULTILINE)
        if compiled.search(text):
            return compiled.sub(replacement.replace("\\", "\\\\"), text, count=1)
        return text

    text = existing
    text = replace(r'^HOST_IP=.*$', f'HOST_IP="{host}"', text)
    text = replace(r'^HOST_PORT=.*$', f'HOST_PORT="{port}"', text)
    text = replace(r'^DASHBOARD_TOKEN=.*$', f'DASHBOARD_TOKEN="{read_token}"', text)
    text = replace(r'^CONTROL_TOKEN=.*$', f'CONTROL_TOKEN="{control_token}"', text)
    text = replace(r'^DASHBOARD_URL=.*$',
                   f'DASHBOARD_URL="http://${{HOST_IP}}:${{HOST_PORT}}/dashboard.png?token=${{DASHBOARD_TOKEN}}"',
                   text)
    return text


def write_config(path: str, example_path: str, host: str, port: int, read_token: str, control_token: str) -> bool:
    try:
        if os.path.exists(path):
            existing = open(path, encoding="utf-8").read()
        elif os.path.exists(example_path):
            existing = open(example_path, encoding="utf-8").read()
        else:
            log("no config.sh or config.sh.example found")
            return False
        updated = render_config(existing, host, port, read_token, control_token)
        temporary = path + ".part"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        return True
    except OSError as exc:
        log(f"config write failed: {exc}")
        return False


def token_ok(token: str) -> bool:
    return bool(token) and len(token) >= 16 and re.fullmatch(r"[A-Za-z0-9._~-]+", token) is not None


# --------------------------------------------------------------------- main
def run_wizard(
    config_path: str = CONFIG_PATH,
    example_path: str = CONFIG_EXAMPLE_PATH,
    discovery_port: int = DISCOVERY_PORT_DEFAULT,
    listen_seconds: float = LISTEN_SECONDS_DEFAULT,
    poll_timeout: float = POLL_TIMEOUT_SECONDS,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    device_name: str = "",
    sock=None,
    urlopen=None,
    sink=None,
) -> int:
    emit = sink or (lambda *lines: [show(*lines[:3]) or log(" | ".join(lines))])
    emit("Hermes setup:", "searching for host...")
    try:
        listener = sock or make_discovery_socket(discovery_port)
    except OSError as exc:
        log(f"discovery bind failed: {exc}")
        emit("No discovery socket.", "Run post_install.sh", "and set host manually.")
        return 1
    try:
        candidates = BeaconCollector(listener).collect(listen_seconds)
    finally:
        if sock is None:
            listener.close()

    if not candidates:
        emit("Host not found.", "Start the host service,", "or run post_install.sh.")
        log("discovery found no hosts")
        return 2

    chosen = candidates[0]
    base_url = f"http://{chosen['address']}:{chosen['port']}"
    log(f"discovered host {chosen['hostname']} at {base_url}")
    emit(f"Found host: {chosen['hostname'][:24]}", "generating device id...")

    device_id, device_secret, display_code = generate_device_credentials()
    client = PairingClient(urlopen=urlopen)
    safe_name = device_name or f"kindle-{hashlib.sha256(device_id.encode()).hexdigest()[:4]}"

    deadline = time.monotonic() + poll_timeout
    status = "pending"
    while status == "pending" and time.monotonic() < deadline:
        status = client.poll(base_url, device_id, device_secret, safe_name)
        if status == "pending":
            emit(f"Code: {display_code}", "On host run:", f"pair approve {display_code}")
            time.sleep(max(0.0, poll_interval))

    if status != "approved":
        messages = {
            "forbidden": "Pairing rejected.",
            "rate_limited": "Too many attempts.",
            "error": "Cannot reach host.",
        }
        emit(messages.get(status, "Pairing failed."), f"Code: {display_code}", "See log for details.")
        log(f"pairing ended with status={status}")
        return 3

    tokens = client.tokens or {}
    read_token = tokens.get("read_token", "")
    control_token = tokens.get("control_token", "")
    if not token_ok(read_token):
        emit("Pairing returned", "invalid tokens.", "See log for details.")
        return 4
    if not write_config(config_path, example_path, chosen["address"], chosen["port"], read_token, control_token):
        emit("Could not write", "config.sh.", "Check log.")
        return 5

    emit("Paired successfully!", "Open KUAL >", "Hermes Dashboard > Start")
    log(f"paired as {safe_name} with {base_url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    config_path = CONFIG_PATH
    discovery_port = DISCOVERY_PORT_DEFAULT
    listen_seconds = LISTEN_SECONDS_DEFAULT
    index = 0
    positional: list[str] = []
    while index < len(argv):
        argument = argv[index]
        if argument == "--config" and index + 1 < len(argv):
            config_path = argv[index + 1]
            index += 2
        elif argument == "--discovery-port" and index + 1 < len(argv):
            discovery_port = int(argv[index + 1])
            index += 2
        elif argument == "--listen-seconds" and index + 1 < len(argv):
            listen_seconds = float(argv[index + 1])
            index += 2
        else:
            positional.append(argument)
            index += 1
    return run_wizard(
        config_path=config_path,
        discovery_port=discovery_port,
        listen_seconds=listen_seconds,
        device_name=positional[0] if positional else "",
    )


if __name__ == "__main__":
    raise SystemExit(main())
