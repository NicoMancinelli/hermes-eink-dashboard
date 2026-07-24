"""Tests for the production action handlers (Phase 4)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from hermes_kindle_dashboard.actions import ActionRegistry, UnknownActionError
from hermes_kindle_dashboard.actions_runtime import (
    AlertDismissAction,
    ContextSetAction,
    RefreshAction,
    WorkflowAction,
    action_config,
    parse_action_config,
    register_all_actions,
)
from hermes_kindle_dashboard.contract import PanelCache


def test_workflow_runs_safe_argv(tmp_path: Path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    action = WorkflowAction(
        name="test_wf",
        command_argv=[
            "python3",
            "-c",
            "import sys; sys.stdout.write('hello stdout'); sys.stderr.write('hello stderr')",
        ],
        config_dir=config_dir,
    )
    result = action(tile_id="wf:test", action="workflow.test_wf")
    assert result["returncode"] == 0
    assert result["stdout"] == "hello stdout"
    assert result["stderr"] == "hello stderr"

    status_file = config_dir / "actions" / "workflow.test_wf.json"
    assert status_file.exists()
    status_data = json.loads(status_file.read_text(encoding="utf-8"))
    assert status_data["action"] == "workflow.test_wf"
    assert status_data["returncode"] == 0
    assert status_data["stdout"] == "hello stdout"
    assert "updated_at" in status_data


def test_workflow_rejects_not_allowlisted_command(tmp_path: Path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    actions_yaml = config_dir / "actions.yaml"
    actions_yaml.write_text('allowed_cmd: ["echo", "yes"]\n', encoding="utf-8")

    registry = ActionRegistry()
    register_all_actions(registry, config_dir)

    res = registry.dispatch(action="workflow.allowed_cmd", ts=1000.0, now=1000.0)
    assert res["action"] == "workflow.allowed_cmd"

    with pytest.raises(UnknownActionError):
        registry.dispatch(action="workflow.forbidden_cmd", ts=1000.0, now=1000.0)


def test_alert_dismiss_persists(tmp_path: Path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    action_handler = AlertDismissAction(config_dir=config_dir)

    action_handler(action="alert.dismiss.stale-orders")
    dismissed_file = config_dir / "dismissed_alerts.json"
    assert dismissed_file.exists()
    data = json.loads(dismissed_file.read_text(encoding="utf-8"))
    assert "stale-orders" in data

    action_handler(action="alert.dismiss.low-battery")
    data2 = json.loads(dismissed_file.read_text(encoding="utf-8"))
    assert "stale-orders" in data2
    assert "low-battery" in data2


def test_context_set_persists(tmp_path: Path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    action_handler = ContextSetAction(config_dir=config_dir)

    res = action_handler(action="context.set.work")
    assert res["context"] == "work"
    context_file = config_dir / "context.json"
    assert context_file.exists()
    data = json.loads(context_file.read_text(encoding="utf-8"))
    assert data["context"] == "work"
    assert "updated_at" in data


def test_refresh_action_is_a_no_op(tmp_path: Path):
    action_handler = RefreshAction()
    res = action_handler()
    assert res["status"] == "ok"

    cache = PanelCache()
    action_handler_cache = RefreshAction(cache=cache)
    action_handler_cache()
    snapshot = cache.snapshot()
    assert "refresh" in snapshot["panels"]


def test_malformed_config_line_skipped_with_warning(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING)
    content = """# Comment line
valid_cmd: ["echo", "ok"]
line_without_colon
bad_json: [invalid json
: ["echo", "no name"]
"""
    result = parse_action_config(content)
    assert result == {"valid_cmd": ["echo", "ok"]}
    assert "Skipping malformed config line" in caplog.text


def test_yaml_config_overrides_default_refresh_handler(tmp_path: Path):
    """The YAML config workflow handler must win over the default refresh handler.

    Regression: previously the explicit `workflow.refresh` registration ran AFTER
    the YAML config loop, so a YAML-declared `refresh:` would silently overwrite
    the user's command with the no-op RefreshAction.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    actions_yaml = config_dir / "actions.yaml"
    actions_yaml.write_text(
        "refresh: [\"python3\", \"-c\", \"import sys; sys.stdout.write('overridden')\"]\n",
    )

    registry = ActionRegistry()
    register_all_actions(registry, config_dir)

    # Dispatch the workflow and verify the YAML command ran (not the no-op).
    registry.dispatch(action="workflow.refresh", ts=1000.0, now=1000.0)
    status_file = config_dir / "actions" / "workflow.refresh.json"
    assert status_file.exists()
    data = json.loads(status_file.read_text(encoding="utf-8"))
    assert data["returncode"] == 0
    assert data["stdout"] == "overridden"


def test_default_refresh_handler_used_when_no_yaml_entry(tmp_path: Path):
    """When no YAML config exists, the default no-op refresh handler is registered."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    registry = ActionRegistry()
    register_all_actions(registry, config_dir)
    res = registry.dispatch(action="workflow.refresh", ts=1000.0, now=1000.0)
    assert res["action"] == "workflow.refresh"
