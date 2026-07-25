from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Iterable

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .actions import (
    ActionError,
    ActionRegistry,
    InvalidNonceError,
    InvalidTimestampError,
    RateLimitExceededError,
    UnknownActionError,
)
from collections.abc import Callable
from .aggregators.base import Aggregator
from .config import ConfigManager
from .config import ConfigManager, ConfigSchema
from .contract import PanelCache, build_default_layout, dashboard_json
from .render import RenderOptions, render_dashboard, render_layout_dashboard
from .scheduler import ControlBus, collect_once, run_aggregator_loop
from .state import DashboardSnapshot


@dataclass(frozen=True)
class ApiSettings:
    token: str
    control_token: str = ""
    width: int = 1072
    height: int = 1448
    bit_depth: int = 1


def _authorized(request: Request, expected: str, *, allow_query: bool) -> bool:
    if not expected:
        return True
    authorization = request.headers.get("Authorization", "")
    supplied = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    if not supplied and allow_query:
        supplied = request.query_params.get("token", "")
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _require_auth(request: Request, settings: ApiSettings, *, allow_query: bool = False) -> None:
    if not _authorized(request, settings.token, allow_query=allow_query):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


def _require_control_auth(request: Request, settings: ApiSettings) -> None:
    if not settings.control_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="control service unavailable",
        )
    if not _authorized(request, settings.control_token, allow_query=False):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


def _legacy_snapshot(payload: dict[str, Any]) -> DashboardSnapshot:
    panel = payload.get("panels", {}).get("hermes")
    if not isinstance(panel, dict):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Hermes state unavailable")
    meta = panel.get("_meta", {})
    if not isinstance(meta, dict) or meta.get("status") not in {"ok", "stale"}:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Hermes state unavailable")
    generated_at = meta.get("updated_at") or payload["generated_at"]
    data = {key: value for key, value in panel.items() if key != "_meta"}
    try:
        return DashboardSnapshot.from_panel(data, str(generated_at))
    except (TypeError, ValueError, KeyError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Hermes state unavailable",
        ) from error


def create_app(
    settings: ApiSettings,
    aggregators: Iterable[Aggregator],
    cache: PanelCache | None = None,
    bus: ControlBus | None = None,
    registry: ActionRegistry | None = None,
    layout: dict[str, Any] | None = None,
    config_manager_factory: Callable[[], ConfigManager] | None = None,
) -> FastAPI:
    panel_cache = cache or PanelCache()
    control_bus = bus or ControlBus()
    action_registry = registry or ActionRegistry()
    config_manager = (config_manager_factory or ConfigManager)()
    providers = tuple(aggregators)
    for provider in providers:
        panel_cache.register(provider.name)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if providers:
            await asyncio.gather(*(collect_once(provider, panel_cache) for provider in providers))
        stop_event = asyncio.Event()
        tasks = [
            asyncio.create_task(
                run_aggregator_loop(provider, panel_cache, stop_event, initial_delay=True),
                name=f"dashboard-{provider.name}",
            )
            for provider in providers
        ]
        try:
            yield
        finally:
            stop_event.set()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(
        title="Hermes E-Ink Dashboard API",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.panel_cache = panel_cache
    app.state.control_bus = control_bus
    app.state.action_registry = action_registry
    app.state.layout = layout
    app.state.config_manager = config_manager

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/dashboard-data")
    async def dashboard_data(request: Request) -> JSONResponse:
        _require_auth(request, settings)
        return JSONResponse(panel_cache.snapshot())

    @app.get("/state.json")
    async def legacy_state(request: Request) -> JSONResponse:
        _require_auth(request, settings, allow_query=True)
        return JSONResponse(_legacy_snapshot(panel_cache.snapshot()).to_dict())

    @app.get("/dashboard.png")
    async def legacy_png(
        request: Request,
        focus_tile: str | None = None,
        focus_tile_id: str | None = None,
    ) -> Response:
        _require_auth(request, settings, allow_query=True)
        snapshot = _legacy_snapshot(panel_cache.snapshot())
        target_focus = (
            focus_tile
            or focus_tile_id
            or request.query_params.get("focus-tile")
            or request.query_params.get("focus_tile")
            or request.query_params.get("focus_tile_id")
        )

        def render_png() -> bytes:
            opts = RenderOptions(
                width=settings.width,
                height=settings.height,
                bit_depth=settings.bit_depth,
            )
            if app.state.layout is not None or target_focus is not None:
                eff_layout = dict(app.state.layout or build_default_layout(settings.width, settings.height))
                if target_focus:
                    eff_layout["focus"] = {"tile_id": target_focus}
                image = render_layout_dashboard(eff_layout, snapshot, opts)
            else:
                image = render_dashboard(snapshot, opts)

            output = BytesIO()
            image.save(output, format="PNG", optimize=True)
            return output.getvalue()

        return Response(await run_in_threadpool(render_png), media_type="image/png")

    @app.get("/dashboard.json")
    async def get_dashboard_json(
        request: Request,
        focus_tile_id: str | None = None,
        focus_tile: str | None = None,
    ) -> JSONResponse:
        _require_auth(request, settings, allow_query=True)
        target_focus = (
            focus_tile_id
            or focus_tile
            or request.query_params.get("focus-tile")
            or request.query_params.get("focus_tile")
        )
        eff_layout = app.state.layout or build_default_layout(settings.width, settings.height)
        return JSONResponse(dashboard_json(eff_layout, focus_tile_id=target_focus))

    @app.post("/control")
    async def post_control(request: Request) -> JSONResponse:
        _require_control_auth(request, settings)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_payload")
        if not isinstance(body, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_payload")

        action = str(body.get("action", ""))
        tile_id = str(body.get("tile_id", ""))
        nonce = str(body.get("nonce", ""))
        ts = body.get("ts", 0)

        try:
            res = action_registry.dispatch(action=action, tile_id=tile_id, nonce=nonce, ts=ts)
        except UnknownActionError:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
        except RateLimitExceededError:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate_limited")
        except InvalidTimestampError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_timestamp")
        except InvalidNonceError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_nonce")
        except (ActionError, ValueError, TypeError):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_payload")

        control_bus.publish(res)
        return JSONResponse({"status": "ok", "action": action, "tile_id": tile_id})

    @app.get("/control/events")
    async def get_control_events(request: Request, timeout: float = 30.0) -> JSONResponse:
        _require_control_auth(request, settings)
        effective_timeout = min(max(0.0, float(timeout)), 30.0)
        event = await control_bus.wait_for_event(timeout=effective_timeout)
        return JSONResponse({"event": event})

    @app.get("/config")
    async def get_config(request: Request) -> JSONResponse:
        """Get the current declarative configuration."""
        _require_control_auth(request, settings)
        config_manager = app.state.config_manager
        config = config_manager.load()
        if config is None:
            return JSONResponse({"config": None, "message": "No configuration file found. POST to /config to create one."})
        return JSONResponse({"config": config.model_dump()})

    @app.post("/config")
    async def post_config(request: Request) -> JSONResponse:
        """Update the declarative configuration and regenerate config.sh."""
        _require_control_auth(request, settings)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_payload")
        if not isinstance(body, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_payload")

        try:
            config = ConfigSchema(**body)
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

        config_manager = app.state.config_manager
        config_manager.save(config)
        rendered = config_manager.regenerate_config_sh(config)
        config_sh_path = config_manager.safe_output_path

        return JSONResponse({
            "status": "ok",
            "message": "Configuration saved and config.sh regenerated",
            "config": config.model_dump(),
            "config_sh_path": str(config_sh_path),
        })

    @app.post("/config/preview")
    async def post_config_preview(request: Request) -> JSONResponse:
        """Preview the config.sh that would be generated without saving."""
        _require_control_auth(request, settings)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_payload")
        if not isinstance(body, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_payload")

        try:
            config = ConfigSchema(**body)
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

        config_manager = app.state.config_manager
        template = config_manager.load_template()
        rendered = config.to_config_sh(template)

        return JSONResponse({
            "status": "ok",
            "preview": rendered,
        })

    @app.get("/config/example")
    async def get_config_example(request: Request) -> JSONResponse:
        """Get an example configuration YAML."""
        _require_control_auth(request, settings)
        config_manager = app.state.config_manager
        example_yaml = config_manager.get_example_config()
        return JSONResponse({
            "example_yaml": example_yaml,
        })

    return app

