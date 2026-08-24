"""Tests for Hermes-native controls (hermes_controls.py)."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from hermes_kindle_dashboard.actions import ActionRegistry
from hermes_kindle_dashboard.contract import build_default_layout
from hermes_kindle_dashboard.hermes_controls import (
    CliTransport,
    ControlRejectedError,
    HermesControlsConfig,
    HermesModelAction,
    HermesPromptAction,
    ModelPreferenceStore,
    control_tiles,
    load_controls_config,
    register_hermes_controls,
)


class FakeTransport:
    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.returncode = returncode

    def send_prompt(self, text: str, model: str | None = None) -> dict:
        self.calls.append(("prompt", text, model))
        return {"returncode": self.returncode, "stdout": "", "stderr": ""}


CONFIG = HermesControlsConfig(
    quick_prompts={"briefing": "Give me today's briefing.", "standup": "Draft a standup update."},
    models=["sonnet", "opus"],
)


class TestConfigLoading:
    def test_missing_file_disabled(self, tmp_path: Path) -> None:
        config = load_controls_config(tmp_path / "absent.yaml")
        assert config.enabled is False
        assert control_tiles(config) == []

    def test_valid_yaml_parsed(self, tmp_path: Path) -> None:
        path = tmp_path / "hermes_controls.yaml"
        path.write_text(
            "quick_prompts:\n  briefing: |\n    Give me today's briefing.\n"
            "models:\n  - sonnet\n  - opus\ncli_path: /usr/local/bin/hermes\n"
        )
        config = load_controls_config(path)
        assert config.enabled is True
        assert config.quick_prompts == {"briefing": "Give me today's briefing."}
        assert config.models == ["sonnet", "opus"]
        assert config.cli_path == "/usr/local/bin/hermes"

    def test_invalid_entries_filtered(self, tmp_path: Path) -> None:
        path = tmp_path / "hermes_controls.yaml"
        path.write_text(
            "quick_prompts:\n  'bad name!': x\n  ok_name: real\nmodels:\n  - 'no spaces'\n  - good\n"
        )
        config = load_controls_config(path)
        assert list(config.quick_prompts) == ["ok_name"]
        assert config.models == ["good"]

    def test_garbage_yaml_disabled_not_crash(self, tmp_path: Path) -> None:
        path = tmp_path / "hermes_controls.yaml"
        path.write_text("::::not yaml {{{")
        assert load_controls_config(path).enabled is False


class TestValidation:
    def test_unknown_prompt_rejected(self) -> None:
        with pytest.raises(ControlRejectedError):
            CONFIG.validate_prompt_name("nope")

    def test_unknown_model_rejected(self) -> None:
        with pytest.raises(ControlRejectedError):
            CONFIG.validate_model_alias("gpt-9")


class TestHandlers:
    def test_prompt_action_success_writes_result(self, tmp_path: Path) -> None:
        transport = FakeTransport()
        handler = HermesPromptAction(CONFIG, transport, tmp_path)
        payload = handler(action="hermes.prompt.briefing")
        assert payload["status"] == "ok"
        assert transport.calls == [("prompt", "Give me today's briefing.", None)]
        written = json.loads((tmp_path / "actions" / "hermes.prompt.briefing.json").read_text())
        assert written["status"] == "ok"

    def test_prompt_uses_saved_model_preference(self, tmp_path: Path) -> None:
        ModelPreferenceStore(tmp_path).set_model("sonnet")
        transport = FakeTransport()
        handler = HermesPromptAction(CONFIG, transport, tmp_path)
        handler(action="hermes.prompt.briefing")
        assert transport.calls == [("prompt", "Give me today's briefing.", "sonnet")]

    def test_prompt_action_unknown_name_never_calls_transport(self, tmp_path: Path) -> None:
        transport = FakeTransport()
        handler = HermesPromptAction(CONFIG, transport, tmp_path)
        payload = handler(action="hermes.prompt.injection")
        assert payload == {"status": "rejected", "reason": "unknown_prompt"}
        assert transport.calls == []

    def test_model_action_persists_preference(self, tmp_path: Path) -> None:
        handler = HermesModelAction(CONFIG, tmp_path)
        payload = handler(action="hermes.model.sonnet")
        assert payload["status"] == "ok"
        store = ModelPreferenceStore(tmp_path)
        assert store.get_model() == "sonnet"
        # Unknown alias never touches the preference.
        assert handler(action="hermes.model.gpt-9")["status"] == "rejected"
        assert store.get_model() == "sonnet"

    def test_model_preference_file_is_private(self, tmp_path: Path) -> None:
        HermesModelAction(CONFIG, tmp_path)(action="hermes.model.opus")
        assert (tmp_path / "hermes_controls_state.json").stat().st_mode & 0o777 == 0o600


class TestCliTransport:
    def test_send_prompt_builds_expected_argv_and_stdin(self) -> None:
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        config = HermesControlsConfig(cli_path="/usr/bin/hermes", cli_extra_args=["--profile", "work"])
        CliTransport(config, run=fake_run).send_prompt("hello world")
        assert captured["argv"] == [
            "/usr/bin/hermes", "--profile", "work", "chat", "--query-file", "-",
        ]
        assert captured["kwargs"]["input"] == "hello world"
        assert captured["kwargs"]["shell"] is False

    def test_model_preference_appended_as_flag(self) -> None:
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        CliTransport(HermesControlsConfig(), run=fake_run).send_prompt("hi", model="sonnet")
        assert captured["argv"][-2:] == ["-m", "sonnet"]

    def test_timeout_and_spawn_failure_are_contained(self) -> None:
        def slow_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 99)

        config = HermesControlsConfig(timeout_seconds=0.01)
        result = CliTransport(config, run=slow_run).send_prompt("x")
        assert result["returncode"] == -1 and result["stderr"] == "timeout"

        def boom(argv, **kwargs):
            raise FileNotFoundError("no such binary")

        result = CliTransport(config, run=boom).send_prompt("y")
        assert result["returncode"] == -1 and result["stderr"] == "spawn_failed"


class TestRegistryIntegration:
    def _registry(self, tmp_path: Path) -> tuple[ActionRegistry, FakeTransport]:
        registry = ActionRegistry(rate_limit_seconds=0.0)
        transport = FakeTransport()
        register_hermes_controls(registry, tmp_path, CONFIG, transport)
        return registry, transport

    def test_dispatch_end_to_end(self, tmp_path: Path) -> None:
        registry, transport = self._registry(tmp_path)
        registry.dispatch(action="hermes.prompt.standup", tile_id="t1", nonce="n1", ts=time.time())
        registry.wait_for_pending()
        assert ("prompt", "Draft a standup update.", None) in transport.calls

    def test_prefix_does_not_allow_arbitrary_names(self, tmp_path: Path) -> None:
        registry, transport = self._registry(tmp_path)
        registry.dispatch(action="hermes.prompt.evil", tile_id="t1", nonce="n2", ts=time.time())
        registry.wait_for_pending()
        assert all(name != "evil" for _, name, _ in transport.calls)

    def test_model_preference_via_registry(self, tmp_path: Path) -> None:
        registry, _transport = self._registry(tmp_path)
        registry.dispatch(action="hermes.model.opus", tile_id="t2", nonce="n3", ts=time.time())
        registry.wait_for_pending()
        assert ModelPreferenceStore(tmp_path).get_model() == "opus"

    def test_no_registration_when_unconfigured(self, tmp_path: Path) -> None:
        registry = ActionRegistry()
        empty = HermesControlsConfig(enabled=False)
        register_hermes_controls(registry, tmp_path, empty, FakeTransport())
        with pytest.raises(Exception):
            registry.dispatch(action="hermes.prompt.x", ts=time.time())


class TestLayoutTiles:
    def test_control_tiles_lead_the_grid(self) -> None:
        layout = build_default_layout(control_tiles=control_tiles(CONFIG))
        ids = [tile["id"] for tile in layout["tiles"]]
        # Two prompts + two models, in deterministic order, at grid positions 0..3.
        assert ids[:4] == [
            "hermes_prompt:briefing",
            "hermes_prompt:standup",
            "hermes_model:sonnet",
            "hermes_model:opus",
        ]
        actions = {tile["id"]: tile.get("action") for tile in layout["tiles"]}
        assert actions["hermes_prompt:briefing"] == "hermes.prompt.briefing"
        assert actions["hermes_model:opus"] == "hermes.model.opus"
        # Grid still filled to 16 action slots plus the panel.
        assert len(layout["tiles"]) == 17
