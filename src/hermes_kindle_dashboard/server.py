from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
from pathlib import Path
from typing import Any

import uvicorn

from .actions import ActionRegistry
from .actions_runtime import register_all_actions
from .aggregators.hermes import HermesAggregator
from .api import ApiSettings, create_app
from .beacon import BeaconConfig
from .contract import build_default_layout
from .pairing import DeviceStore, PairingService
from .render import RenderOptions, render_dashboard, render_layout_dashboard
from .scheduler import ControlBus, collect_once
from .state import DashboardConfig, HermesStateCollector

LOGGER = logging.getLogger("hermes-kindle-dashboard")


def _discovery_default() -> int:
    from .beacon import DEFAULT_DISCOVERY_PORT

    return DEFAULT_DISCOVERY_PORT


def _load_token(token: str, token_file: Path | None, *, optional: bool = False) -> str:
    """Resolve a token from --token flag, --token-file path, or return empty string."""
    value = token.strip()
    if not value and token_file and token_file.exists():
        value = token_file.read_text(encoding="utf-8").strip()
    if not value and not optional:
        raise SystemExit("A token is required. Set --token/--token-file, or pass --insecure.")
    return value


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


def parse_layout_yaml(content: str) -> dict[str, Any]:
    """Parse a layout YAML file using PyYAML.

    PyYAML is already a project dependency, so we don't need a hand-rolled
    parser. The previous hand-rolled parser only handled a tiny subset of
    YAML and silently dropped nested structures, quoted strings, and
    multiline values.
    """
    import yaml as _yaml

    try:
        data = _yaml.safe_load(content) or {}
    except _yaml.YAMLError as exc:
        raise SystemExit(f"invalid layout YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("layout YAML must be a mapping at the top level")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve device-neutral Hermes data for E-Ink dashboards")
    parser.add_argument("--host", default=os.getenv("HERMES_DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("HERMES_DASHBOARD_PORT", "9120")))
    parser.add_argument("--width", type=int, default=int(os.getenv("HERMES_DASHBOARD_WIDTH", "1072")))
    parser.add_argument("--height", type=int, default=int(os.getenv("HERMES_DASHBOARD_HEIGHT", "1448")))
    parser.add_argument("--bit-depth", type=int, choices=(1, 8), default=int(os.getenv("HERMES_DASHBOARD_BIT_DEPTH", "1")))
    parser.add_argument("--context-limit", type=int, default=int(os.getenv("HERMES_DASHBOARD_CONTEXT_LIMIT", "262144")))
    parser.add_argument(
        "--refresh-seconds",
        type=_positive_float,
        default=os.getenv(
            "HERMES_DASHBOARD_REFRESH_SECONDS",
            os.getenv("HERMES_DASHBOARD_CACHE_SECONDS", "15"),
        ),
        help="seconds between local Hermes collection runs",
    )
    parser.add_argument("--hermes-home", type=Path, default=Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser())
    parser.add_argument("--token", default=os.getenv("HERMES_DASHBOARD_TOKEN", ""))
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path(os.getenv("HERMES_DASHBOARD_TOKEN_FILE", "~/.config/hermes-kindle-dashboard/token")).expanduser(),
    )
    parser.add_argument("--control-token", default=os.getenv("HERMES_DASHBOARD_CONTROL_TOKEN", ""))
    parser.add_argument(
        "--control-token-file",
        type=Path,
        default=Path(os.getenv("HERMES_DASHBOARD_CONTROL_TOKEN_FILE", "~/.config/hermes-kindle-dashboard/control_token")).expanduser(),
    )
    parser.add_argument("--layout-yaml", type=Path, help="path to layout YAML file to override default layout")
    parser.add_argument(
        "--devices-file",
        type=Path,
        default=Path(os.getenv("HERMES_DASHBOARD_DEVICES_FILE", "~/.config/hermes-kindle-dashboard/devices.json")).expanduser(),
        help="where paired device records are stored (mode 0600)",
    )
    parser.add_argument(
        "--no-pairing",
        action="store_true",
        help="disable the device pairing endpoints (/pair/*)",
    )
    parser.add_argument(
        "--discovery-port",
        type=int,
        default=int(os.getenv("HERMES_DASHBOARD_DISCOVERY_PORT", str(_discovery_default()))),
        help="UDP port for LAN discovery broadcasts (0 disables discovery)",
    )
    parser.add_argument("--insecure", action="store_true", help="allow unauthenticated access")
    parser.add_argument("--render-once", type=Path, help="write one PNG and exit")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = DashboardConfig.from_home(args.hermes_home, context_limit=args.context_limit)
    collector = HermesStateCollector(config)

    layout_override = None
    if args.layout_yaml and args.layout_yaml.exists():
        layout_override = parse_layout_yaml(args.layout_yaml.read_text(encoding="utf-8"))

    if args.render_once:
        # Use the FastAPI app's lifespan to run a single collection cycle, then
        # render to PNG. This shares the renderer with the live service.
        fastapi_app = create_app(
            ApiSettings(token="", width=args.width, height=args.height, bit_depth=args.bit_depth),
            [HermesAggregator(collector=collector, interval_seconds=args.refresh_seconds)],
        )
        # Trigger one aggregator cycle synchronously.
        from .contract import PanelCache as _PanelCache
        cache = _PanelCache()
        cache.register("hermes")
        asyncio.run(collect_once(HermesAggregator(collector=collector, interval_seconds=args.refresh_seconds), cache))
        snapshot = cache.snapshot().get("panels", {}).get("hermes", {}).get("data", {})
        from .state import DashboardSnapshot as _Snap
        snap = _Snap.from_panel(snapshot, str(cache.snapshot()["generated_at"]))
        layout = layout_override or build_default_layout(args.width, args.height)
        image = render_layout_dashboard(layout, snap, RenderOptions(width=args.width, height=args.height, bit_depth=args.bit_depth))
        args.render_once.parent.mkdir(parents=True, exist_ok=True)
        from io import BytesIO as _BytesIO
        buf = _BytesIO()
        image.save(buf, format="PNG", optimize=True)
        args.render_once.write_bytes(buf.getvalue())
        LOGGER.info("wrote %s", args.render_once)
        return 0

    token = _load_token(args.token, args.token_file, optional=args.insecure)
    control_token = ""
    if args.control_token or (args.control_token_file and args.control_token_file.exists()):
        control_token = _load_token(args.control_token, args.control_token_file, optional=True)
    # If no control token is configured, we leave it empty. The /control endpoints
    # return 503 Service Unavailable in that case, which is the secure default:
    # the read token is never silently granted write access.

    bus = ControlBus()
    registry = ActionRegistry()
    pairing = None if args.no_pairing else PairingService(DeviceStore(args.devices_file))

    actions_dir_str = os.getenv("HERMES_DASHBOARD_ACTIONS_DIR", "~/.config/hermes-kindle-dashboard")
    actions_dir = Path(actions_dir_str).expanduser()
    if actions_dir.exists():
        register_all_actions(registry, actions_dir, logger=LOGGER)
    app = create_app(
        ApiSettings(
            token=token,
            control_token=control_token,
            width=args.width,
            height=args.height,
            bit_depth=args.bit_depth,
            pairing=pairing,
        ),
        [HermesAggregator(collector=collector, interval_seconds=args.refresh_seconds)],
        bus=bus,
        registry=registry,
        layout=layout_override,
        beacon_config=(
            BeaconConfig(port=args.discovery_port, service_port=args.port)
            if args.discovery_port > 0
            else None
        ),
    )
    LOGGER.info("serving on http://%s:%d", args.host, args.port)
    # Access logging is disabled because legacy Kindle requests may contain a query token.
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)
    return 0




if __name__ == "__main__":
    raise SystemExit(main())
