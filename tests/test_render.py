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
