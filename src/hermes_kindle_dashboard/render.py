from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .state import DashboardSnapshot

FontType = ImageFont.FreeTypeFont | ImageFont.ImageFont


@dataclass(frozen=True)
class RenderOptions:
    width: int = 1072
    height: int = 1448
    bit_depth: int = 1
    title: str = "BENDER / HERMES"


_FONT_REGULAR = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
)
_FONT_BOLD = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
)


def _font(size: int, bold: bool = False) -> FontType:
    candidates = _FONT_BOLD if bold else _FONT_REGULAR
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), max(8, size))
    return ImageFont.load_default()


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font: FontType, width: int | float) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    value = text
    while value and draw.textlength(value + "…", font=font) > width:
        value = value[:-1]
    return value.rstrip() + "…"


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: FontType,
    width: int,
    max_lines: int,
) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if draw.textlength(candidate, font=font) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and words:
        joined = " ".join(lines)
        if len(joined.split()) < len(words):
            lines[-1] = _fit_text(draw, lines[-1] + " …", font, width)
    return lines or [""]


def _card(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], radius: int, line: int) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=255, outline=0, width=line)


def _label(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font: FontType) -> None:
    draw.text(xy, text.upper(), fill=0, font=font)


def render_dashboard(snapshot: DashboardSnapshot, options: RenderOptions | None = None) -> Image.Image:
    opts = options or RenderOptions()
    if opts.width < 320 or opts.height < 480:
        raise ValueError("dashboard dimensions must be at least 320x480")

    width, height = opts.width, opts.height
    scale = min(width / 600, height / 800)
    px = lambda value: max(1, round(value * scale))
    margin = px(18)
    gap = px(10)
    line = max(2, px(1.5))
    radius = px(8)

    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)

    title_font = _font(px(28), bold=True)
    headline_font = _font(px(17), bold=True)
    body_font = _font(px(13), bold=False)
    body_bold = _font(px(13), bold=True)
    small_font = _font(px(10), bold=True)
    tiny_font = _font(px(9), bold=False)
    event_font = _font(px(10), bold=False)

    header_h = px(74)
    draw.rectangle((0, 0, width, header_h), fill=0)
    draw.text((margin, px(14)), opts.title, fill=255, font=title_font)
    try:
        updated = datetime.fromisoformat(snapshot.generated_at.replace("Z", "+00:00"))
        stamp = updated.strftime("%H:%M UTC")
    except ValueError:
        stamp = "LIVE"
    stamp_width = draw.textlength(stamp, font=small_font)
    draw.text((width - margin - stamp_width, px(26)), stamp, fill=255, font=small_font)

    content_top = header_h + gap
    content_bottom = height - margin
    available = content_bottom - content_top - gap * 3
    status_h = round(available * 0.25)
    tasks_h = round(available * 0.34)
    memory_h = round(available * 0.16)
    activity_h = available - status_h - tasks_h - memory_h

    status_box = (margin, content_top, width - margin, content_top + status_h)
    tasks_top = status_box[3] + gap
    tasks_box = (margin, tasks_top, width - margin, tasks_top + tasks_h)
    memory_top = tasks_box[3] + gap
    memory_box = (margin, memory_top, width - margin, memory_top + memory_h)
    activity_top = memory_box[3] + gap
    activity_box = (margin, activity_top, width - margin, content_bottom)

    # Current work card
    _card(draw, status_box, radius, line)
    x = status_box[0] + px(14)
    y = status_box[1] + px(10)
    indicator = px(12)
    draw.ellipse((x, y + px(2), x + indicator, y + px(2) + indicator), fill=0 if snapshot.session.status == "working" else 255, outline=0, width=line)
    _label(draw, (x + indicator + px(8), y), f"{snapshot.session.status} · {snapshot.session.source}", small_font)
    model = _fit_text(draw, snapshot.session.model, body_bold, status_box[2] - x - px(20))
    model_w = draw.textlength(model, font=body_bold)
    draw.text((status_box[2] - px(14) - model_w, y), model, fill=0, font=body_bold)

    y += px(25)
    title_width = status_box[2] - status_box[0] - px(28)
    for text_line in _wrap_text(draw, snapshot.session.title, headline_font, title_width, 2):
        draw.text((x, y), text_line, fill=0, font=headline_font)
        y += px(21)

    tool_text = f"NOW: {snapshot.session.current_tool}    MESSAGES: {snapshot.session.message_count}    TOOLS: {snapshot.session.tool_call_count}"
    draw.text((x, status_box[3] - px(56)), _fit_text(draw, tool_text, tiny_font, title_width), fill=0, font=tiny_font)
    bar_y = status_box[3] - px(32)
    bar_h = px(12)
    bar_w = title_width
    draw.rectangle((x, bar_y, x + bar_w, bar_y + bar_h), outline=0, width=line)
    fill_w = round((bar_w - line * 2) * snapshot.session.context_percent / 100)
    if fill_w > 0:
        draw.rectangle((x + line, bar_y + line, x + line + fill_w, bar_y + bar_h - line), fill=0)
    context_text = f"CONTEXT {snapshot.session.context_percent}%  ·  {snapshot.session.input_tokens:,} / {snapshot.session.context_limit:,}"
    text_w = draw.textlength(context_text, font=small_font)
    draw.rectangle((x + px(8), bar_y - px(2), x + px(18) + text_w, bar_y + bar_h + px(2)), fill=255)
    draw.text((x + px(13), bar_y - px(1)), context_text, fill=0, font=small_font)

    # Tasks card
    _card(draw, tasks_box, radius, line)
    x = tasks_box[0] + px(14)
    y = tasks_box[1] + px(10)
    _label(draw, (x, y), f"Active tasks · {len(snapshot.tasks)} shown · {snapshot.kanban_active} kanban", small_font)
    y += px(25)
    task_line_h = max(px(29), round((tasks_box[3] - y - px(8)) / max(1, min(6, len(snapshot.tasks) or 1))))
    if not snapshot.tasks:
        draw.text((x, y), "No active tasks", fill=0, font=body_font)
    for task in snapshot.tasks[:6]:
        box_size = px(12)
        draw.rectangle((x, y + px(2), x + box_size, y + px(2) + box_size), outline=0, width=line)
        if task.status == "in_progress":
            draw.rectangle((x + px(3), y + px(5), x + box_size - px(3), y + box_size - px(1)), fill=0)
        status = "NOW" if task.status == "in_progress" else "NEXT"
        prefix = f"{status}  "
        draw.text((x + box_size + px(8), y), prefix, fill=0, font=body_bold)
        prefix_w = draw.textlength(prefix, font=body_bold)
        remaining = tasks_box[2] - (x + box_size + px(8) + prefix_w) - px(12)
        draw.text(
            (x + box_size + px(8) + prefix_w, y),
            _fit_text(draw, task.title, body_font, remaining),
            fill=0,
            font=body_font,
        )
        y += task_line_h

    # Memory card
    _card(draw, memory_box, radius, line)
    x = memory_box[0] + px(14)
    y = memory_box[1] + px(10)
    _label(draw, (x, y), "Memory", small_font)
    y += px(25)
    metrics = (
        (str(snapshot.memory.fact_count), "FACTS"),
        (f"{snapshot.memory.average_trust:.2f}", "TRUST"),
        (str(snapshot.memory.retrieved_facts), "USED"),
        (f"{snapshot.memory.profile_chars / 1000:.1f}K", "PROFILE"),
    )
    cell_w = (memory_box[2] - memory_box[0] - px(28)) // len(metrics)
    value_font = _font(px(21), bold=True)
    for index, (value, label) in enumerate(metrics):
        cell_x = x + index * cell_w
        draw.text((cell_x, y), value, fill=0, font=value_font)
        draw.text((cell_x, y + px(27)), label, fill=0, font=small_font)
        if index:
            draw.line((cell_x - px(9), y, cell_x - px(9), memory_box[3] - px(12)), fill=0, width=line)

    # Recent activity card
    _card(draw, activity_box, radius, line)
    x = activity_box[0] + px(14)
    y = activity_box[1] + px(9)
    _label(draw, (x, y), "Recent agent activity", small_font)
    y += px(21)
    event_width = activity_box[2] - x - px(14)
    event_line_h = px(20)
    for event in snapshot.recent_events[-5:]:
        draw.rectangle((x, y + px(5), x + px(5), y + px(10)), fill=0)
        draw.text((x + px(12), y), _fit_text(draw, event, event_font, event_width - px(12)), fill=0, font=event_font)
        y += event_line_h
        if y + event_line_h > activity_box[3] - px(5):
            break

    if opts.bit_depth == 1:
        return image.convert("1", dither=Image.Dither.NONE)
    return image
