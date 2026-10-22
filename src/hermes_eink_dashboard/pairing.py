"""Device pairing for the Hermes E-Ink Dashboard.

Implements an OAuth-device-flow-style pairing handshake so a Kindle (or any
input-constrained device) can obtain its own read/control tokens without the
admin ever copying secrets by hand:

1. The device generates ``device_id`` (16 hex chars) and ``device_secret``
   (64 hex chars) locally and shows a short display code derived from the id
   (``AB12-CD34``). It polls :http:POST:`/pair/poll` with both values.
2. The admin, sitting at the host, approves it:
   ``hermes-dashboard-pair approve AB12-CD34`` which calls the authenticated
   ``POST /pair/approve`` endpoint using the control token already present on
   the host. No secret ever needs to be typed on the Kindle.
3. On approval the server mints per-device read and control tokens, persists
   them in ``devices.json`` (mode 0600), and the device's next poll receives
   them.

Security posture of the unauthenticated poll endpoint:

* A ``device_id``/``device_secret`` pair must match the sha256 hash recorded
  by the store; mismatches count toward a per-device lockout.
* Pending requests expire after :data:`PENDING_TTL_SECONDS`.
* A coarse global rate limiter bounds polling traffic.
* All error responses are stable generic codes; no store contents leak.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEVICE_ID_RE = re.compile(r"^[a-f0-9]{16}$")
DEVICE_SECRET_RE = re.compile(r"^[a-f0-9]{64}$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

PENDING_TTL_SECONDS = 30 * 60
MAX_PENDING_DEVICES = 16
MAX_FAILED_ATTEMPTS = 5
# Coarse global limiter for /pair/poll traffic (requests per minute).
POLL_RATE_LIMIT_PER_MINUTE = 240


class PairingValidationError(ValueError):
    """Raised when client-supplied pairing fields fail validation."""


@dataclass
class DeviceRecord:
    device_id: str
    device_secret_hash: str
    name: str
    status: str  # "pending" | "approved"
    requested_at: str  # ISO-8601 UTC
    approved_at: str = ""
    read_token: str = ""
    control_token: str = ""
    failed_attempts: int = 0
    last_seen_at: str = ""

    def public_view(self) -> dict[str, Any]:
        """Admin-facing view; never includes secrets or tokens."""
        return {
            "device_id": self.device_id,
            "display_code": display_code_for(self.device_id),
            "name": self.name,
            "status": self.status,
            "requested_at": self.requested_at,
            "approved_at": self.approved_at,
        }


def utc_now_iso() -> str:
    time_struct = time.gmtime()
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time_struct)


def display_code_for(device_id: str) -> str:
    canonical = device_id[:8].upper()
    return f"{canonical[:4]}-{canonical[4:]}"


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_device_credentials(device_id: Any, device_secret: Any) -> tuple[str, str]:
    if not isinstance(device_id, str) or not DEVICE_ID_RE.fullmatch(device_id):
        raise PairingValidationError("invalid_device_id")
    if not isinstance(device_secret, str) or not DEVICE_SECRET_RE.fullmatch(device_secret):
        raise PairingValidationError("invalid_device_secret")
    return device_id, device_secret


def sanitize_name(name: Any, default: str = "kindle") -> str:
    if name is None or name == "":
        return default
    if isinstance(name, str) and NAME_RE.fullmatch(name):
        return name
    raise PairingValidationError("invalid_name")


@dataclass
class PollResult:
    status: str  # "pending" | "approved" | "forbidden" | "rate_limited"
    read_token: str = ""
    control_token: str = ""
    name: str = ""


class DeviceStore:
    """JSON-file-backed registry of paired and pending devices."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ io
    def _load(self) -> dict[str, DeviceRecord]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        devices: dict[str, DeviceRecord] = {}
        now_iso = utc_now_iso()
        for entry in payload.get("devices", []) if isinstance(payload, dict) else []:
            try:
                record = DeviceRecord(
                    device_id=str(entry["device_id"]),
                    device_secret_hash=str(entry["device_secret_hash"]),
                    name=str(entry.get("name", "kindle")),
                    status=str(entry.get("status", "pending")),
                    requested_at=str(entry.get("requested_at", now_iso)),
                    approved_at=str(entry.get("approved_at", "")),
                    read_token=str(entry.get("read_token", "")),
                    control_token=str(entry.get("control_token", "")),
                    failed_attempts=int(entry.get("failed_attempts", 0)),
                    last_seen_at=str(entry.get("last_seen_at", "")),
                )
            except (KeyError, TypeError, ValueError):
                continue
            # Drop stale pending entries; keep approved ones until revoked.
            if record.status == "pending":
                requested_epoch = _parse_iso(record.requested_at)
                if requested_epoch is None or time.time() - requested_epoch > PENDING_TTL_SECONDS:
                    continue
            elif record.status != "approved":
                continue
            devices[record.device_id] = record
        return devices

    def _save(self, devices: dict[str, DeviceRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "devices": [
                {key: value for key, value in asdict(record).items() if value != ""}
                for record in sorted(devices.values(), key=lambda item: item.requested_at)
            ],
        }
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)

    # ------------------------------------------------------------- queries
    def list_devices(self) -> list[DeviceRecord]:
        with self._lock:
            return [record.public_view() for record in self._load().values()]

    def find_by_display_code(self, display_code: str) -> DeviceRecord | None:
        normalized = display_code.strip().upper().replace(" ", "")
        if "-" not in normalized and len(normalized) == 8:
            normalized = f"{normalized[:4]}-{normalized[4:]}"
        with self._lock:
            for record in self._load().values():
                if display_code_for(record.device_id) == normalized:
                    return record
        return None

    # ------------------------------------------------------------ mutation
    def register(
        self,
        device_id: str,
        device_secret: str,
        name: str,
        *,
        mint_tokens: bool = False,
    ) -> PollResult:
        """Register a device or report its approval status.

        Returns the poll result for the caller. Wrong secrets increment a
        per-device failure counter that eventually locks the request out.
        """
        with self._lock:
            devices = self._load()
            record = devices.get(device_id)
            if record is None:
                if len([item for item in devices.values() if item.status == "pending"]) >= MAX_PENDING_DEVICES:
                    return PollResult(status="rate_limited")
                devices[device_id] = DeviceRecord(
                    device_id=device_id,
                    device_secret_hash=_sha256_hex(device_secret),
                    name=name,
                    status="pending",
                    requested_at=utc_now_iso(),
                )
                self._save(devices)
                return PollResult(status="pending")
            if not hmac.compare_digest(record.device_secret_hash, _sha256_hex(device_secret)):
                record.failed_attempts += 1
                if record.failed_attempts >= MAX_FAILED_ATTEMPTS:
                    del devices[device_id]
                self._save(devices)
                return PollResult(status="forbidden")
            if record.status != "approved":
                return PollResult(status="pending")
            return PollResult(
                status="approved",
                read_token=record.read_token,
                control_token=record.control_token,
                name=record.name,
            )

    def approve(self, device_ref: str, *, mint: Any = None) -> DeviceRecord | None:
        """Approve by device_id or display code; mint per-device tokens."""
        import secrets as _secrets

        del mint  # reserved for test injection
        with self._lock:
            devices = self._load()
            record = devices.get(device_ref)
            if record is None:
                wanted = device_ref.strip().upper().replace(" ", "")
                if "-" not in wanted and len(wanted) == 8:
                    wanted = f"{wanted[:4]}-{wanted[4:]}"
                for candidate in devices.values():
                    if display_code_for(candidate.device_id) == wanted:
                        record = candidate
                        break
            if record is None or record.status == "denied":
                return None
            if record.status != "approved":
                record.status = "approved"
                record.approved_at = utc_now_iso()
                record.read_token = _secrets.token_urlsafe(32)
                record.control_token = _secrets.token_urlsafe(32)
                record.failed_attempts = 0
                devices[record.device_id] = record
                self._save(devices)
            return record

    def deny(self, device_ref: str) -> bool:
        with self._lock:
            devices = self._load()
            record = devices.get(device_ref)
            if record is None:
                wanted = device_ref.strip().upper().replace(" ", "")
                if "-" not in wanted and len(wanted) == 8:
                    wanted = f"{wanted[:4]}-{wanted[4:]}"
                for candidate in devices.values():
                    if display_code_for(candidate.device_id) == wanted:
                        record = candidate
                        break
            if record is None:
                return False
            del devices[record.device_id]
            self._save(devices)
            return True


def _parse_iso(value: str) -> float | None:
    import calendar

    try:
        parsed = time.strptime(value, "%Y-%m-%dT%H:%M:%S+00:00")
    except ValueError:
        try:
            parsed = time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    return calendar.timegm(parsed)


class PairingService:
    """Thin stateless façade combining validation, rate limiting and store."""

    def __init__(self, store: DeviceStore) -> None:
        self.store = store
        self._poll_times: deque[float] = deque()
        self._lock = threading.Lock()

    def poll(self, device_id: Any, device_secret: Any, name: Any = None) -> PollResult:
        validated_id, validated_secret = validate_device_credentials(device_id, device_secret)
        safe_name = sanitize_name(name)
        with self._lock:
            now = time.time()
            while self._poll_times and now - self._poll_times[0] > 60:
                self._poll_times.popleft()
            if len(self._poll_times) >= POLL_RATE_LIMIT_PER_MINUTE:
                return PollResult(status="rate_limited")
            self._poll_times.append(now)
        return self.store.register(validated_id, validated_secret, safe_name)

    def approve(self, device_ref: str) -> DeviceRecord | None:
        if not isinstance(device_ref, str) or not device_ref.strip():
            raise PairingValidationError("invalid_device_reference")
        return self.store.approve(device_ref.strip())

    def deny(self, device_ref: str) -> bool:
        if not isinstance(device_ref, str) or not device_ref.strip():
            raise PairingValidationError("invalid_device_reference")
        return self.store.deny(device_ref.strip())

    def list_devices(self) -> list[dict[str, Any]]:
        return self.store.list_devices()
