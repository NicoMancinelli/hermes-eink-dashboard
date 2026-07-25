#!/usr/bin/env python3
"""Build a ready-to-copy KUAL extension without modifying tracked sources.

The bundle embeds the host address, read token, and (optionally) control token.
The resulting ZIP is intended to be copied to the Kindle's
`/mnt/us/extensions/hermes_dashboard/` directory and installed via KUAL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "kindle" / "hermes_dashboard"
SAFE_HOST = re.compile(r"^[A-Za-z0-9._:-]+$")
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._~-]+$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="LAN or tailnet address reachable by the Kindle")
    parser.add_argument("--port", type=int, default=9120)
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path("~/.config/hermes-kindle-dashboard/token").expanduser(),
        help="Path to the read token file",
    )
    parser.add_argument(
        "--control-token-file",
        type=Path,
        default=Path("~/.config/hermes-kindle-dashboard/control_token").expanduser(),
        help="Path to the control token file (optional; skip if interactive controls are disabled)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "hermes-dashboard-kual-v0.3.0.zip",
    )
    parser.add_argument(
        "--no-control-token",
        action="store_true",
        help="Disable the control token even if the file exists (write endpoints will return 503)",
    )
    return parser.parse_args()


def _read_token(path: Path, label: str) -> str:
    try:
        value = path.expanduser().read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"cannot read {label} file {path}: {exc}") from exc
    if not value or not SAFE_TOKEN.fullmatch(value):
        raise SystemExit(f"{label} must be non-empty and URL-safe")
    return value


def _validate_host(host: str) -> str:
    if not SAFE_HOST.fullmatch(host):
        raise SystemExit("host may contain only letters, numbers, dots, colons, underscores, and hyphens")
    return host


def _validate_port(port: int) -> int:
    if not 1 <= port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    return port


def _token_prefix(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def build(
    host: str,
    port: int,
    token_file: Path,
    control_token_file: Path | None,
    output: Path,
    include_control_token: bool,
) -> tuple[Path, dict]:
    host = _validate_host(host)
    port = _validate_port(port)

    read_token = _read_token(token_file, "read token")

    control_token = ""
    if include_control_token and control_token_file is not None:
        expanded = control_token_file.expanduser()
        if expanded.exists():
            control_token = _read_token(expanded, "control token")

    metadata = {
        "schema_version": 1,
        "host": host,
        "port": port,
        "read_token_set": True,
        "control_token_set": bool(control_token),
        "read_token_sha256_prefix": _token_prefix(read_token),
    }
    if control_token:
        metadata["control_token_sha256_prefix"] = _token_prefix(control_token)

    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hermes-kual-") as temporary:
        extension = Path(temporary) / "hermes_dashboard"
        shutil.copytree(SOURCE, extension)
        example = extension / "config.sh.example"
        config = example.read_text(encoding="utf-8")
        config = config.replace('HOST_IP="HOST_IP"', f'HOST_IP="{host}"')
        config = config.replace('HOST_PORT="9120"', f'HOST_PORT="{port}"')
        config = config.replace('DASHBOARD_TOKEN="CHANGE_ME"', f'DASHBOARD_TOKEN="{read_token}"')
        if control_token:
            config = config.replace('CONTROL_TOKEN=""', f'CONTROL_TOKEN="{control_token}"')
        (extension / "config.sh").write_text(config, encoding="utf-8")
        for script in (extension / "bin").glob("*.sh"):
            script.chmod(0o755)

        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(extension.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(extension.parent))
    output.chmod(0o600)
    return output, metadata


def main() -> int:
    args = parse_args()
    output, metadata = build(
        host=args.host,
        port=args.port,
        token_file=args.token_file,
        control_token_file=args.control_token_file if not args.no_control_token else None,
        output=args.output,
        include_control_token=not args.no_control_token,
    )
    print(json.dumps({"path": str(output), **metadata}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())