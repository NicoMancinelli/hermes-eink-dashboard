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

from hermes_eink_dashboard.actions import ActionRegistry
from hermes_eink_dashboard.api import ApiSettings, create_app
from hermes_eink_dashboard.contract import PanelCache, build_default_layout
from hermes_eink_dashboard.scheduler import ControlBus

from kindle.client.interactive import (
    BusClient,
    CombinedSource,
    DashboardClient,
    FiveWaySource,
    InputEvent,
    Layout,
    MockSource,
    TapSource,
    Tile,
    TouchSource,
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



# ===========================================================================
# Touch support tests
# ===========================================================================

def test_layout_tile_at_returns_correct_tile() -> None:
    """Layout.tile_at maps (x, y) to the tile under that coordinate."""
    layout = Layout.from_dict(
        {
            "schema_version": 2,
            "layout": {"columns": 4, "rows": 6, "tile_size": [240, 160], "grid_size": [1072, 1448]},
            "tiles": [
                {"id": "top-left", "label": "TL", "col": 0, "row": 0, "w": 2, "h": 1, "kind": "action", "action": None},
                {"id": "top-right", "label": "TR", "col": 2, "row": 0, "w": 2, "h": 1, "kind": "action", "action": None},
                {"id": "bottom", "label": "B", "col": 0, "row": 4, "w": 4, "h": 2, "kind": "action", "action": None},
            ],
            "focus": {"tile_id": "top-left", "x": 0, "y": 0},
        }
    )
    # Inside the top-left tile (0..480, 0..160)
    assert layout.tile_at(120, 80) == "top-left"
    # Inside the top-right tile (480..960, 0..160)
    assert layout.tile_at(700, 80) == "top-right"
    # Inside the bottom tile (0..960, 640..960)
    assert layout.tile_at(500, 700) == "bottom"
    # Outside any tile
    assert layout.tile_at(50, 1200) is None
    assert layout.tile_at(-1, 80) is None


def test_layout_tile_at_first_match_wins() -> None:
    """When tiles overlap, the first tile in the layout's tile list wins."""
    layout = Layout.from_dict(
        {
            "schema_version": 2,
            "layout": {"columns": 4, "rows": 6, "tile_size": [240, 160], "grid_size": [1072, 1448]},
            "tiles": [
                {"id": "first", "label": "First", "col": 0, "row": 0, "w": 4, "h": 4, "kind": "action", "action": None},
                {"id": "second", "label": "Second", "col": 1, "row": 1, "w": 2, "h": 2, "kind": "action", "action": None},
            ],
            "focus": {"tile_id": "first", "x": 0, "y": 0},
        }
    )
    # Coordinate inside the overlap zone
    assert layout.tile_at(400, 250) == "first"
    # Coordinate inside the second tile only (top-right of second tile)
    assert layout.tile_at(700, 100) == "first"


def test_touch_source_emits_raw_coordinate_event(tmp_path) -> None:
    """TouchSource translates a synthetic EV_ABS + BTN_TOUCH sequence
    into a focus event tagged with __raw__:x:y."""
    events_path = tmp_path / "events.bin"
    # Synthesize a press at (500, 300). Layout matches: x=500, y=300.
    # Single-touch protocol A sends ABS_X then ABS_Y then BTN_TOUCH=1.
    import struct as _struct
    payload = b""
    payload += _struct.pack("llHHi", 1000, 0, 0x03, 0x00, 500)  # EV_ABS, ABS_X, 500
    payload += _struct.pack("llHHi", 1000, 0, 0x03, 0x01, 300)  # EV_ABS, ABS_Y, 300
    payload += _struct.pack("llHHi", 1000, 0, 0x01, 0x14A, 1)   # EV_KEY, BTN_TOUCH, press
    events_path.write_bytes(payload)

    src = TouchSource(device=str(events_path))
    event = src.next_event(timeout=1.0)
    assert event is not None
    assert event.kind == "focus"
    assert event.tile_id is not None
    assert event.tile_id.startswith("__raw__:")
    # (500, 300) -> "500:300"
    assert event.tile_id == "__raw__:500:300"


def test_touch_source_scales_to_layout(tmp_path) -> None:
    """TouchSource rescales raw device coordinates to layout coordinates."""
    events_path = tmp_path / "events.bin"
    import struct as _struct
    payload = b""
    payload += _struct.pack("llHHi", 1000, 0, 0x03, 0x00, 100)  # x=100 on a 200x300 device
    payload += _struct.pack("llHHi", 1000, 0, 0x03, 0x01, 100)  # y=100
    payload += _struct.pack("llHHi", 1000, 0, 0x01, 0x14A, 1)
    events_path.write_bytes(payload)

    src = TouchSource(
        device=str(events_path),
        device_size=(200, 300),
        layout_size=(1072, 1448),
    )
    event = src.next_event(timeout=1.0)
    assert event is not None
    # 100/200 * 1072 = 536; 100/300 * 1448 = 482 (rounded to int)
    assert event.tile_id == "__raw__:536:483"


def test_touch_source_missing_device_logs_and_returns_none(tmp_path, caplog) -> None:
    """A missing touch device should log a warning and not crash."""
    caplog.set_level("WARNING")
    src = TouchSource(device=str(tmp_path / "does-not-exist"))
    event = src.next_event(timeout=0.1)
    assert event is None
    assert any("touch device unavailable" in r.message for r in caplog.records)


def test_tap_source_emits_events_for_each_tap() -> None:
    """TapSource emits one focus event per tap with __raw__ markers."""
    src = TapSource([(100, 200), (300, 400), (500, 600)])
    e1 = src.next_event(timeout=0.1)
    e2 = src.next_event(timeout=0.1)
    e3 = src.next_event(timeout=0.1)
    e4 = src.next_event(timeout=0.1)
    assert e1.tile_id == "__raw__:100:200"
    assert e2.tile_id == "__raw__:300:400"
    assert e3.tile_id == "__raw__:500:600"
    assert e4 is None


def test_combined_source_returns_first_event() -> None:
    """CombinedSource polls each source and returns the first non-None event."""
    five_way = MockSource([InputEvent(kind="activate")])
    tap = TapSource([])
    src = CombinedSource([five_way, tap])
    event = src.next_event(timeout=0.1)
    assert event is not None
    assert event.kind == "activate"


def test_combined_source_requires_at_least_one() -> None:
    """An empty source list is a programming error."""
    with pytest.raises(ValueError):
        CombinedSource([])


def test_focus_tile_resolves_touch_marker_to_layout_tile() -> None:
    """DashboardClient._focus_tile maps a touch marker via Layout.tile_at."""
    layout = Layout.from_dict(
        {
            "schema_version": 2,
            "layout": {"columns": 4, "rows": 6, "tile_size": [240, 160], "grid_size": [1072, 1448]},
            "tiles": [
                {"id": "a", "label": "A", "col": 0, "row": 0, "w": 1, "h": 1, "kind": "action", "action": "x"},
                {"id": "b", "label": "B", "col": 1, "row": 0, "w": 1, "h": 1, "kind": "action", "action": "y"},
            ],
            "focus": {"tile_id": "a", "x": 0, "y": 0},
        }
    )
    bus = BusClient("http://127.0.0.1:9999", "r", "c")
    client = DashboardClient(bus=bus, source=MockSource([]), image_path="/tmp/x.png", refresh_seconds=1.0)
    client._layout = layout
    client._focus_tile_id = "a"
    # Touch at (300, 50) lands inside tile 'b' (col=1, x in 240..480)
    client._focus_tile("__raw__:300:50")
    assert client._focus_tile_id == "b"


def test_focus_tile_ignores_invalid_marker() -> None:
    """Garbage in the tile_id should not crash the client."""
    layout = Layout.from_dict(
        {
            "schema_version": 2,
            "layout": {"columns": 4, "rows": 6, "tile_size": [240, 160], "grid_size": [1072, 1448]},
            "tiles": [{"id": "a", "label": "A", "col": 0, "row": 0, "w": 1, "h": 1, "kind": "action", "action": None}],
            "focus": {"tile_id": "a", "x": 0, "y": 0},
        }
    )
    bus = BusClient("http://127.0.0.1:9999", "r", "c")
    client = DashboardClient(bus=bus, source=MockSource([]), image_path="/tmp/x.png", refresh_seconds=1.0)
    client._layout = layout
    client._focus_tile_id = "a"
    client._focus_tile("not-a-marker")
    client._focus_tile("__raw__:not_numbers")
    assert client._focus_tile_id == "a"


def test_focus_tile_no_match_keeps_focus() -> None:
    """Touch outside any tile should not change focus."""
    layout = Layout.from_dict(
        {
            "schema_version": 2,
            "layout": {"columns": 4, "rows": 6, "tile_size": [240, 160], "grid_size": [1072, 1448]},
            "tiles": [{"id": "a", "label": "A", "col": 0, "row": 0, "w": 1, "h": 1, "kind": "action", "action": None}],
            "focus": {"tile_id": "a", "x": 0, "y": 0},
        }
    )
    bus = BusClient("http://127.0.0.1:9999", "r", "c")
    client = DashboardClient(bus=bus, source=MockSource([]), image_path="/tmp/x.png", refresh_seconds=1.0)
    client._layout = layout
    client._focus_tile_id = "a"
    # Far below any tile
    client._focus_tile("__raw__:500:1400")
    assert client._focus_tile_id == "a"
