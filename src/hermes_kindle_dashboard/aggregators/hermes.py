from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..contract import PanelData
from ..state import DashboardSnapshot, HermesStateCollector

LOGGER = logging.getLogger("hermes-kindle-dashboard.aggregators.hermes")

DEFAULT_CONFIG_DIR = Path(
    os.environ.get("HERMES_DASHBOARD_ACTIONS_DIR", "~/.config/hermes-kindle-dashboard")
).expanduser()


def snapshot_to_panel(snapshot: DashboardSnapshot) -> PanelData:
    """Normalize a sanitized Hermes snapshot into the stable panel shape."""

    payload = snapshot.to_dict()
    payload.pop("generated_at", None)
    return payload


def _read_json(path: Path) -> dict:
    """Read a JSON file, returning {} on missing/invalid input."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        LOGGER.warning("could not read %s: %s", path, exc)
        return {}


def _load_user_state(config_dir: Path) -> dict:
    """Load user-controlled state (dismissed alerts, active context).

    The /control endpoint writes to these JSON files. Surface them in the
    panel so the dashboard reflects what the user actually dismissed or set,
    and so external clients (the Kindle interactive client, dashboards,
    scripts) can react.
    """
    dismissed = _read_json(config_dir / "dismissed_alerts.json")
    context = _read_json(config_dir / "context.json")
    return {
        "dismissed_alerts": dismissed,
        "active_context": context.get("context", ""),
        "context_updated_at": context.get("updated_at", ""),
    }


@dataclass
class HermesAggregator:
    """Collect Hermes state locally without blocking the event loop."""

    collector: HermesStateCollector
    interval_seconds: float = 15.0
    timeout_seconds: float = 10.0
    config_dir: Path = field(default_factory=lambda: DEFAULT_CONFIG_DIR)
    name: str = field(default="hermes", init=False)

    async def collect(self) -> PanelData:
        snapshot = await asyncio.to_thread(self.collector.collect)
        panel = snapshot_to_panel(snapshot)
        panel["user_state"] = await asyncio.to_thread(_load_user_state, self.config_dir)
        return panel
