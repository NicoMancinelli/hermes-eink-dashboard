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
import urllib.request
from dataclasses import dataclass
from typing import Protocol


LOGGER = logging.getLogger("hermes-kindle-dashboard.client")


@dataclass(frozen=True)
class InputEvent:
    """A normalized input event from any source (5-way, touch, mock)."""

    kind: str  # "focus" or "activate"
    tile_id: str | None = None  # populated for focus events; activate uses current focus
    direction: str | None = None  # only for focus events: "up" | "down" | "left" | "right"


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
    tiles: tuple[Tile, ...]
    focus_tile_id: str

    @classmethod
    def from_dict(cls, data: dict) -> "Layout":
        layout = data.get("layout", {})
        tiles = tuple(Tile.from_dict(t) for t in data.get("tiles", []))
        focus = data.get("focus", {})
        return cls(
            columns=int(layout.get("columns", 4)),
            rows=int(layout.get("rows", 6)),
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
        # The control token is used for POST /control. If absent, fall back to read token.
        self._control_token = control_token or read_token
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
        from urllib.parse import quote as _quote

        url = f"{self._base_url}/dashboard.png"
        if focus_tile_id:
            url += f"?focus_tile_id={_quote(focus_tile_id)}"
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self._read_token}"},
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return response.read()

    def post_control(self, tile_id: str, action: str) -> dict:
        import secrets as _secrets

        body = json.dumps(
            {
                "tile_id": tile_id,
                "action": action,
                "nonce": _secrets.token_hex(8),
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

    def run(self) -> None:
        """Run the client loop until stop() is called."""
        self._refresh_layout()
        self._refresh_png()
        last_refresh = time.monotonic()
        while not self._stop:
            event = self._source.next_event(self._event_poll_seconds)
            if event is not None:
                if event.kind == "focus" and event.direction:
                    self._focus_neighbor(event.direction)
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
    parser.add_argument("--input-device", default="/dev/input/event0")
    parser.add_argument("--mock-events", default="", help="JSON list of InputEvent dicts for harness testing")
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


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
    else:
        source = FiveWaySource(args.input_device)

    client = DashboardClient(
        bus=bus,
        source=source,
        image_path=args.image,
        refresh_seconds=args.refresh_seconds,
    )

    # Cooperative shutdown on SIGTERM/SIGINT for KUAL.
    def _signal_handler(signum: int, _frame: object) -> None:
        LOGGER.info("received signal %s; stopping", signum)
        client.stop()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    client.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
