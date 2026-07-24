from __future__ import annotations

import asyncio
import logging
import time

from .aggregators.base import Aggregator
from .contract import PanelCache

LOGGER = logging.getLogger("hermes-kindle-dashboard.scheduler")
_MAX_BACKOFF_SECONDS = 300.0


def _error_code(error: Exception) -> str:
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(error, PermissionError):
        return "permission_denied"
    return "collection_failed"


async def collect_once(aggregator: Aggregator, cache: PanelCache) -> bool:
    """Refresh one panel without exposing provider exception details to clients."""

    started = time.monotonic()
    try:
        timeout = max(0.001, float(getattr(aggregator, "timeout_seconds", 30.0)))
        data = await asyncio.wait_for(aggregator.collect(), timeout=timeout)
        cache.record_success(aggregator.name, data)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        code = _error_code(error)
        cache.record_failure(aggregator.name, code)
        LOGGER.warning(
            "aggregator=%s result=error code=%s duration_ms=%d",
            aggregator.name,
            code,
            round((time.monotonic() - started) * 1000),
        )
        return False

    LOGGER.info(
        "aggregator=%s result=ok duration_ms=%d",
        aggregator.name,
        round((time.monotonic() - started) * 1000),
    )
    return True


async def _wait_or_stop(stop_event: asyncio.Event, delay: float) -> None:
    if stop_event.is_set():
        return
    if delay <= 0:
        await asyncio.sleep(0)
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except TimeoutError:
        pass


async def run_aggregator_loop(
    aggregator: Aggregator,
    cache: PanelCache,
    stop_event: asyncio.Event,
    *,
    initial_delay: bool = False,
) -> None:
    """Run one serial refresh loop; failures back off without overlapping work."""

    cache.register(aggregator.name)
    failures = 0
    if initial_delay:
        await _wait_or_stop(stop_event, max(0.0, aggregator.interval_seconds))
    while not stop_event.is_set():
        succeeded = await collect_once(aggregator, cache)
        if succeeded:
            failures = 0
            delay = max(0.0, aggregator.interval_seconds)
        else:
            failures += 1
            base = max(1.0, aggregator.interval_seconds)
            delay = min(_MAX_BACKOFF_SECONDS, base * (2 ** min(failures - 1, 6)))
        await _wait_or_stop(stop_event, delay)
