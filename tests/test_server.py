import json
import threading
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from PIL import Image

from hermes_kindle_dashboard.server import DashboardApplication, ServerSettings, create_server
from test_render import sample_snapshot


class FakeCollector:
    def __init__(self):
        self.calls = 0

    def collect(self):
        self.calls += 1
        return sample_snapshot()


@pytest.fixture
def dashboard_server():
    collector = FakeCollector()
    app = DashboardApplication(
        collector=collector,
        settings=ServerSettings(token="test-token", width=600, height=800, cache_seconds=60),
    )
    server = create_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", collector
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_health_is_public(dashboard_server) -> None:
    base, _ = dashboard_server
    with urlopen(f"{base}/healthz", timeout=2) as response:
        assert response.status == 200
        assert json.load(response)["status"] == "ok"


def test_dashboard_requires_token_and_returns_png(dashboard_server) -> None:
    base, collector = dashboard_server
    with pytest.raises(HTTPError) as error:
        urlopen(f"{base}/dashboard.png", timeout=2)
    assert error.value.code == 401

    with urlopen(f"{base}/dashboard.png?token=test-token", timeout=2) as response:
        body = response.read()
        assert response.headers["Content-Type"] == "image/png"
        assert response.headers["Cache-Control"] == "no-store"

    image = Image.open(BytesIO(body))
    assert image.size == (600, 800)
    assert image.mode == "1"
    assert collector.calls == 1


def test_state_supports_bearer_auth_and_never_returns_raw_tool_output(dashboard_server) -> None:
    base, _ = dashboard_server
    request = Request(f"{base}/state.json", headers={"Authorization": "Bearer test-token"})
    with urlopen(request, timeout=2) as response:
        state = json.load(response)

    assert state["session"]["model"] == "gpt-5.6-sol"
    assert "recent_events" in state
    assert "secret" not in json.dumps(state).lower()
