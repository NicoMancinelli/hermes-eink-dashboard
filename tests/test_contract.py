from datetime import datetime, timezone

from hermes_eink_dashboard.contract import PanelCache


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


def test_tile_dataclass_to_dict_and_from_dict() -> None:
    from hermes_eink_dashboard.contract import Tile

    tile = Tile(
        id="wf:briefing",
        label="Morning Briefing",
        col=0,
        row=0,
        w=2,
        h=1,
        kind="action",
        action="workflow.briefing",
        state="idle",
    )
    d = tile.to_dict()
    assert d == {
        "id": "wf:briefing",
        "label": "Morning Briefing",
        "col": 0,
        "row": 0,
        "w": 2,
        "h": 1,
        "kind": "action",
        "action": "workflow.briefing",
        "state": "idle",
    }
    reconstructed = Tile.from_dict(d)
    assert reconstructed == tile


def test_dashboard_json_returns_schema_v2_payload() -> None:
    from hermes_eink_dashboard.contract import Tile, dashboard_json

    layout = {
        "columns": 4,
        "rows": 6,
        "tile_size": [240, 160],
        "grid_size": [1072, 1448],
        "tiles": [
            Tile(
                id="wf:briefing",
                label="Morning Briefing",
                col=0,
                row=0,
                w=2,
                h=1,
                kind="action",
                action="workflow.briefing",
                state="idle",
            )
        ],
    }
    res = dashboard_json(layout)
    assert res["schema_version"] == 2
    assert res["layout"]["columns"] == 4
    assert res["layout"]["rows"] == 6
    assert len(res["tiles"]) == 1
    assert res["tiles"][0]["id"] == "wf:briefing"
    assert res["focus"] == {"tile_id": "wf:briefing", "x": 0, "y": 0}

