"""Action completion events: the host tells devices when an action finished.

Tapping an "Ask Hermes" tile dispatches a host-side quick prompt. The reply
only shows up on the device once the panel has been refreshed, so the host
publishes a completion event *after* re-collecting the panels. The Kindle
client uses that event to repaint immediately instead of waiting for the
next periodic refresh.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient

from hermes_eink_dashboard.actions import ActionRegistry
from hermes_eink_dashboard.api import ApiSettings, create_app
from hermes_eink_dashboard.contract import PanelCache, build_default_layout
from hermes_eink_dashboard.scheduler import ControlBus


class _CountingAggregator:
    """Aggregator that records how often it was collected."""

    name = "hermes"
    interval_seconds = 60.0

    def __init__(self) -> None:
        self.collects = 0

    async def collect(self) -> dict[str, Any]:
        self.collects += 1
        return {"collects": self.collects}


def _client(registry: ActionRegistry, aggregator: _CountingAggregator) -> TestClient:
    app = create_app(
        settings=ApiSettings(token="read-token", control_token="control-token"),
        aggregators=[aggregator],
        cache=PanelCache(),
        bus=ControlBus(),
        registry=registry,
        layout=build_default_layout(1072, 1448, panels=("hermes",)),
    )
    return TestClient(app)


def _dispatch(client: TestClient, action: str = "test.action") -> None:
    response = client.post(
        "/control",
        json={"tile_id": "tile-1", "action": action, "nonce": "n1", "ts": int(time.time())},
        headers={"Authorization": "Bearer control-token"},
    )
    assert response.status_code == 200


def _next_event(client: TestClient, timeout: float = 5.0) -> dict[str, Any] | None:
    response = client.get(
        f"/control/events?timeout={timeout}",
        headers={"Authorization": "Bearer control-token"},
    )
    assert response.status_code == 200
    return response.json()["event"]


def test_completion_event_is_published_after_the_action_finishes() -> None:
    aggregator = _CountingAggregator()
    registry = ActionRegistry()
    finished: list[dict[str, Any]] = []

    def handler(tile_id: str = "", action: str = "", nonce: str = "", ts: float = 0.0) -> None:
        finished.append({"tile_id": tile_id, "action": action})

    registry.register("test.action", handler)
    client = _client(registry, aggregator)

    with client:
        _dispatch(client)
        ack = _next_event(client)
        assert ack is not None
        assert ack["phase"] == "ack"
        assert ack["action"] == "test.action"

        done = _next_event(client)
        assert done is not None
        assert done["phase"] == "done"
        assert done["action"] == "test.action"
        assert done["tile_id"] == "tile-1"
        assert done["status"] == "ok"
        # The handler really ran before the completion was published.
        assert finished


def test_panels_are_refreshed_before_the_completion_event() -> None:
    aggregator = _CountingAggregator()
    registry = ActionRegistry()
    registry.register("test.action", lambda **_: None)
    client = _client(registry, aggregator)

    with client:
        # The lifespan performs the initial collection.
        assert aggregator.collects >= 1
        before = aggregator.collects
        _dispatch(client)
        _next_event(client)  # ack
        done = _next_event(client)
        assert done is not None and done["phase"] == "done"
        # The panels were re-collected before the completion was published, so
        # the device renders the result of the action right away.
        assert aggregator.collects > before


def test_failed_action_reports_an_error_status() -> None:
    aggregator = _CountingAggregator()
    registry = ActionRegistry()

    def handler(tile_id: str = "", action: str = "", nonce: str = "", ts: float = 0.0) -> None:
        raise RuntimeError("boom")

    registry.register("test.action", handler)
    client = _client(registry, aggregator)

    with client:
        _dispatch(client)
        _next_event(client)  # ack
        done = _next_event(client)
        assert done is not None
        assert done["phase"] == "done"
        assert done["status"] == "error"
        # No internals leak into the event.
        assert "boom" not in str(done)
        assert done["error_code"] == "handler_failed"


def test_completion_event_requires_the_control_token() -> None:
    aggregator = _CountingAggregator()
    registry = ActionRegistry()
    registry.register("test.action", lambda **_: None)
    client = _client(registry, aggregator)

    with client:
        response = client.get("/control/events?timeout=0.1")
        assert response.status_code == 401
