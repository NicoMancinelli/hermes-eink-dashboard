"""Interactive Kindle client for the Hermes E-Ink Dashboard.

Phase 3 of the interactive dashboard plan. The client reads the
schema v2 layout from /dashboard.json, listens for navigation input
events (5-way nav on basic Kindles, touch on Paperwhite), and
dispatches /control events when the user activates a tile.

The client is written in stdlib Python 3 so it runs on:
- The Kindle (with whatever Python 3 the user has bundled, e.g. via mrpackage).
- pdi (for harness tests; uses MockSource + a fake host).

Architecture:
- InputSource protocol: FiveWaySource for Kindles (reads /dev/input/event*),
  MockSource for tests.
- BusClient: HTTP wrapper using urllib.request. No external deps.
- DashboardClient: state machine that pulls layout, polls PNG, dispatches
  control events.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import select
import signal
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol


LOGGER = logging.getLogger("hermes-kindle-dashboard.client")


# Linux input event types and key/axis codes (see /usr/include/linux/input-event-codes.h).
EV_KEY = 0x01
EV_ABS = 0x03
ABS_X = 0x00
ABS_Y = 0x01
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
BTN_TOUCH = 0x14A  # 330
# Single-touch protocol A gives screen-space coordinates in ABS_X/ABS_Y.
# Multi-touch protocol B uses ABS_MT_POSITION_X/Y (more common on Voyage+).
# Both are handled.


@dataclass(frozen=True)
class InputEvent:
    """A normalized input event from any source (5-way, touch, mock).

    `kind` is "focus" or "activate". For tap-style focus events (touch),
    `tile_id` is set directly so the client does not need to look up the
    neighbor. For 5-way focus events, `direction` is set so the client uses
    the layout's neighbor() helper.
    """

    kind: str
    tile_id: str | None = None
    direction: str | None = None


@dataclass(frozen=True)
class Tile:
    """Subset of the host-side Tile dataclass used by the client."""

    id: str
    label: str
    col: int
    row: int
    w: int
    h: int
    kind: str
    action: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "Tile":
        return cls(
            id=str(data["id"]),
            label=str(data.get("label", "")),
            col=int(data.get("col", 0)),
            row=int(data.get("row", 0)),
            w=int(data.get("w", 1)),
            h=int(data.get("h", 1)),
            kind=str(data.get("kind", "action")),
            action=data.get("action"),
        )


@dataclass(frozen=True)
class Layout:
    """Parsed /dashboard.json payload."""

    columns: int
    rows: int
    grid_width: int
    grid_height: int
    tile_width: int
    tile_height: int
    tiles: tuple[Tile, ...]
    focus_tile_id: str

    @classmethod
    def from_dict(cls, data: dict) -> "Layout":
        layout = data.get("layout", {})
        tiles = tuple(Tile.from_dict(t) for t in data.get("tiles", []))
        focus = data.get("focus", {})
        grid_size = layout.get("grid_size", [1072, 1448])
        tile_size = layout.get("tile_size", [grid_size[0] // max(1, int(layout.get("columns", 4))), grid_size[1] // max(1, int(layout.get("rows", 6)))])
        return cls(
            columns=int(layout.get("columns", 4)),
            rows=int(layout.get("rows", 6)),
            grid_width=int(grid_size[0]),
            grid_height=int(grid_size[1]),
            tile_width=int(tile_size[0]),
            tile_height=int(tile_size[1]),
            tiles=tiles,
            focus_tile_id=str(focus.get("tile_id", tiles[0].id if tiles else "")),
        )

    def neighbor(self, tile_id: str, direction: str) -> str | None:
        """Return the tile id adjacent to tile_id in the given direction, or None.

        Edges touching count as overlap (adjacent tiles on the same row/col band).
        """
        target = next((t for t in self.tiles if t.id == tile_id), None)
        if target is None:
            return None

        def overlaps(a: Tile, b: Tile) -> bool:
            return not (
                a.col + a.w < b.col
                or b.col + b.w < a.col
                or a.row + a.h < b.row
                or b.row + b.h < a.row
            )

        if direction == "left":
            candidates = [t for t in self.tiles if t.col + t.w <= target.col and overlaps(t, target)]
        elif direction == "right":
            candidates = [t for t in self.tiles if target.col + target.w <= t.col and overlaps(t, target)]
        elif direction == "up":
            candidates = [t for t in self.tiles if t.row + t.h <= target.row and overlaps(t, target)]
        elif direction == "down":
            candidates = [t for t in self.tiles if target.row + target.h <= t.row and overlaps(t, target)]
        else:
            return None
        if not candidates:
            return None
        # Pick the candidate with the smallest Euclidean distance to the target's center.
        tcx = target.col + target.w / 2.0
        tcy = target.row + target.h / 2.0
        candidates.sort(key=lambda t: (tcx - (t.col + t.w / 2.0)) ** 2 + (tcy - (t.row + t.h / 2.0)) ** 2)
        return candidates[0].id

    def tile_at(self, x: float, y: float) -> str | None:
        """Return the tile id under (x, y) in layout coordinates, or None.

        Layout coordinates match the host's `grid_size` (default 1072 x 1448).
        Coordinates outside the grid are rejected. The first matching tile
        wins.
        """
        for tile in self.tiles:
            tile_left = tile.col * self.tile_width
            tile_top = tile.row * self.tile_height
            tile_right = tile_left + tile.w * self.tile_width
            tile_bottom = tile_top + tile.h * self.tile_height
            if tile_left <= x < tile_right and tile_top <= y < tile_bottom:
                return tile.id
        return None
    """Protocol for input sources on the Kindle and in tests."""

    def next_event(self, timeout: float) -> InputEvent | None:
        """Return the next event, or None if timeout elapses with no input."""


class InputSource(Protocol):
    """Protocol for input sources on the Kindle and in tests."""

    def next_event(self, timeout: float) -> InputEvent | None:
        """Return the next event, or None if timeout elapses with no input."""


class MockSource:
    """An input source that emits scripted events from a list. Used for tests."""

    def __init__(self, events: list[InputEvent]) -> None:
        self._events = iter(events)
        self._closed = False

    def next_event(self, timeout: float) -> InputEvent | None:
        if self._closed:
            return None
        try:
            return next(self._events)
        except StopIteration:
            return None


class FiveWaySource:
    """Reads 5-way nav events from a Linux input device.

    The basic Kindle exposes /dev/input/event0 with EV_KEY events for the
    5-way controller. We use raw struct decoding so we don't depend on the
    `evdev` Python package (which is not on the Kindle by default).

    key codes (Kindle 5-way): 103=Up, 108=Down, 105=Left, 106=Right, 28=Select.
    """

    KEY_UP = 103
    KEY_DOWN = 108
    KEY_LEFT = 105
    KEY_RIGHT = 106
    KEY_SELECT = 28
    _KEY_TO_DIRECTION = {
        KEY_UP: "up",
        KEY_DOWN: "down",
        KEY_LEFT: "left",
        KEY_RIGHT: "right",
    }
    _EVENT_STRUCT = struct.Struct("llHHi")  # time_sec, time_usec, type, code, value

    def __init__(self, device: str = "/dev/input/event0") -> None:
        self._device = device

    def next_event(self, timeout: float) -> InputEvent | None:
        try:
            fd = os.open(self._device, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            LOGGER.warning("input device unavailable: %s (%s)", self._device, exc)
            time.sleep(min(timeout, 1.0))
            return None
        try:
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                rlist, _, _ = select.select([fd], [], [], remaining)
                if not rlist:
                    return None
                try:
                    data = os.read(fd, self._EVENT_STRUCT.size)
                except BlockingIOError:
                    continue
                if len(data) < self._EVENT_STRUCT.size:
                    continue
                _, _, ev_type, code, value = self._EVENT_STRUCT.unpack(data)
                if ev_type != 0x01 or value != 1:  # EV_KEY and key-down only
                    continue
                if code in self._KEY_TO_DIRECTION:
                    return InputEvent(kind="focus", direction=self._KEY_TO_DIRECTION[code])
                if code == self.KEY_SELECT:
                    return InputEvent(kind="activate")
        finally:
            os.close(fd)


class TouchSource:
    """Reads touchscreen events from a Linux input device.

    Single-touch protocol A: ABS_X/ABS_Y carry screen-space coordinates
    and BTN_TOUCH (value 1 = press, 0 = release) marks the press boundary.
    Multi-touch protocol B (common on Voyage and later) uses
    ABS_MT_POSITION_X/Y. We handle both.

    The source emits raw-coordinate events tagged with ``__raw__:x:y``. The
    client resolves those to tile ids via Layout.tile_at() so the Kindle
    device pixel resolution can differ from the layout grid.
    """

    _EVENT_STRUCT = struct.Struct("llHHi")

    def __init__(
        self,
        device: str = "/dev/input/event1",
        device_size: tuple[int, int] | None = None,
        layout_size: tuple[int, int] = (1072, 1448),
    ) -> None:
        self._device = device
        self._device_size = device_size
        self._layout_size = layout_size

    def _scale(self, raw_x: int, raw_y: int) -> tuple[float, float]:
        if self._device_size is None:
            return float(raw_x), float(raw_y)
        dx, dy = self._device_size
        lx, ly = self._layout_size
        return raw_x * lx / dx, raw_y * ly / dy

    def next_event(self, timeout: float) -> InputEvent | None:
        try:
            fd = os.open(self._device, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            LOGGER.warning("touch device unavailable: %s (%s)", self._device, exc)
            time.sleep(min(timeout, 1.0))
            return None
        try:
            deadline = time.monotonic() + max(0.0, timeout)
            pending_x: int | None = None
            pending_y: int | None = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                rlist, _, _ = select.select([fd], [], [], remaining)
                if not rlist:
                    return None
                try:
                    data = os.read(fd, self._EVENT_STRUCT.size)
                except BlockingIOError:
                    continue
                if len(data) < self._EVENT_STRUCT.size:
                    continue
                _, _, ev_type, code, value = self._EVENT_STRUCT.unpack(data)
                if ev_type == EV_ABS:
                    if code in (ABS_X, ABS_MT_POSITION_X):
                        pending_x = value
                    elif code in (ABS_Y, ABS_MT_POSITION_Y):
                        pending_y = value
                elif ev_type == EV_KEY and code == BTN_TOUCH:
                    if pending_x is None or pending_y is None:
                        return None
                    sx, sy = self._scale(pending_x, pending_y)
                    return InputEvent(kind="focus", tile_id=f"__raw__:{sx:.0f}:{sy:.0f}")
        finally:
            os.close(fd)


class TapSource:
    """Test helper that emits tap-style events from a list of (x, y) coords."""

    def __init__(self, taps: list[tuple[float, float]]) -> None:
        self._taps = iter(taps)

    def next_event(self, timeout: float) -> InputEvent | None:
        try:
            x, y = next(self._taps)
        except StopIteration:
            return None
        return InputEvent(kind="focus", tile_id=f"__raw__:{x:.0f}:{y:.0f}")


class CombinedSource:
    """Multiplexes multiple InputSource instances.

    Polls each source in turn until one returns a non-None event. Used to
    drive a Kindle with both a 5-way controller (event0) and a touchscreen
    (event1) from a single DashboardClient.
    """

    def __init__(self, sources: list[InputSource]) -> None:
        if not sources:
            raise ValueError("CombinedSource requires at least one source")
        self._sources = list(sources)

    def next_event(self, timeout: float) -> InputEvent | None:
        deadline = time.monotonic() + max(0.0, timeout)
        per_source = max(0.05, min(0.5, timeout))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            budget = min(per_source, remaining)
            for source in self._sources:
                event = source.next_event(budget)
                if event is not None:
                    return event


class BusClient:
    """Thin HTTP client for the dashboard host. Uses stdlib only."""

    def __init__(
        self,
        base_url: str,
        read_token: str,
        control_token: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._read_token = read_token
        # The control token is used for POST /control. It MUST be distinct
        # from the read token: if the host has no separate control token
        # configured, the /control endpoint returns 503 and dispatching
        # silently is worse than failing fast.
        self._control_token = control_token or ""
        self._timeout = timeout

    def get_layout(self) -> Layout:
        request = urllib.request.Request(
            f"{self._base_url}/dashboard.json",
            headers={"Authorization": f"Bearer {self._read_token}"},
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return Layout.from_dict(payload)

    def get_png(self, focus_tile_id: str | None = None) -> bytes:
        url = f"{self._base_url}/dashboard.png"
        if focus_tile_id:
            url += f"?focus_tile_id={urllib.parse.quote(focus_tile_id)}"
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self._read_token}"},
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return response.read()

    def post_control(self, tile_id: str, action: str) -> dict:
        import secrets
        body = json.dumps(
            {
                "tile_id": tile_id,
                "action": action,
                "nonce": secrets.token_hex(8),
                "ts": int(time.time()),
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/control",
            data=body,
            headers={
                "Authorization": f"Bearer {self._control_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return {"status": "error", "code": exc.code, "detail": exc.read().decode("utf-8", errors="replace")[:200]}
        except urllib.error.URLError as exc:
            return {"status": "error", "code": -1, "detail": f"connection_failed: {exc.reason}"}


class DashboardClient:
    """Top-level state machine: read layout, fetch PNG, dispatch events."""

    def __init__(
        self,
        bus: BusClient,
        source: InputSource,
        image_path: str,
        refresh_seconds: float = 15.0,
        event_poll_seconds: float = 0.5,
    ) -> None:
        self._bus = bus
        self._source = source
        self._image_path = image_path
        self._refresh_seconds = max(0.5, refresh_seconds)
        self._event_poll_seconds = max(0.1, event_poll_seconds)
        self._stop = False
        self._layout: Layout | None = None
        self._focus_tile_id: str = ""

    def stop(self) -> None:
        self._stop = True

    def _save_png(self, png: bytes) -> None:
        tmp = self._image_path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(png)
        os.replace(tmp, self._image_path)

    def _refresh_layout(self) -> None:
        self._layout = self._bus.get_layout()
        if not self._focus_tile_id or self._focus_tile_id not in {t.id for t in self._layout.tiles}:
            self._focus_tile_id = self._layout.focus_tile_id

    def _refresh_png(self) -> None:
        png = self._bus.get_png(self._focus_tile_id)
        self._save_png(png)

    def _dispatch(self) -> None:
        if self._layout is None:
            return
        tile = next((t for t in self._layout.tiles if t.id == self._focus_tile_id), None)
        if tile is None or not tile.action:
            LOGGER.info("activate on tile without action: %s", self._focus_tile_id)
            return
        result = self._bus.post_control(tile_id=tile.id, action=tile.action)
        LOGGER.info("control dispatched: tile=%s action=%s result=%s", tile.id, tile.action, result)

    def _focus_neighbor(self, direction: str) -> None:
        if self._layout is None:
            return
        next_id = self._layout.neighbor(self._focus_tile_id, direction)
        if next_id and next_id != self._focus_tile_id:
            self._focus_tile_id = next_id

    def _focus_tile(self, raw_tile_id: str) -> None:
        """Resolve a touch marker (``__raw__:x:y``) to a real tile id."""
        if self._layout is None or not raw_tile_id.startswith("__raw__:"):
            return
        try:
            _, xs, ys = raw_tile_id.split(":", 2)
            x = float(xs)
            y = float(ys)
        except ValueError:
            return
        target = self._layout.tile_at(x, y)
        if target and target != self._focus_tile_id:
            self._focus_tile_id = target

    def run(self) -> None:
        """Run the client loop until stop() is called."""
        self._refresh_layout()
        self._refresh_png()
        last_refresh = time.monotonic()
        while not self._stop:
            event = self._source.next_event(self._event_poll_seconds)
            if event is not None:
                if event.kind == "focus":
                    if event.direction:
                        self._focus_neighbor(event.direction)
                    elif event.tile_id:
                        self._focus_tile(event.tile_id)
                elif event.kind == "activate":
                    self._dispatch()
                # Always refresh the PNG after an input event so the focus border updates.
                self._refresh_png()
                last_refresh = time.monotonic()
                continue
            if time.monotonic() - last_refresh >= self._refresh_seconds:
                self._refresh_png()
                last_refresh = time.monotonic()


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes E-Ink Dashboard interactive client")
    parser.add_argument("--host", default=os.getenv("HERMES_DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("HERMES_DASHBOARD_PORT", "9120")))
    parser.add_argument("--read-token", default=os.getenv("HERMES_DASHBOARD_READ_TOKEN", ""))
    parser.add_argument("--control-token", default=os.getenv("HERMES_DASHBOARD_CONTROL_TOKEN", ""))
    parser.add_argument("--image", default="/tmp/hermes-dashboard.png")
    parser.add_argument("--refresh-seconds", type=float, default=15.0)
    parser.add_argument("--input-device", default="/dev/input/event0", help="5-way controller (e.g. /dev/input/event0)")
    parser.add_argument("--touch-device", default="/dev/input/event1", help="Touchscreen device. Empty to disable touch.")
    parser.add_argument("--touch-device-size", default="", help="WxH of the touch device in pixels (e.g. 1072x1448). Empty = no rescale.")
    parser.add_argument("--mock-events", default="", help="JSON list of InputEvent dicts for harness testing")
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


def _install_signal_handlers(client: DashboardClient) -> None:
    """Wire SIGTERM/SIGINT to a cooperative stop on the client."""
    def _handler(signum: int, _frame: object) -> None:
        LOGGER.info("received signal %s; stopping", signum)
        client.stop()
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if not args.read_token:
        LOGGER.error("--read-token (or HERMES_DASHBOARD_READ_TOKEN) is required")
        return 2

    bus = BusClient(
        base_url=f"http://{args.host}:{args.port}",
        read_token=args.read_token,
        control_token=args.control_token or None,
    )

    if args.mock_events:
        events = [InputEvent(**e) for e in json.loads(args.mock_events)]
        source: InputSource = MockSource(events)
    elif args.touch_device and os.path.exists(args.touch_device):
        device_size = None
        if args.touch_device_size:
            try:
                w, h = args.touch_device_size.split("x", 1)
                device_size = (int(w), int(h))
            except ValueError:
                LOGGER.warning("invalid --touch-device-size %r; ignoring", args.touch_device_size)
        five_way = FiveWaySource(args.input_device)
        touch = TouchSource(device=args.touch_device, device_size=device_size)
        source = CombinedSource([five_way, touch])
    else:
        source = FiveWaySource(args.input_device)

    client = DashboardClient(
        bus=bus,
        source=source,
        image_path=args.image,
        refresh_seconds=args.refresh_seconds,
    )

    _install_signal_handlers(client)
    client.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
