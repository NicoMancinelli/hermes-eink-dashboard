import json
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from hermes_kindle_dashboard.aggregators.hermes import snapshot_to_panel
from hermes_kindle_dashboard.api import ApiSettings, create_app
from hermes_kindle_dashboard.server import build_parser
from test_render import sample_snapshot


class FakeHermesAggregator:
    name = "hermes"
    interval_seconds = 3600.0

    def __init__(self):
        self.calls = 0

    async def collect(self):
        self.calls += 1
        return snapshot_to_panel(sample_snapshot())


@pytest.fixture
def dashboard_client():
    aggregator = FakeHermesAggregator()
    app = create_app(
        settings=ApiSettings(token="test-token", width=600, height=800),
        aggregators=[aggregator],
    )
    with TestClient(app) as client:
        yield client, aggregator


def test_health_is_public(dashboard_client) -> None:
    client, _ = dashboard_client

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_dashboard_data_requires_bearer_auth(dashboard_client) -> None:
    client, _ = dashboard_client

    assert client.get("/dashboard-data").status_code == 401
    assert client.get("/dashboard-data?token=test-token").status_code == 401

    response = client.get(
        "/dashboard-data",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["schema_version"] == 1
    assert response.json()["panels"]["hermes"]["session"]["model"] == "gpt-5.6-sol"


def test_legacy_routes_accept_query_token_without_recollecting(dashboard_client) -> None:
    client, aggregator = dashboard_client

    state_response = client.get("/state.json?token=test-token")
    png_response = client.get("/dashboard.png?token=test-token")

    assert state_response.status_code == 200
    assert state_response.json()["session"]["model"] == "gpt-5.6-sol"
    assert "secret" not in json.dumps(state_response.json()).lower()
    assert png_response.status_code == 200
    assert png_response.headers["Content-Type"] == "image/png"
    assert png_response.headers["Cache-Control"] == "no-store"
    image = Image.open(BytesIO(png_response.content))
    assert image.size == (600, 800)
    assert image.mode == "1"
    assert aggregator.calls == 1


def test_unknown_route_is_not_found(dashboard_client) -> None:
    client, _ = dashboard_client

    response = client.get("/unknown")

    assert response.status_code == 404


def test_cli_rejects_non_positive_refresh_interval(monkeypatch) -> None:
    for value in ("0", "nan", "inf"):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--refresh-seconds", value])

    monkeypatch.setenv("HERMES_DASHBOARD_REFRESH_SECONDS", "-1")
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
