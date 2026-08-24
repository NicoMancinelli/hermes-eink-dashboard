"""Hermes-native controls exposed as dashboard actions.

This module bridges the dashboard's ``/control`` pipeline to the local
Hermes Agent installation so the Kindle can do more than generic shell
workflows: it can fire preconfigured prompts at the agent and switch the
active model.

Design constraints inherited from the security model:

* Actions arrive as static strings attached to tiles (no free-form payload
  from devices), so every parameterizable control is expressed as
  ``hermes.prompt.<name>`` / ``hermes.model.<alias>`` where ``<name>`` and
  ``<alias>`` MUST exist in the host-side allowlist loaded from
  ``~/.config/hermes-kindle-dashboard/hermes_controls.yaml``. Unknown names
  are rejected before any subprocess or network call happens.
* All configuration lives on the trusted host; the Kindle only ever sees
  tile labels.
"""
from __future__ import annotations

import json
import os
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

LOGGER = logging.getLogger("hermes-kindle-dashboard.hermes_controls")

MAX_CONFIG_BYTES = 1_048_576  # 1MB, same cap as actions.yaml
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


class ControlRejectedError(Exception):
    """Raised when an action references a name not present in the allowlist."""


@dataclass(frozen=True)
class HermesControlsConfig:
    """Host-side allowlists for native controls."""

    enabled: bool = True
    # Executable used by the CLI transport (absolute path recommended).
    cli_path: str = "hermes"
    # Extra arguments prepended to every CLI invocation (e.g. ["--config", "/path"]).
    cli_extra_args: list[str] = field(default_factory=list)
    # Named quick prompts: action hermes.prompt.<name> sends quick_prompts[name].
    quick_prompts: dict[str, str] = field(default_factory=dict)
    # Allowed model aliases: action hermes.model.<alias> switches to models entry.
    models: list[str] = field(default_factory=list)
    timeout_seconds: float = 120.0

    def prompt_names(self) -> list[str]:
        return sorted(self.quick_prompts)

    def validate_prompt_name(self, name: str) -> str:
        if name not in self.quick_prompts:
            raise ControlRejectedError("unknown_prompt")
        return self.quick_prompts[name]

    def validate_model_alias(self, alias: str) -> str:
        if alias not in self.models:
            raise ControlRejectedError("unknown_model")
        return alias


def load_controls_config(path: Path, logger: logging.Logger | None = None) -> HermesControlsConfig:
    """Load hermes_controls.yaml (missing file -> disabled defaults)."""
    log = logger or LOGGER
    path = Path(path)
    if not path.exists():
        return HermesControlsConfig(enabled=False)
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            log.warning("controls config exceeds size cap; ignoring")
            return HermesControlsConfig(enabled=False)
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        log.exception("failed reading %s; controls stay disabled", path)
        return HermesControlsConfig(enabled=False)
    if not isinstance(data, dict):
        return HermesControlsConfig(enabled=False)

    prompts_raw = data.get("quick_prompts") or {}
    models_raw = data.get("models") or []
    quick_prompts: dict[str, str] = {}
    if isinstance(prompts_raw, dict):
        for name, text in prompts_raw.items():
            if isinstance(name, str) and NAME_RE.fullmatch(name) and isinstance(text, str) and text.strip():
                quick_prompts[name] = text.strip()
    models: list[str] = [
        alias for alias in models_raw if isinstance(alias, str) and NAME_RE.fullmatch(alias)
    ][:32]
    cli_path = data.get("cli_path")
    extra = data.get("cli_extra_args") or []
    return HermesControlsConfig(
        enabled=bool(data.get("enabled", True)),
        cli_path=cli_path if isinstance(cli_path, str) and cli_path else "hermes",
        cli_extra_args=[str(item) for item in extra] if isinstance(extra, list) else [],
        quick_prompts=quick_prompts,
        models=models,
        timeout_seconds=float(data.get("timeout_seconds", 120.0)) or 120.0,
    )


class HermesTransport(Protocol):
    """Anything that can actuate Hermes on behalf of a validated control."""

    def send_prompt(self, text: str, model: str | None = None) -> dict[str, Any]: ...


class ModelPreferenceStore:
    """Persists which configured model quick prompts are sent with.

    Hermes' own ``hermes model`` picker requires a TTY, so there is no
    scriptable way to change its global default from outside. Instead the
    dashboard keeps a host-side preference and passes ``chat -m <alias>`` on
    each quick-prompt run; the agent's own default stays untouched.
    """

    FILENAME = "hermes_controls_state.json"

    def __init__(self, config_dir: Path | str) -> None:
        self.config_dir = Path(config_dir)

    @property
    def path(self) -> Path:
        return self.config_dir / self.FILENAME

    def get_model(self) -> str:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        model = data.get("quick_prompt_model", "") if isinstance(data, dict) else ""
        return model if isinstance(model, str) else ""

    def set_model(self, alias: str) -> dict[str, Any]:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        payload = {"quick_prompt_model": alias, "updated_at": datetime.now(timezone.utc).isoformat()}
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        return payload


class CliTransport:
    """Runs ``hermes chat --query-file -`` with the prompt piped via stdin.

    Upstream documents ``--query-file`` as safe for arbitrary text (nothing
    shell-interpreted), and stdin delivery keeps prompt contents out of the
    process list.
    """

    def __init__(
        self,
        config: HermesControlsConfig,
        run=subprocess.run,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self._run = run
        self.logger = logger or LOGGER

    def send_prompt(self, text: str, model: str | None = None) -> dict[str, Any]:
        argv = [self.config.cli_path, *self.config.cli_extra_args]
        argv += ["chat", "--query-file", "-"]
        if model:
            argv += ["-m", model]
        return self._execute(argv, kind="prompt", stdin=text)

    def _execute(self, argv: list[str], kind: str, stdin: str = "") -> dict[str, Any]:
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            proc = self._run(
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                shell=False,
            )
            result = {
                "kind": kind,
                "returncode": proc.returncode,
                "stdout": proc.stdout[-2000:],
                "stderr": proc.stderr[-2000:],
            }
        except subprocess.TimeoutExpired:
            self.logger.error("hermes %s timed out after %ss", kind, self.config.timeout_seconds)
            result = {"kind": kind, "returncode": -1, "stdout": "", "stderr": "timeout"}
        except Exception:
            self.logger.exception("hermes %s failed", kind)
            result = {"kind": kind, "returncode": -1, "stdout": "", "stderr": "spawn_failed"}
        result["updated_at"] = now_iso
        return result


def _write_result(config_dir: Path, filename: str, payload: dict[str, Any]) -> None:
    out_dir = config_dir / "actions"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / filename).write_text(json.dumps(payload, indent=2), encoding="utf-8")


class HermesPromptAction:
    """Fire a named, host-configured prompt at the agent."""

    def __init__(
        self,
        config: HermesControlsConfig,
        transport: HermesTransport,
        config_dir: Path | str,
        logger: logging.Logger | None = None,
        preferences: ModelPreferenceStore | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.config_dir = Path(config_dir)
        self.preferences = preferences or ModelPreferenceStore(self.config_dir)
        self.logger = logger or LOGGER

    PREFIX = "hermes.prompt"

    def __call__(self, tile_id: str = "", action: str = "", **kwargs: Any) -> dict[str, Any]:
        name = ""
        for candidate in (action, tile_id):
            for separator in (".", ":"):
                prefix = f"{self.PREFIX}{separator}"
                if candidate.startswith(prefix):
                    name = candidate[len(prefix):]
                    break
            if name:
                break
        try:
            text = self.config.validate_prompt_name(name)
        except ControlRejectedError:
            self.logger.warning("rejected hermes prompt control: unknown name")
            return {"status": "rejected", "reason": "unknown_prompt"}

        model = self.preferences.get_model() or None
        result = self.transport.send_prompt(text, model=model)
        payload = {"status": "ok" if result.get("returncode") == 0 else "error", "prompt": name, **result}
        _write_result(self.config_dir, f"{self.PREFIX}.{name}.json", payload)
        return payload


class HermesModelAction:
    """Set the model used for subsequent quick-prompt runs.

    This is a dashboard-side preference (persisted to
    ``hermes_controls_state.json``), not a change to Hermes' own global
    default — upstream has no non-interactive model setter.
    """

    def __init__(
        self,
        config: HermesControlsConfig,
        config_dir: Path | str,
        logger: logging.Logger | None = None,
        preferences: ModelPreferenceStore | None = None,
    ) -> None:
        self.config = config
        self.config_dir = Path(config_dir)
        self.preferences = preferences or ModelPreferenceStore(self.config_dir)
        self.logger = logger or LOGGER

    PREFIX = "hermes.model"

    def __call__(self, tile_id: str = "", action: str = "", **kwargs: Any) -> dict[str, Any]:
        alias = ""
        for candidate in (action, tile_id):
            for separator in (".", ":"):
                prefix = f"{self.PREFIX}{separator}"
                if candidate.startswith(prefix):
                    alias = candidate[len(prefix):]
                    break
            if alias:
                break
        try:
            self.config.validate_model_alias(alias)
        except ControlRejectedError:
            self.logger.warning("rejected hermes model control: unknown alias")
            return {"status": "rejected", "reason": "unknown_model"}

        payload = self.preferences.set_model(alias)
        payload.update({"status": "ok", "kind": "model_preference", "alias": alias})
        _write_result(self.config_dir, f"{self.PREFIX}.{alias}.json", payload)
        return payload


def register_hermes_controls(
    registry: Any,
    config_dir: Path | str,
    config: HermesControlsConfig,
    transport: HermesTransport,
    logger: logging.Logger | None = None,
) -> None:
    """Register native controls when configured."""
    if not config.enabled or (not config.quick_prompts and not config.models):
        return
    cfg_dir = Path(config_dir)
    if config.quick_prompts:
        registry.register(
            HermesPromptAction.PREFIX,
            handler=HermesPromptAction(config, transport, cfg_dir, logger),
        )
    if config.models:
        registry.register(
            HermesModelAction.PREFIX,
            handler=HermesModelAction(config, cfg_dir, logger),
        )


def control_tiles(config: HermesControlsConfig) -> list[dict[str, str]]:
    """Tile descriptors for the default layout (id/label/action dicts)."""
    tiles: list[dict[str, str]] = []
    for name in config.prompt_names():
        label = f"Ask: {name.replace('_', ' ').replace('-', ' ').title()[:18]}"
        tiles.append({"id": f"hermes_prompt:{name}", "label": label, "action": f"{HermesPromptAction.PREFIX}.{name}"})
    for alias in config.models:
        tiles.append({
            "id": f"hermes_model:{alias}",
            "label": f"Model: {alias[:16]}",
            "action": f"{HermesModelAction.PREFIX}.{alias}",
        })
    return tiles
