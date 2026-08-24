"""Admin CLI for device pairing: ``hermes-dashboard-pair``.

Runs on the host and talks to the local dashboard service over loopback HTTP
using the control token already configured there, so no secrets ever need to
be copied to or typed on a Kindle.

Usage:
    hermes-dashboard-pair list
    hermes-dashboard-pair approve AB12-CD34
    hermes-dashboard-pair deny AB12-CD34

The display code is shown on the Kindle's first-run wizard screen.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_CONTROL_TOKEN_FILE = Path("~/.config/hermes-kindle-dashboard/control_token").expanduser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Approve or deny dashboard device pairing requests")
    parser.add_argument("--host", default=os.getenv("HERMES_DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("HERMES_DASHBOARD_PORT", "9120")))
    parser.add_argument(
        "--control-token-file",
        type=Path,
        default=Path(os.getenv("HERMES_DASHBOARD_CONTROL_TOKEN_FILE", str(DEFAULT_CONTROL_TOKEN_FILE))).expanduser(),
    )
    parser.add_argument("command", choices=("list", "approve", "deny"))
    parser.add_argument("device", nargs="?", default="", help="display code (AB12-CD34) or device id")
    return parser


def _request(base_url: str, path: str, control_token: str, payload: dict | None = None) -> tuple[int, dict]:
    url = f"{base_url}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    request.add_header("Authorization", f"Bearer {control_token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8"))
        except Exception:
            body = {}
        return error.code, body


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in ("approve", "deny") and not args.device.strip():
        print(f"error: '{args.command}' requires a display code or device id", file=sys.stderr)
        return 2

    control_token = ""
    if args.control_token_file.exists():
        control_token = args.control_token_file.read_text(encoding="utf-8").strip()
    if not control_token:
        print(
            f"error: no control token at {args.control_token_file}. Run scripts/install_host.sh first.",
            file=sys.stderr,
        )
        return 1

    base_url = f"http://{args.host}:{args.port}"
    if args.command == "list":
        status_code, body = _request(base_url, "/pair/devices", control_token)
        if status_code != 200:
            print(f"error: /pair/devices returned {status_code}: {body.get('detail', 'unknown')}", file=sys.stderr)
            return 1
        devices = body.get("devices", [])
        if not devices:
            print("No pending or paired devices.")
            return 0
        for device in devices:
            marker = "*" if device["status"] == "pending" else " "
            approved = f" approved {device['approved_at']}" if device.get("approved_at") else ""
            print(f"{marker} {device['display_code']}  {device['name']:<24} {device['status']}{approved}")
        if any(device["status"] == "pending" for device in devices):
            print("\nApprove a pending device with: hermes-dashboard-pair approve <CODE>")
        return 0

    path = "/pair/approve" if args.command == "approve" else "/pair/deny"
    status_code, body = _request(base_url, path, control_token, {"device": args.device.strip()})
    if status_code == 404:
        print(f"error: unknown device {args.device!r} (run 'hermes-dashboard-pair list')", file=sys.stderr)
        return 1
    if status_code != 200:
        print(f"error: {path} returned {status_code}: {body.get('detail', 'unknown')}", file=sys.stderr)
        return 1
    if args.command == "approve":
        name = body.get("device", {}).get("name", "")
        print(f"Approved {name}. The device will pick up its tokens on its next poll.")
    else:
        print("Device removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
