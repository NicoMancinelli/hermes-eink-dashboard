from __future__ import annotations

import copy
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

PanelData = dict[str, Any]
_PANEL_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class PanelMeta:
    status: str
    updated_at: str | None = None
    last_attempt_at: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "status": self.status,
            "updated_at": self.updated_at,
            "last_attempt_at": self.last_attempt_at,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class _PanelRecord:
    meta: PanelMeta
    data: PanelData = field(default_factory=dict)


class PanelCache:
    """Thread-safe copy-on-write storage for independently refreshed panels."""

    schema_version = 1

    def __init__(self, now: Callable[[], datetime] | None = None):
        self._now = now or (lambda: datetime.now(tz=timezone.utc))
        self._lock = threading.RLock()
        self._panels: dict[str, _PanelRecord] = {}

    @staticmethod
    def _validate_name(name: str) -> None:
        if not _PANEL_NAME.fullmatch(name):
            raise ValueError("invalid panel name")

    def _timestamp(self) -> str:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    def register(self, name: str) -> None:
        self._validate_name(name)
        with self._lock:
            self._panels.setdefault(name, _PanelRecord(PanelMeta(status="unavailable")))

    def record_success(self, name: str, data: PanelData) -> None:
        self._validate_name(name)
        if "_meta" in data:
            raise ValueError("panel data cannot define _meta")
        attempted_at = self._timestamp()
        record = _PanelRecord(
            meta=PanelMeta(
                status="ok",
                updated_at=attempted_at,
                last_attempt_at=attempted_at,
            ),
            data=copy.deepcopy(data),
        )
        with self._lock:
            self._panels[name] = record

    def record_failure(self, name: str, error_code: str) -> None:
        self._validate_name(name)
        attempted_at = self._timestamp()
        with self._lock:
            previous = self._panels.get(name, _PanelRecord(PanelMeta(status="unavailable")))
            status = "stale" if previous.meta.updated_at is not None else "unavailable"
            self._panels[name] = _PanelRecord(
                meta=PanelMeta(
                    status=status,
                    updated_at=previous.meta.updated_at,
                    last_attempt_at=attempted_at,
                    error_code=error_code,
                ),
                data=previous.data,
            )

    def record_disabled(self, name: str) -> None:
        self._validate_name(name)
        with self._lock:
            self._panels[name] = _PanelRecord(PanelMeta(status="disabled"))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            panels = {
                name: {"_meta": record.meta.to_dict(), **copy.deepcopy(record.data)}
                for name, record in self._panels.items()
            }
        return {
            "schema_version": self.schema_version,
            "generated_at": self._timestamp(),
            "panels": panels,
        }
