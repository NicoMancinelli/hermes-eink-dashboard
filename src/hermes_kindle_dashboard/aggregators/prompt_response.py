"""Prompt-response panel: surface Hermes quick-prompt answers on devices.

Pressing an ``Ask: <name>`` tile runs a host-side quick prompt and
``HermesPromptAction`` persists the full result to
``<config_dir>/actions/hermes.prompt.<name>.json``. Until now that answer
was invisible on the dashboard itself. This aggregator closes the loop: it
reads those result files (they live only on the trusted host), sanitizes
the agent's textual reply, and publishes it as an independent panel so
renderers can show what Hermes said.

Sanitization follows the project's privacy rules:

* control characters, ANSI escapes, and carriage returns are stripped;
* whitespace runs collapse to single spaces (E-Ink friendly wrapping);
* excerpts are hard-capped, so a chatty answer can never blow up a render;
* history entries carry names/status/timestamps only, never text.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..contract import PanelData

LOGGER = logging.getLogger("hermes-kindle-dashboard.aggregators.prompt_response")

DEFAULT_CONFIG_DIR = Path(
    os.environ.get("HERMES_DASHBOARD_ACTIONS_DIR", "~/.config/hermes-kindle-dashboard")
).expanduser()

RESULT_GLOB = "hermes.prompt.*.json"
MAX_EXCERPT_CHARS = 1200
MAX_ERROR_CHARS = 400
MAX_HISTORY = 5

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_excerpt(text: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    """Make agent output safe to draw: no escapes/control chars, capped length."""
    if not isinstance(text, str):
        return ""
    cleaned = _ANSI_RE.sub("", text)
    cleaned = _CONTROL_RE.sub(" ", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


def _parse_timestamp(value: str) -> datetime:
    """Parse an ISO timestamp for ordering; unparseable values sort oldest."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_result(path: Path) -> dict | None:
    """Read one result file; malformed or non-dict payloads are skipped."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOGGER.warning("skipping unreadable prompt result %s: %s", path.name, exc)
        return None
    return payload if isinstance(payload, dict) else None


def build_prompt_response_panel(config_dir: Path) -> PanelData:
    """Build the device-neutral prompt-response panel from result files."""
    actions_dir = Path(config_dir) / "actions"
    results: list[tuple[datetime, str, dict]] = []
    try:
        candidates = sorted(actions_dir.glob(RESULT_GLOB))
    except OSError as exc:
        LOGGER.warning("could not scan %s: %s", actions_dir, exc)
        candidates = []
    for path in candidates:
        payload = _read_result(path)
        if payload is None:
            continue
        results.append((_parse_timestamp(payload.get("updated_at", "")), path.name, payload))

    if not results:
        return {"available": False}

    results.sort(key=lambda item: item[0], reverse=True)
    _, _, latest = results[0]
    status = "ok" if latest.get("status") == "ok" else "error"
    response_text = latest.get("stdout") if status == "ok" else ""
    error_text = latest.get("stderr") if status == "error" else ""

    history = []
    for when, _, payload in results[:MAX_HISTORY]:
        history.append(
            {
                "prompt": str(payload.get("prompt", "")),
                "status": "ok" if payload.get("status") == "ok" else "error",
                "updated_at": str(payload.get("updated_at", "") or ""),
            }
        )

    return {
        "available": True,
        "prompt": str(latest.get("prompt", "")),
        "status": status,
        "returncode": latest.get("returncode"),
        "response_excerpt": sanitize_excerpt(response_text),
        "error_excerpt": sanitize_excerpt(error_text, limit=MAX_ERROR_CHARS),
        "updated_at": str(latest.get("updated_at", "") or ""),
        "recent": history,
    }


@dataclass
class PromptResponseAggregator:
    """Independently refresh the last Hermes quick-prompt response."""

    config_dir: Path = field(default_factory=lambda: DEFAULT_CONFIG_DIR)
    interval_seconds: float = 5.0
    timeout_seconds: float = 10.0
    name: str = field(default="prompt_response", init=False)

    async def collect(self) -> PanelData:
        # File I/O stays off the event loop, matching the other aggregators.
        return await asyncio.to_thread(build_prompt_response_panel, Path(self.config_dir))
