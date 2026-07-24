import asyncio

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
