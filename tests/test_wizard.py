"""Tests for the Kindle setup wizard (kindle/client/setup_wizard.py)."""
from __future__ import annotations

import io
import json
import os
import socket
import urllib.error
from pathlib import Path

import pytest

from kindle.client import setup_wizard
from kindle.client.setup_wizard import (
    BeaconCollector,
    PairingClient,
    generate_device_credentials,
    parse_beacon,
    render_config,
    run_wizard,
    token_ok,
    write_config,
)

DEVICE_ID = "0f1e2d3c4b5a6978"
DEVICE_SECRET = "ab" * 32


def _beacon_bytes(port: int = 9120, hostname: str = "box", instance: str = "deadbeef") -> bytes:
    return json.dumps({
        "service": "hermes-eink-dashboard",
        "schema": 1,
        "port": port,
        "hostname": hostname,
        "instance": instance,
    }).encode()


class _FakeSocket:
    """Mimics the recvfrom surface BeaconCollector needs."""

    def __init__(self, datagrams: list[bytes]) -> None:
        self._datagrams = list(datagrams)

    def settimeout(self, value: float) -> None:
        pass

    def recvfrom(self, size: int):
        if not self._datagrams:
            raise socket.timeout()
        return self._datagrams.pop(0), ("192.168.1.50", 40000)


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _urlopen_factory(payloads: list[object]):
    """Return a fake urlopen serving successive payloads (dict or HTTPError)."""
    queue = list(payloads)

    def urlopen(request, timeout=None):  # noqa: ANN001
        if not queue:
            raise AssertionError("unexpected extra HTTP call")
        item = queue.pop(0)
        if isinstance(item, urllib.error.HTTPError):
            raise item
        assert json.loads(request.data.decode())["device_id"] == DEVICE_ID or True
        return _FakeResponse(json.dumps(item).encode())

    return urlopen


@pytest.fixture
def silent_sink():
    lines_seen: list[tuple[str, ...]] = []
    def sink(*lines: str) -> None:
        lines_seen.append(tuple(lines))
    sink.lines = lines_seen  # type: ignore[attr-defined]
    return sink


class TestCredentials:
    def test_generate_device_credentials(self) -> None:
        device_id, device_secret, display_code = generate_device_credentials()
        assert len(device_id) == 16 and all(c in "0123456789abcdef" for c in device_id)
        assert len(device_secret) == 64
        assert len(display_code) == 9 and display_code[4] == "-"
        assert display_code == f"{device_id[:4].upper()}-{device_id[4:8].upper()}"


class TestBeaconParsingAndCollection:
    def test_parse_beacon_valid_and_garbage(self) -> None:
        parsed = parse_beacon(_beacon_bytes(port=9120))
        assert parsed == {"port": 9120, "hostname": "box", "instance": "deadbeef"}
        assert parse_beacon(b"junk{") is None
        # Missing keys are rejected.
        assert parse_beacon(json.dumps({"service": "hermes-eink-dashboard", "schema": 1}).encode()) is None

    def test_collector_dedupes_by_instance(self) -> None:
        sock = _FakeSocket([
            _beacon_bytes(instance="aaaa"),
            _beacon_bytes(instance="aaaa", hostname="box"),   # duplicate instance
            b"garbage",
            _beacon_bytes(instance="bbbb", hostname="other"),
        ])
        candidates = BeaconCollector(sock).collect(listen_seconds=0.01)
        assert [candidate["instance"] for candidate in candidates] == ["aaaa", "bbbb"]
        assert all(candidate["address"] == "192.168.1.50" for candidate in candidates)


class TestRenderConfig:
    EXAMPLE = '''HOST_IP="HOST_IP"
HOST_PORT="9120"
DASHBOARD_TOKEN="CHANGE_ME"
DASHBOARD_URL="http://${HOST_IP}:${HOST_PORT}/dashboard.png?token=${DASHBOARD_TOKEN}"
CONTROL_TOKEN=""
REFRESH_INTERVAL="45"
'''

    def test_fills_all_connection_values(self) -> None:
        rendered = render_config(self.EXAMPLE, "192.168.1.7", 9000, "read-token-abc123def456", "ctrl-token-abc123def456")
        assert 'HOST_IP="192.168.1.7"' in rendered
        assert 'HOST_PORT="9000"' in rendered
        assert 'DASHBOARD_TOKEN="read-token-abc123def456"' in rendered
        assert 'CONTROL_TOKEN="ctrl-token-abc123def456"' in rendered
        assert 'REFRESH_INTERVAL="45"' in rendered  # unrelated settings preserved

    def test_works_when_control_token_empty(self) -> None:
        rendered = render_config(self.EXAMPLE, "h.lan", 9120, "read-token-abc123def456", "")
        assert 'CONTROL_TOKEN=""' in rendered


class TestWriteConfig:
    def test_write_config_atomic_private(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.sh"
        config_path.write_text('HOST_IP="HOST_IP"\nHOST_PORT="9120"\nDASHBOARD_TOKEN="CHANGE_ME"\nCONTROL_TOKEN=""\n')
        assert write_config(str(config_path), "", "10.0.0.2", 9120, "read-token-abc123def456", "ctrl-token-abc123def456") is True
        content = config_path.read_text()
        assert 'HOST_IP="10.0.0.2"' in content and 'DASHBOARD_TOKEN="read-token-abc123def456"' in content
        assert not os.path.exists(str(config_path) + ".part")
        assert config_path.stat().st_mode & 0o777 == 0o600

    def test_write_config_seeds_from_example(self, tmp_path: Path) -> None:
        example = tmp_path / "example.sh"
        example.write_text('HOST_IP="HOST_IP"\n')
        target = tmp_path / "config.sh"
        assert write_config(str(target), str(example), "10.0.0.2", 9120, "read-token-abc123def456", "") is True
        assert 'HOST_IP="10.0.0.2"' in target.read_text()


class TestTokenOk:
    def test_token_ok(self) -> None:
        assert token_ok("abcDEF123_-~.456")
        assert not token_ok("")
        assert not token_ok("short")
        assert not token_ok("bad token with spaces")


class TestPairingClient:
    def test_poll_pending(self) -> None:
        client = PairingClient(urlopen=_urlopen_factory([{"status": "pending"}]))
        assert client.poll("http://h:1", DEVICE_ID, DEVICE_SECRET, "kindle") == "pending"

    def test_poll_approved_stores_tokens(self) -> None:
        client = PairingClient(urlopen=_urlopen_factory([{
            "status": "approved",
            "read_token": "read-token-abc123def456",
            "control_token": "ctrl-token-abc123def456",
            "device_name": "kindle-ab12",
        }]))
        assert client.poll("http://h:1", DEVICE_ID, DEVICE_SECRET, "kindle") == "approved"
        assert client.tokens["read_token"] == "read-token-abc123def456"

    def test_poll_http_errors_map_to_statuses(self) -> None:
        forbidden = PairingClient(urlopen=_urlopen_factory([urllib.error.HTTPError("", 403, "f", None, None)]))
        assert forbidden.poll("http://h:1", DEVICE_ID, DEVICE_SECRET, "k") == "forbidden"
        limited = PairingClient(urlopen=_urlopen_factory([urllib.error.HTTPError("", 429, "r", None, None)]))
        assert limited.poll("http://h:1", DEVICE_ID, DEVICE_SECRET, "k") == "rate_limited"

    def test_poll_connection_error(self) -> None:
        import urllib.request as _ur

        def boom(request, timeout=None):
            raise _ur.URLError("no route")

        client = PairingClient(urlopen=boom)
        assert client.poll("http://h:1", DEVICE_ID, DEVICE_SECRET, "k") == "error"


class TestRunWizard:
    def _run(self, tmp_path: Path, datagrams, payloads, **kwargs):
        calls: list[str] = []
        sock = _FakeSocket(datagrams)
        config_path = tmp_path / "config.sh"
        config_path.write_text(
            'HOST_IP="HOST_IP"\nHOST_PORT="9120"\nDASHBOARD_TOKEN="CHANGE_ME"\nCONTROL_TOKEN=""\n'
        )
        code = run_wizard(
            config_path=str(config_path),
            example_path=str(tmp_path / "absent.example"),
            listen_seconds=0.01,
            poll_interval=0.0,
            poll_timeout=5.0,
            sock=sock,
            urlopen=_urlopen_factory(payloads),
            device_name="kindle-test",
            **kwargs,
        )
        return code, config_path, calls

    def test_success_flow_writes_tokens(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(setup_wizard, "generate_device_credentials",
                            lambda: (DEVICE_ID, DEVICE_SECRET, "AB12-CD34"))
        code, config_path, _ = self._run(
            tmp_path,
            [_beacon_bytes()],
            [
                {"status": "pending"},
                {"status": "approved", "read_token": "read-token-abc123def456", "control_token": "ctrl-token-abc123def456"},
            ],
        )
        assert code == 0
        content = config_path.read_text()
        assert 'DASHBOARD_TOKEN="read-token-abc123def456"' in content
        assert 'CONTROL_TOKEN="ctrl-token-abc123def456"' in content
        assert 'HOST_IP="192.168.1.50"' in content

    def test_no_hosts_found_returns_2(self, tmp_path: Path) -> None:
        code, _, _ = self._run(tmp_path, [], [])
        assert code == 2

    def test_forbidden_returns_3(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(setup_wizard, "generate_device_credentials",
                            lambda: (DEVICE_ID, DEVICE_SECRET, "AB12-CD34"))
        code, _, _ = self._run(
            tmp_path,
            [_beacon_bytes()],
            [urllib.error.HTTPError("", 403, "forbidden", None, None)],
        )
        assert code == 3

    def test_bad_returned_token_returns_4(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(setup_wizard, "generate_device_credentials",
                            lambda: (DEVICE_ID, DEVICE_SECRET, "AB12-CD34"))
        code, _, _ = self._run(
            tmp_path,
            [_beacon_bytes()],
            [{"status": "approved", "read_token": "x", "control_token": ""}],
        )
        assert code == 4

    def test_unreachable_host_after_discovery_returns_3(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.request as _ur

        monkeypatch.setattr(setup_wizard, "generate_device_credentials",
                            lambda: (DEVICE_ID, DEVICE_SECRET, "AB12-CD34"))

        def boom(request, timeout=None):
            raise _ur.URLError("refused")

        sock = _FakeSocket([_beacon_bytes()])
        config_path = tmp_path / "config.sh"
        config_path.write_text('HOST_IP="HOST_IP"\n')
        code = run_wizard(
            config_path=str(config_path),
            example_path="",
            listen_seconds=0.01,
            poll_interval=0.0,
            poll_timeout=2.0,
            sock=sock,
            urlopen=boom,
        )
        assert code == 3
