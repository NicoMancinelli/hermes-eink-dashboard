import time
import pytest
from fastapi.testclient import TestClient

from hermes_kindle_dashboard.actions import ActionRegistry
from hermes_kindle_dashboard.api import ApiSettings, create_app
from hermes_kindle_dashboard.scheduler import ControlBus


@pytest.fixture
def api_setup():
    settings = ApiSettings(token="read-token-123", control_token="control-token-456")
    bus = ControlBus()
    registry = ActionRegistry(allowlist=["workflow.briefing", "refresh"], rate_limit_seconds=1.0)
    app = create_app(settings=settings, aggregators=[], bus=bus, registry=registry)
    client = TestClient(app)
    return client, bus, registry, settings


def test_control_auth_failures(api_setup) -> None:
    client, _, _, _ = api_setup

    # Missing token -> 401
    resp = client.post("/control", json={"action": "refresh", "tile_id": "t1", "nonce": "n1", "ts": time.time()})
    assert resp.status_code == 401

    # Wrong token -> 401
    resp = client.post(
        "/control",
        headers={"Authorization": "Bearer wrong-token"},
        json={"action": "refresh", "tile_id": "t1", "nonce": "n1", "ts": time.time()},
    )
    assert resp.status_code == 401


def test_control_action_errors(api_setup) -> None:
    client, _, registry, _ = api_setup
    headers = {"Authorization": "Bearer control-token-456"}
    now = time.time()

    # Unknown action -> 403
    resp = client.post(
        "/control",
        headers=headers,
        json={"action": "forbidden.action", "tile_id": "t1", "nonce": "n1", "ts": now},
    )
    assert resp.status_code == 403

    # Bad timestamp -> 400
    resp = client.post(
        "/control",
        headers=headers,
        json={"action": "refresh", "tile_id": "t1", "nonce": "n2", "ts": now + 100.0},
    )
    assert resp.status_code == 400

    # Success first time
    resp = client.post(
        "/control",
        headers=headers,
        json={"action": "workflow.briefing", "tile_id": "t1", "nonce": "nonce-unique", "ts": now},
    )
    assert resp.status_code == 200

    # Rate limit -> 429
    resp = client.post(
        "/control",
        headers=headers,
        json={"action": "workflow.briefing", "tile_id": "t1", "nonce": "nonce-2", "ts": now},
    )
    assert resp.status_code == 429

    # Duplicate nonce -> 400
    registry._default_rate_limit = 0  # bypass rate limit for nonce test
    resp = client.post(
        "/control",
        headers=headers,
        json={"action": "refresh", "tile_id": "t1", "nonce": "nonce-unique", "ts": now},
    )
    assert resp.status_code == 400


def test_dashboard_json_endpoint(api_setup) -> None:
    client, _, _, _ = api_setup

    # Unauthorized without read token -> 401
    resp = client.get("/dashboard.json")
    assert resp.status_code == 401

    # Authorized with read token -> 200 OK
    resp = client.get("/dashboard.json", headers={"Authorization": "Bearer read-token-123"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["schema_version"] == 2
    assert "layout" in data
    assert "tiles" in data


def test_control_events_endpoint_long_poll(api_setup) -> None:
    client, bus, _, _ = api_setup
    headers = {"Authorization": "Bearer control-token-456"}

    # Unauthorized without control token -> 401
    resp = client.get("/control/events")
    assert resp.status_code == 401

    # Timeout scenario -> returns 200 with {"event": None}
    resp = client.get("/control/events?timeout=0.01", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"event": None}

    # Immediate return when event exists
    bus.publish({"tile_id": "t1", "action": "refresh"})
    resp = client.get("/control/events?timeout=1.0", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"event": {"tile_id": "t1", "action": "refresh"}}
