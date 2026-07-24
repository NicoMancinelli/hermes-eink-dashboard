from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..contract import PanelData
from ..state import DashboardSnapshot, HermesStateCollector


def snapshot_to_panel(snapshot: DashboardSnapshot) -> PanelData:
    """Normalize a sanitized Hermes snapshot into the stable panel shape."""

    payload = snapshot.to_dict()
    payload.pop("generated_at", None)
    return payload


@dataclass
class HermesAggregator:
    """Collect Hermes state locally without blocking the event loop."""

    collector: HermesStateCollector
    interval_seconds: float = 15.0
    timeout_seconds: float = 10.0
    name: str = field(default="hermes", init=False)

    async def collect(self) -> PanelData:
        snapshot = await asyncio.to_thread(self.collector.collect)
        return snapshot_to_panel(snapshot)
