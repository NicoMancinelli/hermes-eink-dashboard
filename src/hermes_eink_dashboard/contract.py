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


@dataclass(frozen=True)
class Tile:
    id: str
    label: str
    col: int
    row: int
    w: int = 1
    h: int = 1
    kind: str = "action"
    action: str | None = None
    state: str | None = None
    panel: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "col": self.col,
            "row": self.row,
            "w": self.w,
            "h": self.h,
            "kind": self.kind,
        }
        if self.action is not None:
            data["action"] = self.action
        if self.state is not None:
            data["state"] = self.state
        if self.panel is not None:
            data["panel"] = self.panel
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Tile:
        return cls(
            id=data["id"],
            label=data["label"],
            col=data["col"],
            row=data["row"],
            w=data.get("w", 1),
            h=data.get("h", 1),
            kind=data.get("kind", "action"),
            action=data.get("action"),
            state=data.get("state"),
            panel=data.get("panel"),
        )


def dashboard_json(
    layout: dict[str, Any] | None = None,
    focus_tile_id: str | None = None,
) -> dict[str, Any]:
    if layout is None:
        layout = {}
    cols = layout.get("columns", 4)
    rows = layout.get("rows", 6)
    tile_size = layout.get("tile_size", [240, 160])
    grid_size = layout.get("grid_size", [1072, 1448])

    raw_tiles = layout.get("tiles", [])
    tiles: list[dict[str, Any]] = []
    tile_objs: list[Tile] = []
    for item in raw_tiles:
        if isinstance(item, Tile):
            tile_objs.append(item)
            tiles.append(item.to_dict())
        elif isinstance(item, dict):
            tile_objs.append(Tile.from_dict(item))
            tiles.append(item)

    focus_dict = None
    target_id = focus_tile_id or (layout.get("focus", {}).get("tile_id") if isinstance(layout.get("focus"), dict) else None)
    if target_id:
        for t in tile_objs:
            if t.id == target_id:
                focus_dict = {"tile_id": t.id, "x": t.col, "y": t.row}
                break
    if focus_dict is None and tile_objs:
        first = tile_objs[0]
        focus_dict = {"tile_id": first.id, "x": first.col, "y": first.row}

    return {
        "schema_version": 2,
        "layout": {
            "columns": cols,
            "rows": rows,
            "tile_size": tile_size,
            "grid_size": grid_size,
        },
        "tiles": tiles,
        "focus": focus_dict,
    }


def build_default_layout(
    width: int = 1072,
    height: int = 1448,
    panels: tuple[str, ...] = ("hermes",),
    control_tiles: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    cols = 4
    rows = 6
    placeholders = (
        ("wf:refresh", "Refresh", "workflow.refresh"),
        ("alert:dismiss_test", "Dismiss Test", "alert.dismiss.test"),
        ("context:set", "Set Context", "context.set"),
    )
    tiles: list[Tile] = []
    slot = 0
    # Real, host-configured control tiles (Hermes prompts/models) come first
    # so they occupy the top-left of the grid; the remaining slots fall back
    # to the generic placeholder actions.
    for descriptor in control_tiles or []:
        if slot >= 16:
            break
        r, c = divmod(slot, cols)
        tiles.append(
            Tile(
                id=str(descriptor["id"]),
                label=str(descriptor["label"]),
                col=c,
                row=r,
                w=1,
                h=1,
                kind="action",
                action=str(descriptor["action"]),
            )
        )
        slot += 1
    while slot < 16:
        r, c = divmod(slot, cols)
        idx = slot
        base_id, label, action = placeholders[idx % len(placeholders)]
        tile_id = base_id if idx < len(placeholders) else f"{base_id}_{idx}"
        tiles.append(
            Tile(
                id=tile_id,
                label=label,
                col=c,
                row=r,
                w=1,
                h=1,
                kind="action",
                action=action,
            )
        )
        slot += 1

    show_prompt_panel = "prompt_response" in panels
    hermes_row = 4
    hermes_h = 1 if show_prompt_panel else 2
    if "hermes" in panels:
        tiles.append(
            Tile(
                id="panel:hermes",
                label="Hermes",
                col=0,
                row=hermes_row,
                w=4,
                h=hermes_h,
                kind="panel",
                panel="hermes",
            )
        )
    if show_prompt_panel:
        # Last Hermes quick-prompt answer, so tapping an Ask tile closes the
        # loop on the device instead of leaving the reply host-side only.
        tiles.append(
            Tile(
                id="panel:prompt_response",
                label="Ask Hermes",
                col=0,
                row=hermes_row + hermes_h,
                w=4,
                h=rows - hermes_row - hermes_h,
                kind="panel",
                panel="prompt_response",
            )
        )

    layout_spec = {
        "columns": cols,
        "rows": rows,
        "tile_size": [width // cols, height // rows],
        "grid_size": [width, height],
        "tiles": tiles,
    }
    return dashboard_json(layout_spec)


