"""Prompt-response panel: closing the loop on Ask tiles.

Tapping ``hermes.prompt.<name>`` writes the agent's answer to
``<config_dir>/actions/hermes.prompt.<name>.json``; these tests cover the
sanitizer, the panel builder/aggregator, layout integration, rendering at
both supported sizes, and the end-to-end API path.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_kindle_dashboard.aggregators.prompt_response import (
    PromptResponseAggregator,
    build_prompt_response_panel,
    sanitize_excerpt,
)
from hermes_kindle_dashboard.api import ApiSettings, create_app
from hermes_kindle_dashboard.contract import PanelCache, build_default_layout
from hermes_kindle_dashboard.render import RenderOptions, render_layout_dashboard


def _write_result(config_dir: Path, name: str, payload: dict) -> Path:
    actions = config_dir / "actions"
    actions.mkdir(parents=True, exist_ok=True)
    path = actions / f"hermes.prompt.{name}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _ok_payload(name: str, stdout: str, updated_at: str) -> dict:
    return {
        "kind": "prompt",
        "status": "ok",
        "prompt": name,
        "returncode": 0,
        "stdout": stdout,
        "stderr": "",
        "updated_at": updated_at,
    }


# --------------------------------------------------------------------- sanitizer


def test_sanitize_strips_ansi_and_control_characters() -> None:
    raw = "\x1b[32mAnswer\x1b[0m:\r\n line\ttwo\x07 with   gaps\n"

    cleaned = sanitize_excerpt(raw)

    assert cleaned == "Answer: line two with gaps"


def test_sanitize_caps_length_with_ellipsis() -> None:
    cleaned = sanitize_excerpt("x" * 5000, limit=100)

    assert len(cleaned) == 100
    assert cleaned.endswith("…")


def test_sanitize_rejects_non_string_input() -> None:
    assert sanitize_excerpt(None) == ""  # type: ignore[arg-type]


# ------------------------------------------------------------------ panel builder


def test_panel_without_results_reports_unavailable(tmp_path: Path) -> None:
    assert build_prompt_response_panel(tmp_path) == {"available": False}


def test_panel_surfaces_latest_ok_result(tmp_path: Path) -> None:
    _write_result(
        tmp_path,
        "summarize_inbox",
        _ok_payload(
            "summarize_inbox",
            "You have 3 unread threads.\nAll low priority.",
            "2026-07-24T19:00:00+00:00",
        ),
    )

    panel = build_prompt_response_panel(tmp_path)

    assert panel["available"] is True
    assert panel["prompt"] == "summarize_inbox"
    assert panel["status"] == "ok"
    assert panel["returncode"] == 0
    assert panel["response_excerpt"] == "You have 3 unread threads. All low priority."
    assert panel["updated_at"] == "2026-07-24T19:00:00+00:00"
    assert panel["recent"][0]["prompt"] == "summarize_inbox"


def test_panel_picks_newest_and_keeps_capped_history(tmp_path: Path) -> None:
    for index in range(7):
        _write_result(
            tmp_path,
            f"job{index}",
            _ok_payload(f"job{index}", f"answer {index}", f"2026-07-24T19:0{index}:00+00:00"),
        )

    panel = build_prompt_response_panel(tmp_path)

    assert panel["prompt"] == "job6"
    recent_names = [entry["prompt"] for entry in panel["recent"]]
    assert recent_names[0] == "job6"
    assert len(recent_names) == 5  # MAX_HISTORY cap
    # History carries no text bodies.
    assert all("answer" not in json.dumps(entry) for entry in panel["recent"])


def test_panel_exposes_sanitized_error_excerpt(tmp_path: Path) -> None:
    _write_result(
        tmp_path,
        "briefing",
        {
            "kind": "prompt",
            "status": "error",
            "prompt": "briefing",
            "returncode": -1,
            "stdout": "",
            "stderr": "timeout after \x1b[31m120s\x1b[0m",
            "updated_at": "2026-07-24T19:05:00+00:00",
        },
    )

    panel = build_prompt_response_panel(tmp_path)

    assert panel["status"] == "error"
    assert panel["response_excerpt"] == ""
    assert panel["error_excerpt"] == "timeout after 120s"


def test_panel_skips_malformed_files(tmp_path: Path) -> None:
    actions = tmp_path / "actions"
    actions.mkdir(parents=True)
    (actions / "hermes.prompt.broken.json").write_text("{not json", encoding="utf-8")
    (actions / "hermes.prompt.listy.json").write_text("[1, 2]", encoding="utf-8")
    _write_result(tmp_path, "good", _ok_payload("good", "fine", "2026-07-24T19:00:00+00:00"))

    panel = build_prompt_response_panel(tmp_path)

    assert panel["available"] is True
    assert panel["prompt"] == "good"


def test_aggregator_collects_off_the_event_loop(tmp_path: Path) -> None:
    aggregator = PromptResponseAggregator(config_dir=tmp_path, interval_seconds=5.0)

    assert aggregator.name == "prompt_response"

    import asyncio

    panel = asyncio.run(aggregator.collect())

    assert panel == {"available": False}


# ---------------------------------------------------------------------- layout


def test_default_layout_gives_prompt_response_its_own_strip() -> None:
    layout = build_default_layout(1072, 1448, panels=("hermes", "prompt_response"))
    by_id = {tile["id"]: tile for tile in layout["tiles"]}

    assert by_id["panel:hermes"]["row"] == 4
    assert by_id["panel:hermes"]["h"] == 1
    assert by_id["panel:prompt_response"]["row"] == 5
    assert by_id["panel:prompt_response"]["h"] == 1
    assert by_id["panel:prompt_response"]["w"] == 4
    assert by_id["panel:prompt_response"]["panel"] == "prompt_response"


def test_default_layout_keeps_legacy_shape_without_prompt_panel() -> None:
    layout = build_default_layout(600, 800)
    ids = {tile["id"] for tile in layout["tiles"]}

    assert "panel:prompt_response" not in ids
    hermes = next(tile for tile in layout["tiles"] if tile["id"] == "panel:hermes")
    assert hermes["h"] == 2


# --------------------------------------------------------------------- rendering


@pytest.mark.parametrize("size", [(600, 800), (1072, 1448)])
def test_layout_render_paints_prompt_response_panel(tmp_path: Path, size: tuple[int, int]) -> None:
    width, height = size
    _write_result(
        tmp_path,
        "summarize_inbox",
        _ok_payload("summarize_inbox", "Three unread threads, all low priority.", "2026-07-24T19:00:00+00:00"),
    )
    cache = PanelCache()
    cache.register("hermes")
    cache.register("prompt_response")
    cache.record_success(
        "hermes",
        {
            "session": {"model": "test-model", "status": "working", "title": "t"},
            "tasks": [],
            "kanban_active": 0,
            "memory": {},
            "recent_events": [],
        },
    )
    cache.record_success("prompt_response", build_prompt_response_panel(tmp_path))

    image = render_layout_dashboard(
        build_default_layout(width, height, panels=("hermes", "prompt_response")),
        cache.snapshot(),
        RenderOptions(width=width, height=height),
    )

    assert image.size == (width, height)
    assert image.mode == "1"


def test_layout_render_handles_error_and_empty_prompt_panels(tmp_path: Path) -> None:
    _write_result(
        tmp_path,
        "briefing",
        {
            "kind": "prompt",
            "status": "error",
            "prompt": "briefing",
            "returncode": -1,
            "stdout": "",
            "stderr": "timeout",
            "updated_at": "2026-07-24T19:05:00+00:00",
        },
    )
    cache = PanelCache()
    cache.register("hermes")
    cache.register("prompt_response")
    cache.record_success(
        "hermes",
        {
            "session": {"model": "m", "status": "idle"},
            "tasks": [],
            "memory": {},
            "recent_events": [],
        },
    )
    cache.record_success("prompt_response", build_prompt_response_panel(tmp_path))
    empty_cache = PanelCache()
    empty_cache.register("hermes")
    empty_cache.register("prompt_response")

    error_image = render_layout_dashboard(
        build_default_layout(600, 800, panels=("hermes", "prompt_response")),
        cache.snapshot(),
        RenderOptions(width=600, height=800),
    )
    empty_image = render_layout_dashboard(
        build_default_layout(600, 800, panels=("hermes", "prompt_response")),
        empty_cache.snapshot(),
        RenderOptions(width=600, height=800),
    )

    assert error_image.mode == "1"
    assert empty_image.mode == "1"


# ------------------------------------------------------------------ API surface


def test_dashboard_data_shows_prompt_response_after_initial_collect(tmp_path: Path) -> None:
    _write_result(
        tmp_path,
        "summarize_inbox",
        _ok_payload("summarize_inbox", "Two threads need you today.", "2026-07-24T19:00:00+00:00"),
    )
    app = create_app(
        ApiSettings(token="read-token"),
        [PromptResponseAggregator(config_dir=tmp_path, interval_seconds=5.0)],
    )
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer read-token"}
        payload = client.get("/dashboard-data", headers=headers).json()

    panel = payload["panels"]["prompt_response"]
    assert panel["_meta"]["status"] == "ok"
    assert panel["available"] is True
    assert panel["response_excerpt"] == "Two threads need you today."


class _AnsweringTransport:
    """Minimal HermesTransport returning a canned agent answer."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls = 0

    def send_prompt(self, text: str, model: str | None = None) -> dict:
        self.calls += 1
        return {"returncode": 0, "stdout": self.answer, "stderr": ""}


def test_ask_tile_dispatch_lands_on_the_device_panel(tmp_path: Path) -> None:
    from hermes_kindle_dashboard.actions import ActionRegistry
    from hermes_kindle_dashboard.hermes_controls import (
        HermesControlsConfig,
        register_hermes_controls,
    )
    from hermes_kindle_dashboard.scheduler import collect_once

    transport = _AnsweringTransport("Standup notes are ready.")
    config = HermesControlsConfig(quick_prompts={"standup": "Draft a standup update."})
    registry = ActionRegistry(rate_limit_seconds=0.0)
    register_hermes_controls(registry, tmp_path, config, transport)

    aggregator = PromptResponseAggregator(config_dir=tmp_path, interval_seconds=5.0)
    app = create_app(
        ApiSettings(token="read-token", control_token="control-token"),
        [aggregator],
        registry=registry,
    )
    with TestClient(app) as client:
        resp = client.post(
            "/control",
            headers={"Authorization": "Bearer control-token"},
            json={"action": "hermes.prompt.standup", "tile_id": "t1", "nonce": "n1", "ts": time.time()},
        )
        assert resp.status_code == 200

        # The handler runs in the registry's thread pool; wait, then refresh.
        import asyncio

        registry.wait_for_pending()
        asyncio.run(collect_once(aggregator, client.app.state.panel_cache))

        payload = client.get("/dashboard-data", headers={"Authorization": "Bearer read-token"}).json()

    assert transport.calls == 1
    panel = payload["panels"]["prompt_response"]
    assert panel["available"] is True
    assert panel["prompt"] == "standup"
    assert panel["status"] == "ok"
    assert panel["response_excerpt"] == "Standup notes are ready."
