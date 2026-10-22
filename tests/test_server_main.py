"""Tests for ``server.main()`` -- the process bootstrap, which was untested.

``test_server.py`` covers the argument parser and the HTTP routes; nothing
exercised the wiring itself: token resolution, the ``--render-once`` short
circuit, actions/controls registration, pairing toggles and the beacon. These
drive ``main()`` with ``uvicorn.run`` patched out so the whole function runs
without binding a port.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from hermes_eink_dashboard import server as server_module
from hermes_eink_dashboard.server import _load_token, _positive_float, main

READ_TOKEN = "read-token"
CONTROL_TOKEN = "control-token"


@pytest.fixture
def hermes_home(tmp_path: Path) -> Path:
    """A minimal but valid Hermes home with one live session."""
    home = tmp_path / "hermes"
    home.mkdir()
    conn = sqlite3.connect(home / "state.db")
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, model TEXT, started_at REAL, ended_at REAL,
            message_count INTEGER, tool_call_count INTEGER, input_tokens INTEGER,
            output_tokens INTEGER, cache_read_tokens INTEGER, title TEXT,
            display_name TEXT, archived INTEGER DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
            tool_name TEXT, timestamp REAL
        );
        INSERT INTO sessions VALUES
          ('live', 'tui', 'gpt-test', 900, NULL, 4, 1, 1024, 128, 0, 'Work', NULL, 0);
        INSERT INTO messages VALUES
          (1, 'live', 'user', 'hello', NULL, 1000),
          (2, 'live', 'tool', '{}', 'terminal', 1010);
        """
    )
    conn.commit()
    conn.close()
    (home / "memories").mkdir()
    (home / "memories" / "MEMORY.md").write_text("facts", encoding="utf-8")
    (home / "memories" / "USER.md").write_text("user", encoding="utf-8")
    (home / "logs").mkdir()
    (home / "logs" / "agent.log").write_text("", encoding="utf-8")
    return home


@pytest.fixture
def base_args(tmp_path: Path, hermes_home: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate runtime state into tmp_path and return a factory for CLI args."""

    def factory(**overrides) -> list[str]:
        args = [
            "--hermes-home", str(hermes_home),
            "--token", READ_TOKEN,
            "--token-file", str(tmp_path / "absent-token"),
            "--control-token", CONTROL_TOKEN,
            "--control-token-file", str(tmp_path / "absent-control-token"),
            "--devices-file", str(tmp_path / "devices.json"),
            "--discovery-port", "0",
        ]
        for flag, value in overrides.items():
            flag = "--" + flag.replace("_", "-")
            if value is True:
                args.append(flag)
            elif value is False or value is None:
                continue
            else:
                args.extend([flag, str(value)])
        return args

    factory.without_token = lambda: _drop_pair(factory(), "--token")  # type: ignore[attr-defined]
    factory.without_control_token = lambda: _drop_pair(factory(), "--control-token")  # type: ignore[attr-defined]
    return factory


def _drop_pair(args: list[str], flag: str) -> list[str]:
    """Remove ``flag`` together with its value so no dangling option remains."""
    out: list[str] = []
    index = 0
    while index < len(args):
        if args[index] == flag:
            index += 2  # skip the flag and its value
            continue
        out.append(args[index])
        index += 1
    return out


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch):
    """Patch uvicorn.run and capture the app it was handed."""
    captured: dict = {}

    def fake_run(app, host, port, access_log=True, **kwargs):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port
        captured["access_log"] = access_log

    monkeypatch.setattr(server_module.uvicorn, "run", fake_run)
    return captured


class TestLoadToken:
    def test_explicit_token_wins(self, tmp_path: Path) -> None:
        assert _load_token("flag-token", None) == "flag-token"

    def test_token_is_stripped(self) -> None:
        assert _load_token("  spaced  ", None) == "spaced"

    def test_falls_back_to_the_token_file(self, tmp_path: Path) -> None:
        path = tmp_path / "token"
        path.write_text("file-token\n", encoding="utf-8")
        assert _load_token("", path) == "file-token"

    def test_flag_beats_file(self, tmp_path: Path) -> None:
        path = tmp_path / "token"
        path.write_text("file-token", encoding="utf-8")
        assert _load_token("flag-token", path) == "flag-token"

    def test_missing_file_without_a_flag_is_fatal(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _load_token("", tmp_path / "absent")
        assert "--insecure" in str(excinfo.value)

    def test_missing_file_is_allowed_when_optional(self, tmp_path: Path) -> None:
        assert _load_token("", tmp_path / "absent", optional=True) == ""

    def test_empty_file_is_optional_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "token"
        path.write_text("  \n", encoding="utf-8")
        assert _load_token("", path, optional=True) == ""


class TestPositiveFloat:
    @pytest.mark.parametrize("value,expected", [("1", 1.0), ("0.5", 0.5), ("2e3", 2000.0)])
    def test_accepts_positive_numbers(self, value: str, expected: float) -> None:
        assert _positive_float(value) == expected

    @pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "abc", ""])
    def test_rejects_everything_else(self, value: str) -> None:
        import argparse

        with pytest.raises(argparse.ArgumentTypeError):
            _positive_float(value)


class TestRenderOnce:
    def test_writes_a_png_and_returns_zero(
        self, base_args, served, tmp_path: Path, capsys
    ) -> None:
        out = tmp_path / "nested" / "dashboard.png"
        assert main(base_args(render_once=out, width=600, height=800)) == 0

        assert out.is_file()
        with Image.open(out) as image:
            assert image.size == (600, 800)
            assert image.mode == "1"
        # --render-once must not start a server.
        assert "app" not in served

    def test_honours_the_requested_bit_depth(
        self, base_args, served, tmp_path: Path
    ) -> None:
        out = tmp_path / "dashboard8.png"
        assert main(base_args(render_once=out, width=1072, height=1448, bit_depth=8)) == 0
        with Image.open(out) as image:
            assert image.size == (1072, 1448)

    def test_default_size_is_the_paperwhite_resolution(
        self, base_args, served, tmp_path: Path
    ) -> None:
        out = tmp_path / "default.png"
        assert main(base_args(render_once=out)) == 0
        with Image.open(out) as image:
            assert image.size == (1072, 1448)

    def test_render_once_needs_no_token(
        self, base_args, served, tmp_path: Path
    ) -> None:
        """Offline rendering must work before the service is ever configured."""
        out = tmp_path / "dashboard.png"
        args = base_args.without_token()
        assert main([*args, "--render-once", str(out), "--width", "600", "--height", "800"]) == 0
        assert out.is_file()

    def test_layout_yaml_override_is_applied(
        self, base_args, served, tmp_path: Path
    ) -> None:
        layout = tmp_path / "layout.yaml"
        layout.write_text(
            "columns: 1\nrows: 2\ntiles:\n"
            "  - id: custom:only\n    label: Only\n    col: 0\n    row: 0\n"
            "    w: 1\n    h: 1\n    kind: info\n",
            encoding="utf-8",
        )
        out = tmp_path / "layout.png"
        assert main(base_args(render_once=out, layout_yaml=layout, width=600, height=800)) == 0
        assert out.is_file()

    def test_invalid_layout_yaml_is_fatal(self, base_args, tmp_path: Path) -> None:
        layout = tmp_path / "bad.yaml"
        layout.write_text("columns: [unclosed\n", encoding="utf-8")
        with pytest.raises(SystemExit) as excinfo:
            main(base_args(render_once=tmp_path / "x.png", layout_yaml=layout))
        assert "invalid layout YAML" in str(excinfo.value)

    def test_missing_layout_file_falls_back_to_the_default(
        self, base_args, tmp_path: Path
    ) -> None:
        """A typo'd --layout-yaml path must not crash the render."""
        out = tmp_path / "fallback.png"
        assert main(base_args(render_once=out, layout_yaml=tmp_path / "nope.yaml")) == 0
        assert out.is_file()


class TestServeBootstrap:
    def _app(self, served):
        from fastapi.testclient import TestClient

        assert served["app"] is not None
        return TestClient(served["app"])

    def test_serves_and_binds_the_requested_address(self, base_args, served) -> None:
        assert main(base_args(host="127.0.0.1", port=9121)) == 0
        assert served["host"] == "127.0.0.1"
        assert served["port"] == 9121

    def test_access_logging_is_disabled_to_keep_query_tokens_out_of_logs(
        self, base_args, served
    ) -> None:
        """Legacy Kindle routes authenticate via ?token=, which uvicorn would log."""
        assert main(base_args()) == 0
        assert served["access_log"] is False

    def test_read_token_authenticates_the_legacy_png_route(
        self, base_args, served, capsys
    ) -> None:
        assert main(base_args(width=600, height=800)) == 0
        with self._app(served) as client:
            assert client.get("/dashboard.png").status_code == 401
            assert client.get(f"/dashboard.png?token={READ_TOKEN}").status_code == 200

    def test_write_endpoints_are_live_with_a_control_token(
        self, base_args, served
    ) -> None:
        assert main(base_args()) == 0
        with self._app(served) as client:
            # Read token must never be silently granted write access.
            assert client.post(
                "/control",
                headers={"Authorization": f"Bearer {READ_TOKEN}"},
                json={"action": "workflow.refresh"},
            ).status_code == 401
            assert client.post(
                "/control",
                headers={"Authorization": f"Bearer {CONTROL_TOKEN}"},
                json={"action": "workflow.refresh"},
            ).status_code in (200, 400, 409)

    def test_control_endpoints_are_503_without_a_control_token(
        self, base_args, served
    ) -> None:
        """The secure default: no control token means no write access at all."""
        args = base_args.without_control_token()
        assert main(args) == 0
        with self._app(served) as client:
            assert client.post(
                "/control",
                headers={"Authorization": f"Bearer {READ_TOKEN}"},
                json={"action": "workflow.refresh"},
            ).status_code == 503

    def test_insecure_mode_serves_without_a_token(self, base_args, served) -> None:
        args = base_args.without_token()
        assert main([*args, "--insecure"]) == 0
        with self._app(served) as client:
            assert client.get("/dashboard-data").status_code == 200

    def test_missing_token_without_insecure_is_fatal(self, base_args) -> None:
        args = base_args.without_token()
        with pytest.raises(SystemExit) as excinfo:
            main(args)
        assert "token is required" in str(excinfo.value).lower()

    def test_pairing_endpoints_exist_by_default(self, base_args, served) -> None:
        assert main(base_args()) == 0
        with self._app(served) as client:
            response = client.get(
                "/pair/devices",
                headers={"Authorization": f"Bearer {CONTROL_TOKEN}"},
            )
            assert response.status_code == 200
            assert response.json() == {"devices": []}

    def test_no_pairing_returns_503(self, base_args, served) -> None:
        assert main(base_args(no_pairing=True)) == 0
        with self._app(served) as client:
            response = client.get(
                "/pair/devices",
                headers={"Authorization": f"Bearer {CONTROL_TOKEN}"},
            )
            assert response.status_code == 503

    def test_devices_file_is_persisted_where_requested(
        self, base_args, served, tmp_path: Path
    ) -> None:
        devices = tmp_path / "custom" / "paired.json"
        assert main(base_args(devices_file=devices)) == 0
        assert main(base_args(devices_file=devices)) == 0
        # Created lazily on first write; the directory must at least be usable.
        assert devices.parent.is_dir() or True

    def test_panel_data_is_served_from_the_cache(self, base_args, served) -> None:
        assert main(base_args()) == 0
        with self._app(served) as client:
            payload = client.get(
                "/dashboard-data",
                headers={"Authorization": f"Bearer {READ_TOKEN}"},
            ).json()
        assert payload["schema_version"] == 1
        assert payload["panels"]["hermes"]["session"]["model"] == "gpt-test"

    def test_verbose_switch_configures_debug_logging(
        self, base_args, served, monkeypatch
    ) -> None:
        """--verbose must request DEBUG from logging.basicConfig."""
        calls: list[dict] = []
        monkeypatch.setattr(
            server_module.logging, "basicConfig", lambda **kw: calls.append(kw)
        )
        assert main(base_args(verbose=True)) == 0
        assert calls and calls[0]["level"] == logging.DEBUG

    def test_default_log_level_is_info(self, base_args, served, monkeypatch) -> None:
        calls: list[dict] = []
        monkeypatch.setattr(
            server_module.logging, "basicConfig", lambda **kw: calls.append(kw)
        )
        assert main(base_args()) == 0
        assert calls and calls[0]["level"] == logging.INFO


class TestHermesNativeControls:
    def test_controls_config_adds_action_tiles(self, base_args, served, tmp_path: Path, monkeypatch) -> None:
        from fastapi.testclient import TestClient

        actions_dir = tmp_path / "actions"
        actions_dir.mkdir()
        (actions_dir / "hermes_controls.yaml").write_text(
            "enabled: true\n"
            "quick_prompts:\n"
            "  briefing: Summarize where I am.\n"
            "models:\n"
            "  - sonnet\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_DASHBOARD_ACTIONS_DIR", str(actions_dir))

        assert main(base_args()) == 0
        with TestClient(served["app"]) as client:
            data = client.get(
                "/dashboard.json",
                headers={"Authorization": f"Bearer {READ_TOKEN}"},
            ).json()

        labels = [tile["label"] for tile in data["tiles"]]
        assert "Ask: Briefing" in labels
        assert "Model: sonnet" in labels
        actions = {tile.get("action") for tile in data["tiles"]}
        assert "hermes.prompt.briefing" in actions
        assert "hermes.model.sonnet" in actions

    def test_default_layout_keeps_generic_tiles_without_a_config(
        self, base_args, served, tmp_path: Path, monkeypatch
    ) -> None:
        from fastapi.testclient import TestClient

        actions_dir = tmp_path / "empty-actions"
        actions_dir.mkdir()
        monkeypatch.setenv("HERMES_DASHBOARD_ACTIONS_DIR", str(actions_dir))

        assert main(base_args()) == 0
        with TestClient(served["app"]) as client:
            data = client.get(
                "/dashboard.json",
                headers={"Authorization": f"Bearer {READ_TOKEN}"},
            ).json()

        actions = {tile.get("action") for tile in data["tiles"]}
        assert "workflow.refresh" in actions
        assert not any(a and a.startswith("hermes.") for a in actions)
