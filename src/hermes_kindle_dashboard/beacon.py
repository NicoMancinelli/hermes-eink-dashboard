"""LAN discovery beacon for the Hermes E-Ink Dashboard.

The host periodically broadcasts a small secret-less JSON datagram so that
devices on the same LAN (the Kindle setup wizard) can find the dashboard
service without anyone typing an IP address:

    {"service": "hermes-eink-dashboard", "schema": 1,
     "port": 9120, "hostname": "box", "instance": "<8 hex>"}

The beacon carries no tokens or private data — only *where* the service
lives. Broadcasts do not traverse routers; clients outside the segment can
still configure the host address manually. Disable with ``--no-discovery``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import socket
from dataclasses import dataclass

SERVICE_NAME = "hermes-eink-dashboard"
SCHEMA_VERSION = 1
DEFAULT_DISCOVERY_PORT = 9121
DEFAULT_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class BeaconConfig:
    port: int = DEFAULT_DISCOVERY_PORT
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    # API port advertised inside the payload (what clients actually connect to).
    service_port: int = 9120


def instance_id_for(hostname: str, service_port: int) -> str:
    digest = hashlib.sha256(f"{hostname}:{service_port}".encode("utf-8")).hexdigest()
    return digest[:8]


def build_beacon_payload(service_port: int, hostname: str) -> bytes:
    payload = {
        "service": SERVICE_NAME,
        "schema": SCHEMA_VERSION,
        "port": int(service_port),
        "hostname": hostname,
        "instance": instance_id_for(hostname, service_port),
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def parse_beacon_payload(data: bytes) -> dict | None:
    """Parse and validate a received beacon; return None for anything else."""
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("service") != SERVICE_NAME:
        return None
    if payload.get("schema") != SCHEMA_VERSION:
        return None
    port = payload.get("port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    if not isinstance(payload.get("hostname"), str) or not isinstance(payload.get("instance"), str):
        return None
    return payload


def _open_broadcast_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    return sock


def broadcast_once(sock: socket.socket, payload: bytes, destinations: tuple[str, ...], port: int) -> None:
    """Send one datagram to every destination, ignoring per-target failures."""
    for address in destinations:
        try:
            sock.sendto(payload, (address, port))
        except OSError:
            # A single unreachable broadcast target must never kill the beacon.
            continue


async def run_beacon(
    config: BeaconConfig,
    stop_event: asyncio.Event,
    *,
    destinations: tuple[str, ...] = ("255.255.255.255",),
    hostname: str | None = None,
) -> None:
    """Broadcast the beacon every ``interval_seconds`` until ``stop_event``."""
    resolved_hostname = hostname or socket.gethostname()
    payload = build_beacon_payload(config.service_port, resolved_hostname)
    loop = asyncio.get_running_loop()
    sock = await loop.run_in_executor(None, _open_broadcast_socket)
    try:
        while True:
            broadcast_once(sock, payload, destinations, config.port)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=config.interval_seconds)
                break  # stop_event set
            except asyncio.TimeoutError:
                continue
    finally:
        sock.close()
