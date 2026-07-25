import pytest
from fastapi.testclient import TestClient

from hermes_kindle_dashboard.actions import ActionRegistry
from hermes_kindle_dashboard.api import ApiSettings, create_app
from hermes_kindle_dashboard.scheduler import ControlBus


@pytest.fixture
def api_setup(tmp_path):
    from hermes_kindle_dashboard.config import ConfigManager

    class _IsolatedManager(ConfigManager):
        def __init__(self):
            super().__init__(config_path=tmp_path / "config.yaml")
            # Override the template path to use the test's tmp_path instead
            # of walking up from the package.
            self._template_override = tmp_path / "config.sh.example"
        def load_template(self):
            return (
                "HOST_IP=\"HOST_IP\"\n"
                "HOST_PORT=\"9120\"\n"
                "DASHBOARD_TOKEN=\"CHANGE_ME\"\n"
                "CONTROL_TOKEN=\"\"\n"
                "REFRESH_INTERVAL=\"45\"\n"
                "DOWNLOAD_TIMEOUT=\"12\"\n"
                "FULL_REFRESH_EVERY=\"10\"\n"
                "KEEP_AWAKE=\"1\"\n"
                "STOP_FRAMEWORK=\"1\"\n"
                "FBINK=\"\"\n"
            )

    settings = ApiSettings(token="read-token-123", control_token="control-token-456")
    bus = ControlBus()
    registry = ActionRegistry(allowlist=["workflow.briefing", "refresh"], rate_limit_seconds=1.0)
    app = create_app(
        settings=settings,
        aggregators=[],
        bus=bus,
        registry=registry,
        config_manager_factory=_IsolatedManager,
    )
    client = TestClient(app)
    return client, bus, registry, settings


def test_config_get_without_existing_config(api_setup):
    client, _, _, _ = api_setup
    headers = {"Authorization": "Bearer control-token-456"}

    resp = client.get("/config", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["config"] is None
    assert "No configuration file found" in data["message"]


def test_config_post_creates_and_regenerates(api_setup):
    client, _, _, _ = api_setup
    headers = {"Authorization": "Bearer control-token-456"}

    payload = {
        "host_ip": "192.168.1.50",
        "host_port": 9120,
        "dashboard_token": "read-token-abc",
        "control_token": "control-token-xyz",
        "refresh_interval": 60,
        "download_timeout": 15,
        "full_refresh_every": 20,
        "keep_awake": 1,
        "stop_framework": 1,
        "fbink": "",
    }

    resp = client.post("/config", headers=headers, json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["config"]["host_ip"] == "192.168.1.50"
    assert data["config"]["dashboard_token"] == "read-token-abc"
    assert "config_sh_path" in data


def test_config_post_validation_error(api_setup):
    client, _, _, _ = api_setup
    headers = {"Authorization": "Bearer control-token-456"}

    # Missing required field
    payload = {
        "host_port": 9120,
        "dashboard_token": "read-token-abc",
    }

    resp = client.post("/config", headers=headers, json=payload)
    assert resp.status_code == 422


def test_config_post_invalid_host_ip(api_setup):
    client, _, _, _ = api_setup
    headers = {"Authorization": "Bearer control-token-456"}

    payload = {
        "host_ip": "invalid host!",  # Invalid characters
        "host_port": 9120,
        "dashboard_token": "read-token-abc",
    }

    resp = client.post("/config", headers=headers, json=payload)
    assert resp.status_code == 422


def test_config_post_invalid_token_format(api_setup):
    client, _, _, _ = api_setup
    headers = {"Authorization": "Bearer control-token-456"}

    payload = {
        "host_ip": "192.168.1.50",
        "host_port": 9120,
        "dashboard_token": "invalid token with spaces!",  # Invalid format
    }

    resp = client.post("/config", headers=headers, json=payload)
    assert resp.status_code == 422


def test_config_preview_endpoint(api_setup):
    client, _, _, _ = api_setup
    headers = {"Authorization": "Bearer control-token-456"}

    payload = {
        "host_ip": "10.0.0.1",
        "host_port": 9120,
        "dashboard_token": "preview-token",
        "control_token": "preview-control",
        "refresh_interval": 30,
    }

    resp = client.post("/config/preview", headers=headers, json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "preview" in data
    assert 'HOST_IP="10.0.0.1"' in data["preview"]
    assert 'DASHBOARD_TOKEN="preview-token"' in data["preview"]
    assert 'CONTROL_TOKEN="preview-control"' in data["preview"]


def test_config_example_endpoint(api_setup):
    client, _, _, _ = api_setup
    headers = {"Authorization": "Bearer control-token-456"}

    resp = client.get("/config/example", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "example_yaml" in data
    assert "host_ip" in data["example_yaml"]
    assert "dashboard_token" in data["example_yaml"]


def test_config_get_after_post(api_setup):
    client, _, _, _ = api_setup
    headers = {"Authorization": "Bearer control-token-456"}

    # First create a config
    payload = {
        "host_ip": "192.168.1.100",
        "host_port": 9120,
        "dashboard_token": "read-token-get",
        "control_token": "control-token-get",
        "refresh_interval": 45,
    }
    resp = client.post("/config", headers=headers, json=payload)
    assert resp.status_code == 200

    # Now get it back
    resp = client.get("/config", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["config"] is not None
    assert data["config"]["host_ip"] == "192.168.1.100"
    assert data["config"]["dashboard_token"] == "read-token-get"


def test_config_requires_control_token(api_setup):
    client, _, _, _ = api_setup

    # Try without control token
    resp = client.post("/config", headers={"Authorization": "Bearer read-token-123"}, json={"host_ip": "1.2.3.4", "dashboard_token": "t"})
    assert resp.status_code == 401

    # Try with no token
    resp = client.post("/config", json={"host_ip": "1.2.3.4", "dashboard_token": "t"})
    assert resp.status_code == 401


def test_config_get_requires_control_token(api_setup):
    client, _, _, _ = api_setup

    # Try without control token
    resp = client.get("/config", headers={"Authorization": "Bearer read-token-123"})
    assert resp.status_code == 401

    # Try with no token
    resp = client.get("/config")
    assert resp.status_code == 401


def test_config_preview_requires_control_token(api_setup):
    client, _, _, _ = api_setup

    payload = {"host_ip": "1.2.3.4", "dashboard_token": "t"}
    resp = client.post("/config/preview", headers={"Authorization": "Bearer read-token-123"}, json=payload)
    assert resp.status_code == 401


def test_config_example_requires_control_token(api_setup):
    client, _, _, _ = api_setup

    resp = client.get("/config/example", headers={"Authorization": "Bearer read-token-123"})
    assert resp.status_code == 401


def test_config_without_control_token_configured_returns_503():
    settings = ApiSettings(token="read-token-123", control_token="")
    app = create_app(settings=settings, aggregators=[])
    client = TestClient(app)

    resp = client.post("/config", headers={"Authorization": "Bearer read-token-123"}, json={"host_ip": "1.2.3.4", "dashboard_token": "t"})
    assert resp.status_code == 503

    resp = client.get("/config", headers={"Authorization": "Bearer read-token-123"})
    assert resp.status_code == 503

    resp = client.post("/config/preview", headers={"Authorization": "Bearer read-token-123"}, json={"host_ip": "1.2.3.4", "dashboard_token": "t"})
    assert resp.status_code == 503

    resp = client.get("/config/example", headers={"Authorization": "Bearer read-token-123"})
    assert resp.status_code == 503


def test_config_post_default_values(api_setup):
    client, _, _, _ = api_setup
    headers = {"Authorization": "Bearer control-token-456"}

    # Only required fields
    payload = {
        "host_ip": "192.168.1.77",
        "dashboard_token": "minimal-token",
    }

    resp = client.post("/config", headers=headers, json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["config"]["host_port"] == 9120
    assert data["config"]["refresh_interval"] == 45
    assert data["config"]["download_timeout"] == 12
    assert data["config"]["full_refresh_every"] == 10
    assert data["config"]["keep_awake"] == 1
    assert data["config"]["stop_framework"] == 1
    assert data["config"]["control_token"] == ""


def test_config_post_refresh_interval_bounds(api_setup):
    client, _, _, _ = api_setup
    headers = {"Authorization": "Bearer control-token-456"}

    # Too low
    payload = {"host_ip": "1.2.3.4", "dashboard_token": "t", "refresh_interval": 2}
    resp = client.post("/config", headers=headers, json=payload)
    assert resp.status_code == 422

    # Too high
    payload = {"host_ip": "1.2.3.4", "dashboard_token": "t", "refresh_interval": 5000}
    resp = client.post("/config", headers=headers, json=payload)
    assert resp.status_code == 422

    # Valid bounds
    for val in [5, 45, 3600]:
        payload = {"host_ip": "1.2.3.4", "dashboard_token": "t", "refresh_interval": val}
        resp = client.post("/config", headers=headers, json=payload)
        assert resp.status_code == 200


def test_config_post_port_bounds(api_setup):
    client, _, _, _ = api_setup
    headers = {"Authorization": "Bearer control-token-456"}

    # Too low
    payload = {"host_ip": "1.2.3.4", "dashboard_token": "t", "host_port": 0}
    resp = client.post("/config", headers=headers, json=payload)
    assert resp.status_code == 422

    # Too high
    payload = {"host_ip": "1.2.3.4", "dashboard_token": "t", "host_port": 70000}
    resp = client.post("/config", headers=headers, json=payload)
    assert resp.status_code == 422