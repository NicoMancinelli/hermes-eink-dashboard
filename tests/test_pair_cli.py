"""Tests for the pairing admin CLI (``hermes-dashboard-pair``).

The CLI is exercised end to end against a real uvicorn server so that auth
headers, JSON bodies, status handling and output formatting are checked against
the actual routes rather than a hand-rolled fake. ``test_pairing_api.py``
covers the routes themselves and only touches the CLI's two argument-validation
branches; everything below closes the rest of the dispatch body.
"""

from __future__ import annotations

import io
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import uvicorn

from hermes_eink_dashboard.api import ApiSettings, create_app
from hermes_eink_dashboard.pair_cli import main as cli_main
from hermes_eink_dashboard.pairing import DeviceStore, PairingService, display_code_for

CONTROL_TOKEN = "cli-control-token"
DEVICE_ID = "0f1e2d3c4b5a6978"
DEVICE_SECRET = "ab" * 32
# Device names are shell-rendered into config.sh on the Kindle, so the pairing
# store deliberately rejects spaces (see NAME_RE in pairing.py).
DEVICE_NAME = "LivingRoomKindle"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_ready(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"server not ready on {port}")


def _direct_opener() -> urllib.request.OpenerDirector:
    """Opener that ignores proxy environment variables (loopback traffic)."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "control_token"
    path.write_text(CONTROL_TOKEN + "\n", encoding="utf-8")
    return path


@pytest.fixture
def live_host(tmp_path: Path):
    """Run the real pairing API on a background uvicorn thread."""
    port = _free_port()
    app = create_app(
        ApiSettings(
            token="read-token",
            control_token=CONTROL_TOKEN,
            pairing=PairingService(DeviceStore(tmp_path / "devices.json")),
        ),
        [],
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_ready(port)
    try:
        yield ("127.0.0.1", port)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _args(live_host, token_file: Path) -> list[str]:
    host, port = live_host
    return ["--host", host, "--port", str(port), "--control-token-file", str(token_file)]


def _register_pending(live_host, name: str = DEVICE_NAME, device_id: str = DEVICE_ID) -> str:
    """Register a pending device via the real (unauthenticated) poll route."""
    body = _poll(live_host, name=name, device_id=device_id)
    assert body["status"] == "pending"
    return display_code_for(device_id)


def _poll(live_host, name: str | None = None, device_id: str = DEVICE_ID) -> dict:
    host, port = live_host
    payload: dict = {"device_id": device_id, "device_secret": DEVICE_SECRET}
    if name is not None:
        payload["name"] = name
    request = urllib.request.Request(
        f"http://{host}:{port}/pair/poll",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    with _direct_opener().open(request, timeout=5) as response:
        return json.loads(response.read().decode())


class TestBuildParser:
    def test_defaults_come_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HERMES_DASHBOARD_HOST", "10.0.0.5")
        monkeypatch.setenv("HERMES_DASHBOARD_PORT", "9999")
        monkeypatch.setenv("HERMES_DASHBOARD_CONTROL_TOKEN_FILE", "/tmp/env-token")

        from hermes_eink_dashboard.pair_cli import build_parser

        args = build_parser().parse_args(["list"])
        assert args.host == "10.0.0.5"
        assert args.port == 9999
        assert args.control_token_file == Path("/tmp/env-token")

    def test_device_defaults_to_empty(self) -> None:
        from hermes_eink_dashboard.pair_cli import build_parser

        assert build_parser().parse_args(["list"]).device == ""

    def test_explicit_flags_override_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HERMES_DASHBOARD_HOST", "10.0.0.5")
        from hermes_eink_dashboard.pair_cli import build_parser

        assert build_parser().parse_args(["--host", "192.168.1.2", "list"]).host == "192.168.1.2"


class TestArgumentAndTokenValidation:
    def test_deny_requires_device(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        assert cli_main(["deny", "--control-token-file", str(tmp_path / "absent")]) == 2
        assert "requires a display code" in capsys.readouterr().err

    def test_whitespace_only_device_is_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        assert cli_main(["approve", "   ", "--control-token-file", str(tmp_path / "absent")]) == 2
        assert "requires a display code" in capsys.readouterr().err

    def test_missing_token_file_names_the_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        missing = tmp_path / "absent"
        assert cli_main(["list", "--control-token-file", str(missing)]) == 1
        err = capsys.readouterr().err
        assert "no control token" in err
        assert str(missing) in err
        assert "install_host.sh" in err

    def test_blank_token_file_is_treated_as_missing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        path = tmp_path / "control_token"
        path.write_text("   \n", encoding="utf-8")
        assert cli_main(["list", "--control-token-file", str(path)]) == 1
        assert "no control token" in capsys.readouterr().err


class TestList:
    def test_reports_when_there_are_no_devices(self, live_host, token_file, capsys) -> None:
        assert cli_main([*_args(live_host, token_file), "list"]) == 0
        assert "No pending or paired devices." in capsys.readouterr().out

    def test_marks_pending_devices_and_shows_the_approve_hint(
        self, live_host, token_file, capsys
    ) -> None:
        code = _register_pending(live_host)
        assert cli_main([*_args(live_host, token_file), "list"]) == 0
        out = capsys.readouterr().out
        assert code in out
        assert DEVICE_NAME in out
        assert "pending" in out
        assert out.startswith("* ")  # pending marker
        assert "hermes-dashboard-pair approve <CODE>" in out

    def test_approved_device_shows_timestamp_and_drops_the_pending_marker(
        self, live_host, token_file, capsys
    ) -> None:
        code = _register_pending(live_host)
        assert cli_main([*_args(live_host, token_file), "approve", code]) == 0
        capsys.readouterr()

        assert cli_main([*_args(live_host, token_file), "list"]) == 0
        out = capsys.readouterr().out
        assert "approved" in out
        assert not out.startswith("*")
        assert "hermes-dashboard-pair approve <CODE>" not in out  # no pending devices

    def test_wrong_token_reports_the_server_status(self, live_host, tmp_path, capsys) -> None:
        bad = tmp_path / "bad_token"
        bad.write_text("wrong-token", encoding="utf-8")
        assert cli_main([*_args(live_host, bad), "list"]) == 1
        assert "/pair/devices returned 401: unauthorized" in capsys.readouterr().err


class TestApprove:
    def test_approve_by_display_code_prints_the_device_name(
        self, live_host, token_file, capsys
    ) -> None:
        code = _register_pending(live_host)
        assert cli_main([*_args(live_host, token_file), "approve", code]) == 0
        out = capsys.readouterr().out
        assert f"Approved {DEVICE_NAME}." in out
        assert "next poll" in out

    def test_approve_by_raw_device_id_also_works(self, live_host, token_file, capsys) -> None:
        _register_pending(live_host)
        assert cli_main([*_args(live_host, token_file), "approve", DEVICE_ID]) == 0
        assert f"Approved {DEVICE_NAME}." in capsys.readouterr().out

    def test_approved_device_receives_its_tokens_on_the_next_poll(
        self, live_host, token_file
    ) -> None:
        """The CLI's effect is verified against the device-flow contract."""
        code = _register_pending(live_host)
        assert cli_main([*_args(live_host, token_file), "approve", code]) == 0

        host, port = live_host
        assert cli_main([*_args(live_host, token_file), "approve", code]) == 0

        approved = _poll(live_host)
        assert approved["status"] == "approved"
        assert approved["read_token"]
        assert approved["control_token"]
        assert approved["device_name"] == DEVICE_NAME

    def test_device_name_with_a_space_is_rejected_by_the_store(
        self, live_host, token_file, capsys
    ) -> None:
        """Names are shell-rendered on the Kindle, so the store refuses spaces.

        The CLI surfaces that as a plain 400 rather than crashing.
        """
        host, port = live_host
        request = urllib.request.Request(
            f"http://{host}:{port}/pair/poll",
            data=json.dumps(
                {"device_id": DEVICE_ID, "device_secret": DEVICE_SECRET, "name": "Bad Name!"}
            ).encode("utf-8"),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _direct_opener().open(request, timeout=5)
        assert excinfo.value.code == 400

        assert cli_main([*_args(live_host, token_file), "list"]) == 0
        assert "No pending or paired devices." in capsys.readouterr().out

    def test_unknown_device_is_a_clear_error(self, live_host, token_file, capsys) -> None:
        assert cli_main([*_args(live_host, token_file), "approve", "ZZ99-ZZ99"]) == 1
        err = capsys.readouterr().err
        assert "unknown device" in err
        assert "'ZZ99-ZZ99'" in err
        assert "hermes-dashboard-pair list" in err

    def test_bad_token_on_approve_reports_status(self, live_host, tmp_path, capsys) -> None:
        code = _register_pending(live_host)
        bad = tmp_path / "bad_token"
        bad.write_text("wrong-token", encoding="utf-8")
        assert cli_main([*_args(live_host, bad), "approve", code]) == 1
        assert "/pair/approve returned 401: unauthorized" in capsys.readouterr().err


class TestDeny:
    def test_deny_removes_the_device(self, live_host, token_file, capsys) -> None:
        code = _register_pending(live_host)
        assert cli_main([*_args(live_host, token_file), "deny", code]) == 0
        assert "Device removed." in capsys.readouterr().out

        # The record is gone, so listing is empty again.
        assert cli_main([*_args(live_host, token_file), "list"]) == 0
        assert "No pending or paired devices." in capsys.readouterr().out

    def test_denied_device_must_be_approved_again(self, live_host, token_file) -> None:
        """A deny is not an outright ban, but it revokes every issued token.

        The record is dropped, so the next poll starts a *fresh* pending request
        that never receives tokens until an operator approves it again.
        """
        code = _register_pending(live_host)
        assert cli_main([*_args(live_host, token_file), "deny", code]) == 0

        host, port = live_host
        body = _poll(live_host)
        assert body == {"status": "pending"}
        assert "read_token" not in body

        # Re-approving works, which is what the pending state is for.
        assert cli_main([*_args(live_host, token_file), "approve", code]) == 0
        approved = _poll(live_host)
        assert approved["status"] == "approved"
        assert approved["read_token"] and approved["control_token"]

    def test_unknown_device_is_a_clear_error(self, live_host, token_file, capsys) -> None:
        assert cli_main([*_args(live_host, token_file), "deny", "ZZ99-ZZ99"]) == 1
        assert "unknown device" in capsys.readouterr().err


class TestRequestHelper:
    def _patched(self, monkeypatch, impl):
        import hermes_eink_dashboard.pair_cli as pair_cli

        monkeypatch.setattr(pair_cli.urllib.request, "urlopen", impl)

    def test_get_request_shape(self, monkeypatch) -> None:
        import hermes_eink_dashboard.pair_cli as pair_cli

        captured: dict = {}

        class _Response:
            status = 200

            def read(self) -> bytes:
                return json.dumps({"devices": []}).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

        def fake(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["body"] = request.data
            captured["auth"] = request.get_header("Authorization")
            captured["content_type"] = request.get_header("Content-type")
            captured["timeout"] = timeout
            return _Response()

        self._patched(monkeypatch, fake)
        status, body = pair_cli._request("http://h:1", "/pair/devices", "tok")

        assert (status, body) == (200, {"devices": []})
        assert captured["url"] == "http://h:1/pair/devices"
        assert captured["method"] == "GET"
        assert captured["body"] is None
        assert captured["auth"] == "Bearer tok"
        assert captured["content_type"] is None
        assert captured["timeout"] == 10

    def test_post_request_shape(self, monkeypatch) -> None:
        import hermes_eink_dashboard.pair_cli as pair_cli

        captured: dict = {}

        class _Response:
            status = 200

            def read(self) -> bytes:
                return json.dumps({"ok": True}).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

        def fake(request, timeout=None):
            captured["method"] = request.get_method()
            captured["body"] = request.data
            captured["content_type"] = request.get_header("Content-type")
            return _Response()

        self._patched(monkeypatch, fake)
        status, body = pair_cli._request("http://h", "/pair/deny", "tok", {"device": "AB12-CD34"})

        assert (status, body) == (200, {"ok": True})
        assert captured["method"] == "POST"
        assert json.loads(captured["body"].decode("utf-8")) == {"device": "AB12-CD34"}
        assert captured["content_type"] == "application/json"

    def test_json_error_body_is_returned(self, monkeypatch) -> None:
        import hermes_eink_dashboard.pair_cli as pair_cli

        body = json.dumps({"detail": "rate_limited"}).encode("utf-8")

        def fake(request, timeout=None):
            raise urllib.error.HTTPError("http://h", 429, "Too Many", None, io.BytesIO(body))

        self._patched(monkeypatch, fake)
        assert pair_cli._request("http://h", "/pair/devices", "tok") == (
            429,
            {"detail": "rate_limited"},
        )

    def test_non_json_error_body_degrades_to_empty_dict(self, monkeypatch) -> None:
        """A proxy/crash HTML page must not crash the CLI."""
        import hermes_eink_dashboard.pair_cli as pair_cli

        def fake(request, timeout=None):
            raise urllib.error.HTTPError(
                "http://h",
                503,
                "Bad Gateway",
                None,
                io.BytesIO(b"<html>502 Bad Gateway</html>"),
            )

        self._patched(monkeypatch, fake)
        assert pair_cli._request("http://h", "/pair/devices", "tok") == (503, {})
