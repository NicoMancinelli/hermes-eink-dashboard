from datetime import datetime, timezone

from PIL import Image

from hermes_kindle_dashboard.render import RenderOptions, render_dashboard
from hermes_kindle_dashboard.state import (
    DashboardSnapshot,
    MemoryState,
    SessionState,
    TaskState,
)


def sample_snapshot() -> DashboardSnapshot:
    return DashboardSnapshot(
        generated_at=datetime.now(tz=timezone.utc).isoformat(),
        session=SessionState(
            id="session-1",
            source="tui",
            model="gpt-5.6-sol",
            title="Build the Kindle dashboard with a readable responsive layout",
            status="working",
            current_tool="terminal",
            input_tokens=131072,
            output_tokens=4096,
            context_limit=262144,
            context_percent=50,
            message_count=80,
            tool_call_count=24,
        ),
        tasks=(
            TaskState("Render a crisp monochrome PNG", "in_progress", "session"),
            TaskState("Install the KUAL extension", "pending", "session"),
            TaskState("Verify the endpoint from Wi-Fi", "pending", "kanban"),
        ),
        kanban_active=1,
        memory=MemoryState(fact_count=185, average_trust=0.82, retrieved_facts=90, profile_chars=10745),
        recent_events=(
            "gpt-5.6-sol via openai-codex · 131072 in / 1024 out · 12.4s",
            "tool terminal completed",
        ),
    )


def test_render_is_exact_size_and_one_bit() -> None:
    image = render_dashboard(sample_snapshot(), RenderOptions(width=600, height=800, bit_depth=1))

    assert image.size == (600, 800)
    assert image.mode == "1"
    assert image.getbbox() is not None
    assert image.getextrema() == (0, 255)


def test_render_can_save_png(tmp_path) -> None:
    output = tmp_path / "dashboard.png"
    image = render_dashboard(sample_snapshot(), RenderOptions(width=1072, height=1448, bit_depth=1))
    image.save(output, format="PNG", optimize=True)

    reopened = Image.open(output)
    assert reopened.size == (1072, 1448)
    assert reopened.mode == "1"


def test_build_default_layout() -> None:
    from hermes_kindle_dashboard.contract import build_default_layout

    layout = build_default_layout(1072, 1448, panels=("hermes",))
    assert layout["schema_version"] == 2
    assert layout["layout"]["columns"] == 4
    assert layout["layout"]["rows"] == 6
    assert layout["layout"]["grid_size"] == [1072, 1448]

    tiles = layout["tiles"]
    assert len(tiles) == 17  # 16 action tiles in top 4 rows + 1 hermes panel tile at bottom
    hermes_tile = [t for t in tiles if t["id"] == "panel:hermes"][0]
    assert hermes_tile["col"] == 0
    assert hermes_tile["row"] == 4
    assert hermes_tile["w"] == 4
    assert hermes_tile["h"] == 2
    assert hermes_tile["kind"] == "panel"
    assert hermes_tile["panel"] == "hermes"

    # Top action tiles cover placeholder actions
    actions = [t["action"] for t in tiles if t["kind"] == "action"]
    assert "workflow.refresh" in actions
    assert "alert.dismiss.test" in actions
    assert "context.set" in actions
    assert layout["focus"]["tile_id"] == tiles[0]["id"]


def test_render_layout_dashboard_focus_border_presence() -> None:
    import hashlib
    from hermes_kindle_dashboard.contract import build_default_layout
    from hermes_kindle_dashboard.render import render_layout_dashboard

    layout = build_default_layout(1072, 1448)
    snapshot = sample_snapshot()

    # Focus on first tile vs second tile
    layout1 = dict(layout)
    layout1["focus"] = {"tile_id": layout["tiles"][0]["id"], "x": 0, "y": 0}
    img1 = render_layout_dashboard(layout1, snapshot, RenderOptions(width=1072, height=1448))

    layout2 = dict(layout)
    layout2["focus"] = {"tile_id": layout["tiles"][1]["id"], "x": 1, "y": 0}
    img2 = render_layout_dashboard(layout2, snapshot, RenderOptions(width=1072, height=1448))

    hash1 = hashlib.sha256(img1.tobytes()).hexdigest()
    hash2 = hashlib.sha256(img2.tobytes()).hexdigest()

    assert img1.size == (1072, 1448)
    assert img1.mode == "1"
    assert hash1 != hash2, "Focus tile change must produce different deterministic PNG hashes"


def test_render_layout_dashboard_label_and_panel_preview() -> None:
    import hashlib
    from hermes_kindle_dashboard.contract import build_default_layout
    from hermes_kindle_dashboard.render import render_layout_dashboard

    layout = build_default_layout(1072, 1448)
    snapshot = sample_snapshot()

    img = render_layout_dashboard(layout, snapshot, RenderOptions(width=1072, height=1448))
    img_hash = hashlib.sha256(img.tobytes()).hexdigest()

    assert img.size == (1072, 1448)
    assert img.mode == "1"
    # Deterministic hash check: ensure image is populated and hash is stable
    img2 = render_layout_dashboard(layout, snapshot, RenderOptions(width=1072, height=1448))
    assert hashlib.sha256(img2.tobytes()).hexdigest() == img_hash


def test_parse_layout_yaml() -> None:
    from hermes_kindle_dashboard.server import parse_layout_yaml

    yaml_text = """
columns: 4
rows: 6
grid_size: [1072, 1448]
tiles:
  - id: wf:refresh
    label: Refresh
    col: 0
    row: 0
    w: 1
    h: 1
    kind: action
    action: workflow.refresh
  - id: panel:hermes
    label: Hermes Panel
    col: 0
    row: 4
    w: 4
    h: 2
    kind: panel
    panel: hermes
"""
    layout = parse_layout_yaml(yaml_text)
    assert layout["columns"] == 4
    assert layout["rows"] == 6
    assert layout["grid_size"] == [1072, 1448]
    assert len(layout["tiles"]) == 2
    assert layout["tiles"][0]["id"] == "wf:refresh"
    assert layout["tiles"][1]["label"] == "Hermes Panel"


def test_render_layout_dashboard_label_variations() -> None:
    import hashlib
    from hermes_kindle_dashboard.contract import Tile, dashboard_json
    from hermes_kindle_dashboard.render import render_layout_dashboard

    layout1 = dashboard_json({
        "columns": 2,
        "rows": 2,
        "tiles": [Tile(id="t1", label="Action Alpha", col=0, row=0, kind="action")]
    })
    layout2 = dashboard_json({
        "columns": 2,
        "rows": 2,
        "tiles": [Tile(id="t1", label="Action Beta", col=0, row=0, kind="action")]
    })

    img1 = render_layout_dashboard(layout1)
    img2 = render_layout_dashboard(layout2)

    hash1 = hashlib.sha256(img1.tobytes()).hexdigest()
    hash2 = hashlib.sha256(img2.tobytes()).hexdigest()

    assert hash1 != hash2, "Different tile labels must alter rendered PNG image hash"


def test_render_layout_dashboard_panel_preview_with_and_without_snapshot() -> None:
    import hashlib
    from hermes_kindle_dashboard.contract import Tile, dashboard_json
    from hermes_kindle_dashboard.render import render_layout_dashboard

    layout = dashboard_json({
        "columns": 2,
        "rows": 2,
        "tiles": [Tile(id="p1", label="Hermes", col=0, row=0, w=2, h=2, kind="panel", panel="hermes")]
    })

    img_no_snap = render_layout_dashboard(layout, snapshot=None)
    img_snap = render_layout_dashboard(layout, snapshot=sample_snapshot())

    hash_no_snap = hashlib.sha256(img_no_snap.tobytes()).hexdigest()
    hash_snap = hashlib.sha256(img_snap.tobytes()).hexdigest()

    assert hash_no_snap != hash_snap, "Panel preview with snapshot data must differ from panel preview without snapshot"


