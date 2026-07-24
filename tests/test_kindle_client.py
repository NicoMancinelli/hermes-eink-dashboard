"""Harness tests for the Kindle interactive client.

These tests run on pdi (or any Linux machine) without a real Kindle.
They spin up a real FastAPI server in a thread on a random localhost port,
configure a `MockSource` to drive the client, and verify the client's
end-to-end behavior:

- Reads /dashboard.json on startup.
- Sends GET /dashboard.png with focus_tile_id on focus changes.
- Sends POST /control with valid payload on activate.
- Refuses to dispatch when the focused tile has no action.
- Cooperatively shuts down.

Hardware-specific input (FiveWaySource) is only tested for protocol
correctness, not for live device behavior.
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import uvicorn

from hermes_kindle_dashboard.actions import ActionRegistry
from hermes_kindle_dashboard.api import ApiSettings, create_app
from hermes_kindle_dashboard.contract import PanelCache, build_default_layout
from hermes_kindle_dashboard.scheduler import ControlBus

from kindle.client.interactive import (
    BusClient,
    DashboardClient,
    FiveWaySource,
    InputEvent,
    Layout,
    MockSource,
    Tile,
)


class _EmptyAggregator:
    """A no-op aggregator that registers a panel but never refreshes it."""

    name = "hermes"
    interval_seconds = 60.0

    async def collect(self):  # noqa: D401
        return {}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_server(app, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if server.started:
            return server, thread
        time.sleep(0.05)
    return server, thread


def _wait_ready(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"server did not become ready on port {port}")


@pytest.fixture
def fake_host():
    """Spin up a real FastAPI server with a known token and a default layout."""
    port = _free_port()
    bus = ControlBus()
    registry = ActionRegistry()
    registry.register("workflow.refresh")
    registry.register("alert.dismiss.test")
    registry.register("context.set")
    cache = PanelCache()
    layout = build_default_layout(1072, 1448, panels=("hermes",))
    app = create_app(
        settings=ApiSettings(token="read-token", control_token="control-token"),
        aggregators=[_EmptyAggregator()],
        cache=cache,
        bus=bus,
        registry=registry,
        layout=layout,
    )
    server, thread = _start_server(app, port)
    _wait_ready(port)
    yield {"port": port, "registry": registry, "bus": bus, "cache": cache}
    server.should_exit = True
    thread.join(timeout=5.0)


def _tmp_image(tmp_path: Path) -> str:
    return str(tmp_path / "dashboard.png")


def test_layout_neighbor_picks_closest_adjacent_tile() -> None:
    layout_data = {
        "schema_version": 2,
        "layout": {"columns": 4, "rows": 6, "tile_size": [240, 160], "grid_size": [1072, 1448]},
        "tiles": [
            {"id": "a", "label": "A", "col": 0, "row": 0, "w": 2, "h": 1, "kind": "action", "action": None},
            {"id": "b", "label": "B", "col": 2, "row": 0, "w": 2, "h": 1, "kind": "action", "action": None},
            {"id": "c", "label": "C", "col": 0, "row": 1, "w": 2, "h": 1, "kind": "action", "action": None},
            {"id": "d", "label": "D", "col": 2, "row": 1, "w": 2, "h": 1, "kind": "action", "action": None},
        ],
        "focus": {"tile_id": "a", "x": 0, "y": 0},
    }
    layout = Layout.from_dict(layout_data)
    assert layout.neighbor("a", "right") == "b"
    assert layout.neighbor("a", "down") == "c"
    assert layout.neighbor("b", "down") == "d"
    assert layout.neighbor("c", "right") == "d"
    assert layout.neighbor("a", "left") is None


def test_client_dispatches_control_on_activate(fake_host, tmp_path) -> None:
    image_path = _tmp_image(tmp_path)
    bus = BusClient(
        base_url=f"http://127.0.0.1:{fake_host['port']}",
        read_token="read-token",
        control_token="control-token",
    )
    source = MockSource([InputEvent(kind="activate")])
    client = DashboardClient(
        bus=bus,
        source=source,
        image_path=image_path,
        refresh_seconds=0.5,
        event_poll_seconds=0.05,
    )

    # Run the client loop in a thread; stop after one event is consumed.
    client_thread = threading.Thread(target=client.run, daemon=True)
    client_thread.start()

    # Wait for the bus event to arrive.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        with fake_host["bus"]._lock:
            if fake_host["bus"]._events:
                break
        time.sleep(0.05)
    client.stop()
    client_thread.join(timeout=2.0)

    with fake_host["bus"]._lock:
        assert fake_host["bus"]._events, "expected at least one bus event after activate"
        payload = fake_host["bus"]._events[-1]
    assert payload["action"] in {"workflow.refresh", "alert.dismiss.test", "context.set"}
    assert Path(image_path).exists()
    assert Path(image_path).stat().st_size > 0


def test_client_does_not_dispatch_when_tile_has_no_action(fake_host, tmp_path) -> None:
    image_path = _tmp_image(tmp_path)
    bus = BusClient(
        base_url=f"http://127.0.0.1:{fake_host['port']}",
        read_token="read-token",
        control_token="control-token",
    )
    layout = Layout.from_dict(
        {
            "schema_version": 2,
            "layout": {"columns": 1, "rows": 1, "tile_size": [240, 160], "grid_size": [240, 160]},
            "tiles": [
                {
                    "id": "p",
                    "label": "P",
                    "col": 0,
                    "row": 0,
                    "w": 1,
                    "h": 1,
                    "kind": "panel",
                    "panel": "hermes",
                }
            ],
            "focus": {"tile_id": "p", "x": 0, "y": 0},
        }
    )
    source = MockSource([])
    client = DashboardClient(
        bus=bus,
        source=source,
        image_path=image_path,
        refresh_seconds=0.5,
        event_poll_seconds=0.05,
    )
    client._layout = layout
    client._focus_tile_id = "p"
    client._dispatch()
    with fake_host["bus"]._lock:
        assert fake_host["bus"]._events == []


def test_client_fetches_layout_and_png_on_start(fake_host, tmp_path) -> None:
    image_path = _tmp_image(tmp_path)
    bus = BusClient(
        base_url=f"http://127.0.0.1:{fake_host['port']}",
        read_token="read-token",
        control_token="control-token",
    )
    source = MockSource([])
    client = DashboardClient(
        bus=bus,
        source=source,
        image_path=image_path,
        refresh_seconds=0.5,
        event_poll_seconds=0.05,
    )
    client._refresh_layout()
    client._refresh_png()
    assert client._layout is not None
    assert client._focus_tile_id
    assert Path(image_path).exists()
    assert Path(image_path).stat().st_size > 0


def test_five_way_source_maps_known_keycodes() -> None:
    assert FiveWaySource.KEY_UP == 103
    assert FiveWaySource.KEY_DOWN == 108
    assert FiveWaySource.KEY_LEFT == 105
    assert FiveWaySource.KEY_RIGHT == 106
    assert FiveWaySource.KEY_SELECT == 28
    assert FiveWaySource._KEY_TO_DIRECTION[103] == "up"
    assert FiveWaySource._KEY_TO_DIRECTION[108] == "down"
    assert FiveWaySource._KEY_TO_DIRECTION[105] == "left"
    assert FiveWaySource._KEY_TO_DIRECTION[106] == "right"


def test_five_way_source_struct_size_is_input_event_size() -> None:
    """Linux input_event is 24 bytes on 64-bit. We must read exactly that."""
    assert FiveWaySource._EVENT_STRUCT.size == 24


def test_tile_from_dict_minimal() -> None:
    tile = Tile.from_dict({"id": "x", "label": "X", "col": 0, "row": 0})
    assert tile.id == "x"
    assert tile.w == 1
    assert tile.h == 1
    assert tile.kind == "action"
    assert tile.action is None


def test_layout_from_dict_falls_back_to_first_tile_focus() -> None:
    layout = Layout.from_dict(
        {
            "schema_version": 2,
            "layout": {"columns": 1, "rows": 1, "tile_size": [240, 160], "grid_size": [240, 160]},
            "tiles": [{"id": "first", "label": "F", "col": 0, "row": 0, "w": 1, "h": 1}],
            "focus": {},
        }
    )
    assert layout.focus_tile_id == "first"


def test_input_event_frozen_dataclass() -> None:
    event = InputEvent(kind="focus", direction="up")
    with pytest.raises(FrozenInstanceError):
        event.direction = "down"  # type: ignore[misc]


def test_focus_changes_via_neighbor(fake_host, tmp_path) -> None:
    image_path = _tmp_image(tmp_path)
    bus = BusClient(
        base_url=f"http://127.0.0.1:{fake_host['port']}",
        read_token="read-token",
        control_token="control-token",
    )
    layout = Layout.from_dict(
        {
            "schema_version": 2,
            "layout": {"columns": 4, "rows": 6, "tile_size": [240, 160], "grid_size": [1072, 1448]},
            "tiles": [
                {"id": "a", "label": "A", "col": 0, "row": 0, "w": 2, "h": 1, "kind": "action", "action": None},
                {"id": "b", "label": "B", "col": 2, "row": 0, "w": 2, "h": 1, "kind": "action", "action": None},
            ],
            "focus": {"tile_id": "a", "x": 0, "y": 0},
        }
    )
    source = MockSource([InputEvent(kind="focus", direction="right")])
    client = DashboardClient(
        bus=bus,
        source=source,
        image_path=image_path,
        refresh_seconds=0.5,
        event_poll_seconds=0.05,
    )
    client._layout = layout
    client._focus_tile_id = "a"
    client._focus_neighbor("right")
    assert client._focus_tile_id == "b"
