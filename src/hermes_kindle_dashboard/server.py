from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from .render import RenderOptions, render_dashboard
from .state import DashboardConfig, DashboardSnapshot, HermesStateCollector

LOGGER = logging.getLogger("hermes-kindle-dashboard")


@dataclass(frozen=True)
class ServerSettings:
    token: str
    width: int = 1072
    height: int = 1448
    bit_depth: int = 1
    cache_seconds: float = 10.0


class DashboardApplication:
    def __init__(self, collector: HermesStateCollector, settings: ServerSettings):
        self.collector = collector
        self.settings = settings
        self._lock = threading.Lock()
        self._snapshot: DashboardSnapshot | None = None
        self._png: bytes | None = None
        self._updated_monotonic = 0.0

    def authorized(self, supplied: str) -> bool:
        if not self.settings.token:
            return True
        return hmac.compare_digest(supplied.encode("utf-8"), self.settings.token.encode("utf-8"))

    def _refresh(self) -> None:
        now = time.monotonic()
        if self._snapshot is not None and now - self._updated_monotonic < self.settings.cache_seconds:
            return
        with self._lock:
            now = time.monotonic()
            if self._snapshot is not None and now - self._updated_monotonic < self.settings.cache_seconds:
                return
            snapshot = self.collector.collect()
            image = render_dashboard(
                snapshot,
                RenderOptions(
                    width=self.settings.width,
                    height=self.settings.height,
                    bit_depth=self.settings.bit_depth,
                ),
            )
            output = BytesIO()
            image.save(output, format="PNG", optimize=True)
            self._snapshot = snapshot
            self._png = output.getvalue()
            self._updated_monotonic = now

    def snapshot(self) -> DashboardSnapshot:
        self._refresh()
        assert self._snapshot is not None
        return self._snapshot

    def png(self) -> bytes:
        self._refresh()
        assert self._png is not None
        return self._png


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], app: DashboardApplication):
        self.app = app
        super().__init__(server_address, DashboardRequestHandler)


class DashboardRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Never log the request path because query-string auth may be enabled for BusyBox wget.
        LOGGER.info("%s %s", self.client_address[0], args[1] if len(args) > 1 else "request")

    def _token(self) -> str:
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            return authorization[7:].strip()
        query = parse_qs(urlparse(self.path).query)
        return query.get("token", [""])[0]

    def _send(self, status: int, content_type: str, body: bytes, head_only: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _json(self, status: int, payload: dict, head_only: bool = False) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        self._send(status, "application/json", body, head_only)

    def _handle(self, head_only: bool = False) -> None:
        server = cast(DashboardHTTPServer, self.server)
        path = urlparse(self.path).path
        if path == "/healthz":
            self._json(HTTPStatus.OK, {"status": "ok"}, head_only)
            return
        if path not in {"/dashboard.png", "/state.json"}:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"}, head_only)
            return
        if not server.app.authorized(self._token()):
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"}, head_only)
            return
        try:
            if path == "/dashboard.png":
                self._send(HTTPStatus.OK, "image/png", server.app.png(), head_only)
            else:
                self._json(HTTPStatus.OK, server.app.snapshot().to_dict(), head_only)
        except Exception:
            LOGGER.exception("dashboard request failed")
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "state unavailable"}, head_only)

    def do_GET(self) -> None:  # noqa: N802
        self._handle(False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle(True)


def create_server(host: str, port: int, app: DashboardApplication) -> DashboardHTTPServer:
    return DashboardHTTPServer((host, port), app)


def _load_token(token: str, token_file: Path | None, insecure: bool) -> str:
    value = token.strip()
    if not value and token_file and token_file.exists():
        value = token_file.read_text(encoding="utf-8").strip()
    if not value and not insecure:
        raise SystemExit("A token is required. Set --token/--token-file, or explicitly use --insecure.")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve a Hermes status dashboard for an E-Ink Kindle")
    parser.add_argument("--host", default=os.getenv("HERMES_DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("HERMES_DASHBOARD_PORT", "9120")))
    parser.add_argument("--width", type=int, default=int(os.getenv("HERMES_DASHBOARD_WIDTH", "1072")))
    parser.add_argument("--height", type=int, default=int(os.getenv("HERMES_DASHBOARD_HEIGHT", "1448")))
    parser.add_argument("--bit-depth", type=int, choices=(1, 8), default=int(os.getenv("HERMES_DASHBOARD_BIT_DEPTH", "1")))
    parser.add_argument("--context-limit", type=int, default=int(os.getenv("HERMES_DASHBOARD_CONTEXT_LIMIT", "262144")))
    parser.add_argument("--cache-seconds", type=float, default=float(os.getenv("HERMES_DASHBOARD_CACHE_SECONDS", "10")))
    parser.add_argument("--hermes-home", type=Path, default=Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser())
    parser.add_argument("--token", default=os.getenv("HERMES_DASHBOARD_TOKEN", ""))
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path(os.getenv("HERMES_DASHBOARD_TOKEN_FILE", "~/.config/hermes-kindle-dashboard/token")).expanduser(),
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
    settings = ServerSettings(
        token=_load_token(args.token, args.token_file, args.insecure or bool(args.render_once)),
        width=args.width,
        height=args.height,
        bit_depth=args.bit_depth,
        cache_seconds=args.cache_seconds,
    )
    app = DashboardApplication(collector, settings)
    if args.render_once:
        args.render_once.parent.mkdir(parents=True, exist_ok=True)
        args.render_once.write_bytes(app.png())
        LOGGER.info("wrote %s", args.render_once)
        return 0

    server = create_server(args.host, args.port, app)
    LOGGER.info("serving on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
