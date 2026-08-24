"""Resilience tests: transient network failures never kill the interactive client."""
from __future__ import annotations

import threading
import time
import urllib.error
from types import SimpleNamespace

import pytest

from kindle.client.interactive import (
    DashboardClient,
    FbinkPainter,
    InputEvent,
    MockSource,
    TransientNetworkError,
    _is_transient,
)


class FlakyBus:
    """BusClient stand-in failing N times per method before succeeding."""

    def __init__(self, layout_failures: int = 0, png_failures: int = 0) -> None:
        self.layout_failures = layout_failures
        self.png_failures = png_failures
        self.layout_calls = 0
        self.png_calls = 0

    def get_layout(self):
        self.layout_calls += 1
        if self.layout_failures > 0:
            self.layout_failures -= 1
            raise urllib.error.URLError("connection refused")
        return SimpleNamespace(
            tiles=[],
            focus_tile_id="t1",
            neighbor=lambda *_: "t1",
            tile_at=lambda *_: None,
        )

    def get_png(self, focus_tile_id=None):
        self.png_calls += 1
        if self.png_failures > 0:
            self.png_failures -= 1
            raise urllib.error.URLError("timed out")
        return b"png-bytes"

    def post_control(self, tile_id, action):
        return {"status": "ok"}


class RecordingPainter:
    def __init__(self) -> None:
        self.paints = 0
        self.offline_screens = 0

    def paint(self, image_path: str) -> bool:
        self.paints += 1
        return True

    def show_offline(self) -> bool:
        self.offline_screens += 1
        return True


def _client(bus, painter=None, **kwargs) -> DashboardClient:
    defaults = dict(
        source=MockSource([]),
        image_path="/tmp/test-dash.png",
        refresh_seconds=0.05,
        event_poll_seconds=0.01,
        retry_attempts=2,
        retry_backoff=0.0,
    )
    defaults.update(kwargs)
    return DashboardClient(bus=bus, painter=painter, **defaults)


class TestClassification:
    def test_transient_vs_fatal(self) -> None:
        assert _is_transient(urllib.error.URLError("refused"))
        assert _is_transient(TimeoutError())
        assert _is_transient(ConnectionError())
        assert _is_transient(urllib.error.HTTPError("u", 502, "bad gw", None, None))
        assert not _is_transient(urllib.error.HTTPError("u", 401, "no", None, None))
        assert not _is_transient(ValueError())


class TestFbinkPainter:
    def _painter(self, runner, fbink="/usr/bin/fbink", every=3):
        return FbinkPainter(enabled=True, fbink_path=fbink, full_refresh_every=every, runner=runner)

    def test_paint_argv_and_flash_cadence(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0)

        painter = self._painter(runner, every=2)
        assert painter.active is True
        assert painter.paint("/tmp/a.png") is True
        assert painter.paint("/tmp/a.png") is True
        assert painter.paint("/tmp/a.png") is True
        # First paint flashes (-f), second doesn't, third does again.
        assert "-f" in calls[0]
        assert "-f" not in calls[1]
        assert "-f" in calls[2]
        for argv in calls:
            joined = " ".join(argv)
            assert "-g" in argv and "file=/tmp/a.png" in joined and "GC16" in joined
            assert argv[0] == "/usr/bin/fbink" and argv[1] == "-q"

    def test_inactive_without_fbink_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(FbinkPainter, "_locate_fbink", staticmethod(lambda shutil: ""))
        painter = FbinkPainter(enabled=True)
        assert painter.active is False
        assert painter.paint("/tmp/x.png") is False
        assert painter.show_offline() is False

    def test_disabled_painter_never_invokes(self) -> None:
        def boom(argv, **kwargs):
            raise AssertionError("should not run")

        painter = FbinkPainter(enabled=False, runner=boom)
        assert painter.active is False
        assert painter.paint("/tmp/x.png") is False

    def test_show_offline_falls_back_to_plain_text(self, tmp_path) -> None:
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0)

        monkeypatched_regular = "/nonexistent/caecilia.ttf"
        painter = self._painter(runner)
        # Force the font branch to fail by pointing at a nonexistent regular font.
        import kindle.client.interactive as mod

        original_exists = mod.os.path.exists
        mod.os.path.exists = lambda p: p == monkeypatched_regular or original_exists(p)
        try:
            ok = painter.show_offline()
        finally:
            mod.os.path.exists = original_exists
        # With a nonexistent font dir we take the plain-text tier directly.
        assert ok is True
        assert len(calls) == 1 and "-S" in calls[0]


class TestClientResilience:
    def test_startup_failures_go_offline_then_recover(self) -> None:
        bus = FlakyBus(layout_failures=99, png_failures=99)
        painter = RecordingPainter()
        client = _client(bus, painter=painter)

        thread = threading.Thread(target=client.run, daemon=True)
        thread.start()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not client.offline_reason:
            time.sleep(0.01)
        assert client.offline_reason, "client did not enter offline state"
        # Exactly one offline screen despite repeated failures.
        first_count = painter.offline_screens
        time.sleep(0.15)
        assert painter.offline_screens == first_count

        # Heal the network; the next periodic refresh must recover.
        bus.layout_failures = 0
        bus.png_failures = 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and client.offline_reason:
            time.sleep(0.01)
        assert not client.offline_reason
        assert painter.paints >= 1

        client.stop()
        thread.join(timeout=5)
        assert not thread.is_alive()

    def test_offline_screen_drawn_once_per_outage(self) -> None:
        bus = FlakyBus(layout_failures=99)
        painter = RecordingPainter()
        client = _client(bus, painter=painter)
        thread = threading.Thread(target=client.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not client.offline_reason:
            time.sleep(0.01)
        time.sleep(0.1)
        assert painter.offline_screens == 1
        client.stop()
        thread.join(timeout=5)

    def test_fatal_401_stops_client_with_exit_code(self) -> None:
        class AuthDeadBus(FlakyBus):
            def get_layout(self):
                raise urllib.error.HTTPError("http://h/dashboard.json", 401, "unauthorized", None, None)

        client = _client(AuthDeadBus(), painter=RecordingPainter())
        result = client.run()
        assert result == 1

    def test_retry_exhaustion_raises_transient(self) -> None:
        client = _client(FlakyBus(layout_failures=99), retry_attempts=2, retry_backoff=0.5)
        sleeps: list[float] = []
        client._sleep = lambda seconds: sleeps.append(seconds)
        with pytest.raises(TransientNetworkError):
            client._refresh_display()
        # One backoff sleep between two attempts, exponential base.
        assert sleeps == [0.5]

    def test_focus_state_survives_offline_period(self) -> None:
        bus = FlakyBus(layout_failures=99, png_failures=99)
        client = _client(bus, painter=RecordingPainter())
        thread = threading.Thread(target=client.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not client.offline_reason:
            time.sleep(0.01)

        bus.layout_failures = 0
        bus.png_failures = 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and client.offline_reason:
            time.sleep(0.01)
        assert client._layout is not None
        assert client._focus_tile_id == "t1"
        client.stop()
        thread.join(timeout=5)
