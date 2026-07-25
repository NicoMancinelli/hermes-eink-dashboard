import asyncio
import json
from pathlib import Path

import pytest

from hermes_kindle_dashboard.aggregators.hermes import HermesAggregator, snapshot_to_panel
from hermes_kindle_dashboard.state import DashboardSnapshot
from test_render import sample_snapshot


class FakeCollector:
    def __init__(self, snapshot: DashboardSnapshot):
        self.snapshot = snapshot
        self.calls = 0

    def collect(self) -> DashboardSnapshot:
        self.calls += 1
        return self.snapshot


def test_snapshot_converts_to_device_neutral_hermes_panel() -> None:
    snapshot = sample_snapshot()

    panel = snapshot_to_panel(snapshot)

    assert "generated_at" not in panel
    assert panel["session"]["model"] == "gpt-5.6-sol"
    assert panel["tasks"][0] == {
        "title": "Render a crisp monochrome PNG",
        "status": "in_progress",
        "source": "session",
    }
    assert panel["memory"]["fact_count"] == 185
    assert "secret" not in str(panel).lower()


def test_snapshot_reconstructs_from_panel_for_legacy_rendering() -> None:
    original = sample_snapshot()
    panel = snapshot_to_panel(original)

    reconstructed = DashboardSnapshot.from_panel(panel, original.generated_at)

    assert reconstructed == original


def test_local_hermes_aggregator_wraps_sync_collector() -> None:
    collector = FakeCollector(sample_snapshot())
    aggregator = HermesAggregator(collector=collector, interval_seconds=17.0)

    panel = asyncio.run(aggregator.collect())

    assert aggregator.name == "hermes"
    assert aggregator.interval_seconds == 17.0
    assert collector.calls == 1
    assert panel["session"]["status"] == "working"



class _TmpConfig:
    def __init__(self, path: Path) -> None:
        self.path = path

    def write_dismissed(self, alert_id: str, ts: str) -> None:
        existing = {}
        if self.path.exists():
            existing = json.loads(self.path.read_text())
        existing[alert_id] = ts
        self.path.write_text(json.dumps(existing, indent=2))

    def write_context(self, ctx: str, ts: str) -> None:
        payload = {"context": ctx, "updated_at": ts}
        self.path.write_text(json.dumps(payload, indent=2))


def test_aggregator_surfaces_user_state(tmp_path: Path) -> None:
    cfg = _TmpConfig(tmp_path / "dismissed_alerts.json")
    cfg.write_dismissed("alert-1", "2026-07-25T07:00:00Z")
    cfg.write_dismissed("alert-2", "2026-07-25T07:05:00Z")
    (tmp_path / "context.json").write_text(json.dumps({"context": "ops-room", "updated_at": "2026-07-25T07:10:00Z"}))
    collector = FakeCollector(sample_snapshot())
    aggregator = HermesAggregator(collector=collector, interval_seconds=10.0, config_dir=tmp_path)
    panel = asyncio.run(aggregator.collect())
    assert "user_state" in panel
    assert panel["user_state"]["dismissed_alerts"] == {
        "alert-1": "2026-07-25T07:00:00Z",
        "alert-2": "2026-07-25T07:05:00Z",
    }
    assert panel["user_state"]["active_context"] == "ops-room"
    assert panel["user_state"]["context_updated_at"] == "2026-07-25T07:10:00Z"


def test_aggregator_handles_missing_user_state(tmp_path: Path) -> None:
    collector = FakeCollector(sample_snapshot())
    aggregator = HermesAggregator(collector=collector, interval_seconds=10.0, config_dir=tmp_path)
    panel = asyncio.run(aggregator.collect())
    assert panel["user_state"] == {
        "dismissed_alerts": {},
        "active_context": "",
        "context_updated_at": "",
    }


def test_aggregator_swallows_invalid_json(tmp_path: Path) -> None:
    (tmp_path / "dismissed_alerts.json").write_text("{this is not json")
    (tmp_path / "context.json").write_text("also bad")
    collector = FakeCollector(sample_snapshot())
    aggregator = HermesAggregator(collector=collector, interval_seconds=10.0, config_dir=tmp_path)
    panel = asyncio.run(aggregator.collect())
    assert panel["user_state"]["dismissed_alerts"] == {}
    assert panel["user_state"]["active_context"] == ""
