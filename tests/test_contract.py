from datetime import datetime, timezone

from hermes_kindle_dashboard.contract import PanelCache


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def test_registered_panel_is_unavailable_in_versioned_snapshot() -> None:
    clock = Clock(datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc))
    cache = PanelCache(now=clock)

    cache.register("weather")
    payload = cache.snapshot()

    assert payload == {
        "schema_version": 1,
        "generated_at": "2026-07-24T12:00:00+00:00",
        "panels": {
            "weather": {
                "_meta": {
                    "status": "unavailable",
                    "updated_at": None,
                    "last_attempt_at": None,
                    "error_code": None,
                }
            }
        },
    }


def test_success_then_failure_retains_stale_panel_data() -> None:
    clock = Clock(datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc))
    cache = PanelCache(now=clock)
    cache.register("weather")

    cache.record_success("weather", {"current": {"temperature": 82}})
    clock.value = datetime(2026, 7, 24, 12, 5, tzinfo=timezone.utc)
    cache.record_failure("weather", "provider_timeout")

    weather = cache.snapshot()["panels"]["weather"]
    assert weather["current"] == {"temperature": 82}
    assert weather["_meta"] == {
        "status": "stale",
        "updated_at": "2026-07-24T12:00:00+00:00",
        "last_attempt_at": "2026-07-24T12:05:00+00:00",
        "error_code": "provider_timeout",
    }


def test_first_failure_marks_panel_unavailable_without_exception_details() -> None:
    clock = Clock(datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc))
    cache = PanelCache(now=clock)
    cache.register("calendar")

    cache.record_failure("calendar", "auth_failed")

    assert cache.snapshot()["panels"]["calendar"] == {
        "_meta": {
            "status": "unavailable",
            "updated_at": None,
            "last_attempt_at": "2026-07-24T12:00:00+00:00",
            "error_code": "auth_failed",
        }
    }


def test_disabled_panel_has_explicit_status() -> None:
    clock = Clock(datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc))
    cache = PanelCache(now=clock)

    cache.record_disabled("home")

    assert cache.snapshot()["panels"]["home"]["_meta"]["status"] == "disabled"


def test_snapshot_is_isolated_from_caller_mutation() -> None:
    clock = Clock(datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc))
    cache = PanelCache(now=clock)
    cache.record_success("tasks", {"items": [{"title": "Ship"}]})

    first = cache.snapshot()
    first["panels"]["tasks"]["items"][0]["title"] = "Mutated"

    assert cache.snapshot()["panels"]["tasks"]["items"][0]["title"] == "Ship"


def test_panel_names_must_be_safe_identifiers() -> None:
    cache = PanelCache()

    try:
        cache.register("../../secret")
    except ValueError as error:
        assert str(error) == "invalid panel name"
    else:
        raise AssertionError("unsafe panel name accepted")
