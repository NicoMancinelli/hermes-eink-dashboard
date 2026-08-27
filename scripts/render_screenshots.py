"""Regenerate the README screenshots from synthetic sample state.

The dashboard images in ``docs/screenshots/`` are renders of the default
tile layout from synthetic state, so the README never depends on a live
Hermes install. This script is that synthetic state, made reproducible:

* one quick prompt + one model preference (the ``Ask:`` / ``Model:`` tiles),
* a sample quick-prompt answer on the ``Ask Hermes`` panel,
* the Hermes summary panel built from ``tests.test_render.sample_snapshot``.

Run from the repo root:

    .venv/bin/python scripts/render_screenshots.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from hermes_kindle_dashboard.aggregators.prompt_response import build_prompt_response_panel  # noqa: E402
from hermes_kindle_dashboard.contract import PanelCache, build_default_layout  # noqa: E402
from hermes_kindle_dashboard.hermes_controls import HermesControlsConfig, control_tiles  # noqa: E402
from hermes_kindle_dashboard.render import RenderOptions, render_layout_dashboard  # noqa: E402
from tests.test_render import sample_snapshot  # noqa: E402

SIZES = ((1072, 1448, "dashboard-paperwhite.png"), (600, 800, "dashboard-classic.png"))


def _sample_prompt_panel() -> dict:
    """A sample quick-prompt answer written like HermesPromptAction would."""
    with tempfile.TemporaryDirectory() as tmp:
        config_dir = Path(tmp)
        actions = config_dir / "actions"
        actions.mkdir()
        (actions / "hermes.prompt.standup.json").write_text(
            json.dumps(
                {
                    "kind": "prompt",
                    "status": "ok",
                    "prompt": "standup",
                    "returncode": 0,
                    "stdout": "Three threads need you today: the KUAL build, "
                    "the resize flicker, and the beacon flakiness. Two are "
                    "waiting on a re-test; one is ready to merge.",
                    "stderr": "",
                    "updated_at": datetime.now(tz=timezone.utc).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        return build_prompt_response_panel(config_dir)


def main() -> None:
    cache = PanelCache()
    cache.register("hermes")
    cache.register("prompt_response")
    hermes_panel = sample_snapshot().to_dict()
    hermes_panel.pop("generated_at", None)
    hermes_panel["user_state"] = {"quick_prompt_model": "openai-codex"}
    cache.record_success("hermes", hermes_panel)
    cache.record_success("prompt_response", _sample_prompt_panel())

    controls = HermesControlsConfig(
        quick_prompts={"standup": "Draft a standup update."},
        models=["openai-codex"],
    )
    out_dir = _REPO_ROOT / "docs" / "screenshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    for width, height, name in SIZES:
        layout = build_default_layout(
            width,
            height,
            panels=("hermes", "prompt_response"),
            control_tiles=control_tiles(controls),
        )
        image = render_layout_dashboard(
            layout,
            cache.snapshot(),
            RenderOptions(width=width, height=height, bit_depth=1),
        )
        dest = out_dir / name
        image.save(dest, format="PNG", optimize=True)
        print(f"wrote {dest} ({image.size}, mode={image.mode})")


if __name__ == "__main__":
    main()