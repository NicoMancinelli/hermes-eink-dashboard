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

from .aggregators.base import Aggregator
from .contract import PanelCache
from .render import RenderOptions, render_dashboard
from .scheduler import collect_once, run_aggregator_loop
from .state import DashboardSnapshot


@dataclass(frozen=True)
class ApiSettings:
    token: str
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
) -> FastAPI:
    panel_cache = cache or PanelCache()
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

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
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
    async def legacy_png(request: Request) -> Response:
        _require_auth(request, settings, allow_query=True)
        snapshot = _legacy_snapshot(panel_cache.snapshot())

        def render_png() -> bytes:
            image = render_dashboard(
                snapshot,
                RenderOptions(
                    width=settings.width,
                    height=settings.height,
                    bit_depth=settings.bit_depth,
                ),
            )
            output = BytesIO()
            image.save(output, format="PNG", optimize=True)
            return output.getvalue()

        return Response(await run_in_threadpool(render_png), media_type="image/png")

    return app
