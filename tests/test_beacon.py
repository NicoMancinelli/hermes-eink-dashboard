"""Tests for the LAN discovery beacon."""
from __future__ import annotations

import asyncio
import json
import socket

from hermes_kindle_dashboard.beacon import (
    BeaconConfig,
    DEFAULT_DISCOVERY_PORT,
    broadcast_once,
    build_beacon_payload,
    instance_id_for,
    parse_beacon_payload,
    run_beacon,
)


def _free_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class TestPayload:
    def test_payload_fields_and_stability(self) -> None:
        payload = json.loads(build_beacon_payload(9120, "testhost"))
        assert payload["service"] == "hermes-eink-dashboard"
        assert payload["schema"] == 1
        assert payload["port"] == 9120
        assert payload["hostname"] == "testhost"
        assert payload["instance"] == instance_id_for("testhost", 9120)
        # Stable for identical inputs, distinct otherwise.
        assert instance_id_for("a", 1) == instance_id_for("a", 1)
        assert instance_id_for("a", 1) != instance_id_for("a", 2)

    def test_parse_rejects_garbage(self) -> None:
        assert parse_beacon_payload(b"") is None
        assert parse_beacon_payload(b"{not json") is None
        assert parse_beacon_payload(b'{"service": "other"}') is None
        assert parse_beacon_payload(json.dumps({"service": "hermes-eink-dashboard", "schema": 99, "port": 1}).encode()) is None
        bad_port = {"service": "hermes-eink-dashboard", "schema": 1, "port": 99999, "hostname": "h", "instance": "i"}
        assert parse_beacon_payload(json.dumps(bad_port).encode()) is None
        missing = {"service": "hermes-eink-dashboard", "schema": 1, "port": 9120}
        assert parse_beacon_payload(json.dumps(missing).encode()) is None

    def test_parse_accepts_valid(self) -> None:
        raw = build_beacon_payload(9120, "box")
        parsed = parse_beacon_payload(raw)
        assert parsed is not None
        assert parsed["port"] == 9120
        assert parsed["hostname"] == "box"


class TestBroadcast:
    def test_broadcast_once_delivers_datagram(self) -> None:
        port = _free_udp_port()
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", port))
        receiver.settimeout(3)
        try:
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                payload = build_beacon_payload(9120, "box")
                broadcast_once(sender, payload, ("127.0.0.1",), port)
            finally:
                sender.close()
            data, _addr = receiver.recvfrom(2048)
            assert parse_beacon_payload(data)["port"] == 9120
        finally:
            receiver.close()

    def test_broadcast_survives_bad_destination(self) -> None:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            # Must not raise even though this target cannot receive.
            broadcast_once(sender, b"x", ("127.0.0.1",), 1)
        finally:
            sender.close()


class TestRunBeacon:
    def test_run_beacon_sends_until_stopped(self) -> None:
        port = _free_udp_port()
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", port))
        receiver.settimeout(5)

        async def scenario() -> bytes:
            stop_event = asyncio.Event()
            config = BeaconConfig(port=port, interval_seconds=10, service_port=9120)
            task = asyncio.create_task(run_beacon(config, stop_event, destinations=("127.0.0.1",), hostname="box"))
            await asyncio.sleep(0.2)
            data, _addr = receiver.recvfrom(2048)
            stop_event.set()
            await asyncio.wait_for(task, timeout=5)
            return data

        try:
            data = asyncio.run(scenario())
            assert parse_beacon_payload(data)["instance"] == instance_id_for("box", 9120)
        finally:
            receiver.close()


def test_default_discovery_port_constant() -> None:
    assert 1024 <= DEFAULT_DISCOVERY_PORT <= 65535
