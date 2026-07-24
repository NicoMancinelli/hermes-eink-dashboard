import asyncio

from hermes_kindle_dashboard.contract import PanelCache
from hermes_kindle_dashboard.scheduler import collect_once, run_aggregator_loop


class SuccessfulAggregator:
    name = "weather"
    interval_seconds = 60.0

    async def collect(self):
        return {"current": {"temperature": 82}}


class FailingAggregator:
    name = "calendar"
    interval_seconds = 60.0

    async def collect(self):
        raise TimeoutError("upstream details must not leak")


def test_collect_once_records_success() -> None:
    cache = PanelCache()

    succeeded = asyncio.run(collect_once(SuccessfulAggregator(), cache))

    assert succeeded is True
    weather = cache.snapshot()["panels"]["weather"]
    assert weather["_meta"]["status"] == "ok"
    assert weather["current"]["temperature"] == 82


def test_collect_once_records_sanitized_failure() -> None:
    cache = PanelCache()

    succeeded = asyncio.run(collect_once(FailingAggregator(), cache))

    assert succeeded is False
    calendar = cache.snapshot()["panels"]["calendar"]
    assert calendar["_meta"]["status"] == "unavailable"
    assert calendar["_meta"]["error_code"] == "timeout"
    assert "upstream details" not in str(calendar)


def test_repeated_failures_retain_last_successful_data() -> None:
    cache = PanelCache()
    asyncio.run(collect_once(SuccessfulAggregator(), cache))

    class NowFailing(SuccessfulAggregator):
        async def collect(self):
            raise RuntimeError("provider response")

    asyncio.run(collect_once(NowFailing(), cache))

    weather = cache.snapshot()["panels"]["weather"]
    assert weather["_meta"]["status"] == "stale"
    assert weather["_meta"]["error_code"] == "collection_failed"
    assert weather["current"]["temperature"] == 82


def test_collect_once_enforces_provider_timeout() -> None:
    class SlowAggregator:
        name = "home"
        interval_seconds = 60.0
        timeout_seconds = 0.001

        async def collect(self):
            await asyncio.sleep(1)
            return {"sensors": {}}

    cache = PanelCache()

    succeeded = asyncio.run(collect_once(SlowAggregator(), cache))

    assert succeeded is False
    assert cache.snapshot()["panels"]["home"]["_meta"]["error_code"] == "timeout"


def test_collect_once_contains_invalid_provider_payload() -> None:
    class InvalidAggregator:
        name = "weather"
        interval_seconds = 60.0

        async def collect(self):
            return {"_meta": {"status": "forged"}}

    cache = PanelCache()

    succeeded = asyncio.run(collect_once(InvalidAggregator(), cache))

    assert succeeded is False
    weather = cache.snapshot()["panels"]["weather"]
    assert weather["_meta"]["status"] == "unavailable"
    assert weather["_meta"]["error_code"] == "collection_failed"


def test_aggregator_loop_never_overlaps_collection() -> None:
    class CountingAggregator:
        name = "tasks"
        interval_seconds = 0.0

        def __init__(self):
            self.calls = 0
            self.active = 0
            self.max_active = 0
            self.stop_event: asyncio.Event | None = None

        async def collect(self):
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0)
            self.active -= 1
            if self.calls == 3:
                assert self.stop_event is not None
                self.stop_event.set()
            return {"items": []}

    async def scenario():
        aggregator = CountingAggregator()
        stop_event = asyncio.Event()
        aggregator.stop_event = stop_event
        await run_aggregator_loop(aggregator, PanelCache(), stop_event)
        return aggregator

    aggregator = asyncio.run(scenario())
    assert aggregator.calls == 3
    assert aggregator.max_active == 1


def test_control_bus_publish_and_receive() -> None:
    from hermes_kindle_dashboard.scheduler import ControlBus

    bus = ControlBus()

    async def scenario():
        bus.publish({"tile_id": "wf:briefing", "action": "workflow.briefing"})
        event = await bus.wait_for_event(timeout=1.0)
        return event

    event = asyncio.run(scenario())
    assert event == {"tile_id": "wf:briefing", "action": "workflow.briefing"}


def test_control_bus_wait_timeout() -> None:
    from hermes_kindle_dashboard.scheduler import ControlBus

    bus = ControlBus()

    async def scenario():
        return await bus.wait_for_event(timeout=0.01)

    event = asyncio.run(scenario())
    assert event is None


def test_control_bus_async_wakeup() -> None:
    from hermes_kindle_dashboard.scheduler import ControlBus

    bus = ControlBus()

    async def scenario():
        async def delayed_publish():
            await asyncio.sleep(0.02)
            bus.publish({"action": "refresh"})

        task = asyncio.create_task(delayed_publish())
        event = await bus.wait_for_event(timeout=1.0)
        await task
        return event

    event = asyncio.run(scenario())
    assert event == {"action": "refresh"}

