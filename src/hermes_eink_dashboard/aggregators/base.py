from __future__ import annotations

from typing import Protocol

from ..contract import PanelData


class Aggregator(Protocol):
    """A provider that independently refreshes one dashboard panel."""

    name: str
    interval_seconds: float

    async def collect(self) -> PanelData:
        """Return normalized, device-neutral panel data."""
        ...
