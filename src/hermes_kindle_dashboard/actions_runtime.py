"""Production action handlers for the Hermes E-Ink Dashboard.

Phase 4 of the interactive dashboard plan. Each handler is a callable
that registers with an `ActionRegistry` (Phase 1) and runs in response
to a `POST /control` event.

Handlers:
- WorkflowAction: runs a configured shell command as argv list. Reads from
  a config file at ~/.config/hermes-kindle-dashboard/actions.yaml.
  Each line is `name: [json_argv]`. Lines starting with # are comments.
- AlertDismissAction: writes {alert_id: ISO timestamp} to
  ~/.config/hermes-kindle-dashboard/dismissed_alerts.json.
- ContextSetAction: writes {'context': str, 'updated_at': ISO} to
  ~/.config/hermes-kindle-dashboard/context.json.
- RefreshAction: forces the panel cache to refresh (no-op when no cache).

register_all_actions() wires all four handlers to an ActionRegistry.
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .actions import ActionRegistry
from .contract import PanelCache

LOGGER = logging.getLogger("hermes-kindle-dashboard.actions_runtime")


MAX_ACTION_CONFIG_SIZE_BYTES = 1_048_576  # 1MB


def parse_action_config(content: str, logger: logging.Logger | None = None) -> dict[str, list[str]]:
    """Parse a YAML-ish workflow config file. No PyYAML dependency.

    Each non-comment, non-empty line is `name: [json_argv]`. Malformed
    lines are logged and skipped.
    """
    log = logger or LOGGER
    if len(content.encode("utf-8")) > MAX_ACTION_CONFIG_SIZE_BYTES:
        log.warning("Action config exceeds maximum allowed size of 1MB (%d bytes)", len(content.encode("utf-8")))
        return {}
    result: dict[str, list[str]] = {}
    for line_num, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            log.warning("Skipping malformed config line %d: missing colon", line_num)
            continue
        name, json_str = line.split(":", 1)
        name = name.strip()
        json_str = json_str.strip()
        if not name:
            log.warning("Skipping malformed config line %d: empty name", line_num)
            continue
        try:
            parsed = json.loads(json_str)
        except Exception as err:
            log.warning("Skipping malformed config line %d: invalid JSON (%s)", line_num, err)
            continue
        if not isinstance(parsed, list):
            log.warning("Skipping malformed config line %d: command_argv must be a JSON array", line_num)
            continue
        result[name] = [str(x) for x in parsed]
    return result


def load_action_config(filepath: Path | str, logger: logging.Logger | None = None) -> dict[str, list[str]]:
    path = Path(filepath)
    if not path.exists():
        return {}
    log = logger or LOGGER
    if path.stat().st_size > MAX_ACTION_CONFIG_SIZE_BYTES:
        log.warning("Action config file %s exceeds maximum allowed size of 1MB (%d bytes)", path, path.stat().st_size)
        return {}
    return parse_action_config(path.read_text(encoding="utf-8"), logger=logger)


def action_config(
    target: str | Path,
    logger: logging.Logger | None = None,
) -> dict[str, list[str]]:
    """Flexible loader: accept a path (existing file) or a raw config string."""
    if isinstance(target, Path):
        return load_action_config(target, logger=logger)
    path = Path(target)
    if path.exists():
        return load_action_config(path, logger=logger)
    return parse_action_config(target, logger=logger)


class WorkflowAction:
    """Run a configured shell command as argv list (no shell injection)."""

    def __init__(
        self,
        name: str,
        command_argv: list[str],
        config_dir: Path | str,
        logger: logging.Logger | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.name = name.removeprefix("workflow.")
        self.command_argv = list(command_argv)
        self.config_dir = Path(config_dir)
        self.logger = logger or LOGGER
        self.timeout = float(timeout)

    def __call__(
        self,
        tile_id: str = "",
        action: str = "",
        nonce: str = "",
        ts: float = 0.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            proc = subprocess.run(
                self.command_argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                shell=False,
            )
            status = {
                "action": f"workflow.{self.name}",
                "name": self.name,
                "command": self.command_argv,
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "updated_at": now_iso,
            }
        except subprocess.TimeoutExpired as err:
            self.logger.error("Workflow %s timed out after %fs", self.name, self.timeout)
            status = {
                "action": f"workflow.{self.name}",
                "name": self.name,
                "command": self.command_argv,
                "returncode": -1,
                "stdout": (err.stdout or "") if isinstance(err.stdout, str) else "",
                "stderr": (err.stderr or "") if isinstance(err.stderr, str) else "Timeout expired",
                "updated_at": now_iso,
            }
        except Exception as err:
            self.logger.exception("Workflow %s failed", self.name)
            status = {
                "action": f"workflow.{self.name}",
                "name": self.name,
                "command": self.command_argv,
                "returncode": -1,
                "stdout": "",
                "stderr": str(err),
                "updated_at": now_iso,
            }

        out_dir = self.config_dir / "actions"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"workflow.{self.name}.json"
        out_file.write_text(json.dumps(status, indent=2), encoding="utf-8")
        return status


class AlertDismissAction:
    """Persist alert dismissals to a JSON file."""

    def __init__(
        self,
        config_dir: Path | str,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config_dir = Path(config_dir)
        self.logger = logger or LOGGER

    def __call__(
        self,
        tile_id: str = "",
        action: str = "",
        nonce: str = "",
        ts: float = 0.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        alert_id = ""
        if action.startswith("alert.dismiss."):
            alert_id = action[len("alert.dismiss.") :]
        elif action.startswith("alert.dismiss:"):
            alert_id = action[len("alert.dismiss:") :]
        elif tile_id.startswith("alert:"):
            alert_id = tile_id[len("alert:") :]
        else:
            alert_id = tile_id or action or "default"

        out_file = self.config_dir / "dismissed_alerts.json"
        dismissed: dict[str, str] = {}
        if out_file.exists():
            try:
                data = json.loads(out_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    dismissed = data
            except Exception:
                pass

        now_iso = datetime.now(timezone.utc).isoformat()
        dismissed[alert_id] = now_iso
        self.config_dir.mkdir(parents=True, exist_ok=True)
        out_file.write_text(json.dumps(dismissed, indent=2), encoding="utf-8")
        return {alert_id: now_iso}


class ContextSetAction:
    """Persist the active dashboard context to a JSON file."""

    def __init__(
        self,
        config_dir: Path | str,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config_dir = Path(config_dir)
        self.logger = logger or LOGGER

    def __call__(
        self,
        tile_id: str = "",
        action: str = "",
        nonce: str = "",
        ts: float = 0.0,
        context: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        ctx_val = context
        if not ctx_val:
            if action.startswith("context.set."):
                ctx_val = action[len("context.set.") :]
            elif action.startswith("context.set:"):
                ctx_val = action[len("context.set:") :]
            elif tile_id.startswith("context:"):
                ctx_val = tile_id[len("context:") :]
            else:
                ctx_val = tile_id or action or "default"

        now_iso = datetime.now(timezone.utc).isoformat()
        payload = {"context": ctx_val, "updated_at": now_iso}
        self.config_dir.mkdir(parents=True, exist_ok=True)
        out_file = self.config_dir / "context.json"
        out_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload


class RefreshAction:
    """Force the panel cache to refresh. No-op when no cache is wired."""

    def __init__(
        self,
        cache: PanelCache | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.cache = cache
        self.logger = logger or LOGGER

    def __call__(
        self,
        tile_id: str = "",
        action: str = "",
        nonce: str = "",
        ts: float = 0.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self.cache is not None:
            try:
                self.cache.register("refresh")
            except ValueError:
                pass
            self.cache.record_success("refresh", {})
        return {"status": "ok", "action": "refresh"}


def register_all_actions(
    registry: ActionRegistry,
    config_dir: Path | str,
    logger: logging.Logger | None = None,
    cache: PanelCache | None = None,
) -> None:
    """Register all production handlers. YAML config workflows win over defaults."""
    cfg_dir = Path(config_dir)
    log = logger or LOGGER

    # 1. Pin the explicit fallback handlers first so the YAML config can override them.
    refresh_handler = RefreshAction(cache=cache, logger=log)
    registry.register("refresh", handler=refresh_handler)
    registry.register("workflow.refresh", handler=refresh_handler)

    # 2. YAML-configured workflows override the fallback for any name they declare.
    actions_yaml_path = cfg_dir / "actions.yaml"
    if actions_yaml_path.exists():
        commands = load_action_config(actions_yaml_path, logger=log)
        for name, argv in commands.items():
            short_name = name.removeprefix("workflow.")
            action_name = f"workflow.{short_name}"
            handler = WorkflowAction(
                name=short_name,
                command_argv=argv,
                config_dir=cfg_dir,
                logger=log,
            )
            registry.register(action_name, handler=handler)

    # 3. Alert/context handlers are always available (with prefix matching in the registry).
    alert_handler = AlertDismissAction(config_dir=cfg_dir, logger=log)
    registry.register("alert.dismiss", handler=alert_handler)

    context_handler = ContextSetAction(config_dir=cfg_dir, logger=log)
    registry.register("context.set", handler=context_handler)
