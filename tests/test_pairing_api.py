"""Tests for the device pairing store, service and HTTP endpoints."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_kindle_dashboard.api import ApiSettings, create_app
from hermes_kindle_dashboard.pairing import (
    DeviceStore,
    PairingService,
    PollResult,
    display_code_for,
)

DEVICE_ID = "0f1e2d3c4b5a6978"
DEVICE_SECRET = "ab" * 32


@pytest.fixture
def store(tmp_path: Path) -> DeviceStore:
    return DeviceStore(tmp_path / "devices.json")


@pytest.fixture
def pairing(store: DeviceStore) -> PairingService:
    return PairingService(store)


@pytest.fixture
def client(pairing: PairingService) -> TestClient:
    settings = ApiSettings(token="read-token", control_token="control-token", pairing=pairing)
    return TestClient(create_app(settings=settings, aggregators=[]))


def _poll(client: TestClient, device_id=DEVICE_ID, secret=DEVICE_SECRET, name=None) -> "object":
    payload: dict = {"device_id": device_id, "device_secret": secret}
    if name is not None:
        payload["name"] = name
    return client.post("/pair/poll", json=payload)


class TestDeviceStore:
    def test_register_pending_then_approve_flow(self, store: DeviceStore) -> None:
        result = store.register(DEVICE_ID, DEVICE_SECRET, "kindle")
        assert result.status == "pending"
        assert result.read_token == ""

        approved = store.approve(display_code_for(DEVICE_ID))
        assert approved is not None
        assert approved.status == "approved"
        assert len(approved.read_token) >= 32
        assert len(approved.control_token) >= 32

        again = store.register(DEVICE_ID, DEVICE_SECRET, "kindle")
        assert again.status == "approved"
        assert again.read_token == approved.read_token
        assert again.control_token == approved.control_token

    def test_wrong_secret_locks_device_out(self, store: DeviceStore) -> None:
        store.register(DEVICE_ID, DEVICE_SECRET, "kindle")
        for _ in range(4):
            assert store.register(DEVICE_ID, "cd" * 32, "kindle").status == "forbidden"
        # Fifth failure removes the record entirely.
        assert store.register(DEVICE_ID, "cd" * 32, "kindle").status == "forbidden"
        # Even the correct secret no longer matches (record gone; re-registers fresh).
        assert store.register(DEVICE_ID, DEVICE_SECRET, "kindle").status == "pending"

    def test_deny_removes_record(self, store: DeviceStore) -> None:
        store.register(DEVICE_ID, DEVICE_SECRET, "kindle")
        assert store.deny(display_code_for(DEVICE_ID)) is True
        assert store.deny(display_code_for(DEVICE_ID)) is False
        assert store.list_devices() == []

    def test_stale_pending_entries_pruned_on_load(self, store: DeviceStore, tmp_path: Path) -> None:
        store.register(DEVICE_ID, DEVICE_SECRET, "kindle")
        # Age the pending entry beyond the TTL by rewriting requested_at.
        raw = json.loads((tmp_path / "devices.json").read_text())
        old = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 3600))
        raw["devices"][0]["requested_at"] = old
        (tmp_path / "devices.json").write_text(json.dumps(raw))

        reloaded = DeviceStore(tmp_path / "devices.json")
        assert reloaded.list_devices() == []

    def test_corrupt_store_file_tolerated(self, tmp_path: Path) -> None:
        path = tmp_path / "devices.json"
        path.write_text("not json at all{{{")
        assert DeviceStore(path).list_devices() == []
        # And it can still be used afterwards.
        assert DeviceStore(path).register(DEVICE_ID, DEVICE_SECRET, "kindle").status == "pending"

    def test_store_file_written_with_private_permissions(self, store: DeviceStore, tmp_path: Path) -> None:
        store.register(DEVICE_ID, DEVICE_SECRET, "kindle")
        mode = (tmp_path / "devices.json").stat().st_mode & 0o777
        assert mode == 0o600

    def test_public_view_hides_secrets(self, store: DeviceStore) -> None:
        store.register(DEVICE_ID, DEVICE_SECRET, "kindle")
        store.approve(DEVICE_ID)
        view = store.list_devices()[0]
        assert "read_token" not in view
        assert "control_token" not in view
        assert "device_secret_hash" not in view
        assert view["display_code"] == display_code_for(DEVICE_ID)

    def test_max_pending_devices_cap(self, store: DeviceStore) -> None:
        from hermes_kindle_dashboard.pairing import MAX_PENDING_DEVICES

        for index in range(MAX_PENDING_DEVICES):
            device_id = f"{index:016x}"
            assert store.register(device_id, DEVICE_SECRET, "kindle").status == "pending"
        overflow = f"{MAX_PENDING_DEVICES + 1:016x}"
        assert store.register(overflow, DEVICE_SECRET, "kindle").status == "rate_limited"


class TestPairingService:
    def test_poll_validates_credentials(self, pairing: PairingService) -> None:
        with pytest.raises(Exception):
            pairing.poll("XYZ", DEVICE_SECRET)
        with pytest.raises(Exception):
            pairing.poll(DEVICE_ID, "short")

    def test_poll_rate_limited(self, pairing: PairingService, monkeypatch: pytest.MonkeyPatch) -> None:
        from hermes_kindle_dashboard import pairing as pairing_module

        monkeypatch.setattr(pairing_module, "POLL_RATE_LIMIT_PER_MINUTE", 3)
        for _ in range(3):
            assert pairing.poll(DEVICE_ID, DEVICE_SECRET).status in {"pending", "approved"}
        assert pairing.poll(DEVICE_ID, DEVICE_SECRET).status == "rate_limited"

    def test_name_sanitized(self, pairing: PairingService) -> None:
        from hermes_kindle_dashboard.pairing import PairingValidationError

        with pytest.raises(PairingValidationError):
            pairing.poll(DEVICE_ID, DEVICE_SECRET, name="bad name!")

    def test_approve_unknown_returns_none(self, pairing: PairingService) -> None:
        assert pairing.approve("ZZZZ-ZZZZ") is None

    def test_poll_result_defaults(self) -> None:
        result = PollResult(status="pending")
        assert result.read_token == "" and result.control_token == ""


class TestPairingEndpoints:
    def test_full_pairing_flow(self, client: TestClient) -> None:
        admin = {"Authorization": "Bearer control-token"}

        # 1. Device polls before approval.
        response = _poll(client)
        assert response.status_code == 200
        assert response.json() == {"status": "pending"}

        # 2. Admin lists devices and sees the pending request.
        listing = client.get("/pair/devices", headers=admin)
        assert listing.status_code == 200
        devices = listing.json()["devices"]
        assert len(devices) == 1
        assert devices[0]["display_code"] == display_code_for(DEVICE_ID)
        assert devices[0]["status"] == "pending"

        # 3. Admin approves by display code.
        approve = client.post("/pair/approve", headers=admin, json={"device": display_code_for(DEVICE_ID)})
        assert approve.status_code == 200

        # 4. Device polls again and receives its tokens.
        final = _poll(client).json()
        assert final["status"] == "approved"
        assert final["read_token"]
        assert final["control_token"]
        assert final["device_name"] == "kindle"

    def test_pair_requires_control_auth(self, client: TestClient) -> None:
        assert client.get("/pair/devices").status_code == 401
        assert (
            client.post("/pair/approve", headers={"Authorization": "Bearer read-nope"}, json={"device": "X"})
            .status_code
            == 401
        )
        assert _poll(client).status_code != 401  # poll itself is unauthenticated by design

    def test_pair_endpoints_503_when_disabled(self) -> None:
        settings = ApiSettings(token="t", control_token="c", pairing=None)
        client = TestClient(create_app(settings=settings, aggregators=[]))
        assert client.get("/pair/devices", headers={"Authorization": "Bearer c"}).status_code == 503
        assert _poll(client).status_code == 503

    def test_poll_invalid_payloads(self, client: TestClient) -> None:
        assert client.post("/pair/poll", json={"device_id": DEVICE_ID}).status_code == 400
        assert client.post("/pair/poll", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400
        assert client.post("/pair/poll", json=[1, 2]).status_code == 400

    def test_approve_unknown_device_404(self, client: TestClient) -> None:
        response = client.post(
            "/pair/approve",
            headers={"Authorization": "Bearer control-token"},
            json={"device": "0000-0000"},
        )
        assert response.status_code == 404

    def test_denied_device_cannot_reclaim(self, client: TestClient) -> None:
        admin = {"Authorization": "Bearer control-token"}
        _poll(client)
        assert client.post("/pair/deny", headers=admin, json={"device": display_code_for(DEVICE_ID)}).status_code == 200
        # Record removed; a new poll registers a fresh pending request.
        assert _poll(client).json() == {"status": "pending"}


class TestPairCli:
    def test_missing_control_token_file_exits_1(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        from hermes_kindle_dashboard.pair_cli import main as cli_main

        assert cli_main(["list", "--control-token-file", str(tmp_path / "absent")]) == 1
        assert "no control token" in capsys.readouterr().err

    def test_approve_requires_device_argument(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        from hermes_kindle_dashboard.pair_cli import main as cli_main

        assert cli_main(["approve", "--control-token-file", str(tmp_path / "absent")]) == 2
        assert "requires a display code" in capsys.readouterr().err
