#!/usr/bin/env python3
"""Build KUAL extension ZIPs for the Hermes E-Ink Dashboard.

Two build modes are supported:

1. **Template mode** (default): produces a bundle with placeholder tokens
   (HOST_IP=PLACEHOLDER, DASHBOARD_TOKEN=PLACEHOLDER). Safe to publish as
   a release asset — anyone can download, unzip, and configure via the
   post-install flow on the Kindle.

2. **Personal mode** (``--inject-tokens``): embeds the user's real tokens
   from ``~/.config/hermes-kindle-dashboard/{token,control_token}``. Use
   this to produce a per-host bundle. Never publish this artifact.

The bundle layout:

    hermes_dashboard/
        config.sh        # generated from config.sh.example
        config.sh.example
        config.xml
        menu.json
        bin/             # fetch.sh, refresh.sh, start.sh, stop.sh

The bundle is unpacked on the Kindle at ``/mnt/us/extensions/hermes_dashboard/``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "kindle" / "hermes_dashboard"
SAFE_HOST = re.compile(r"^[A-Za-z0-9._:-]+$")
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._~-]+$")
PLACEHOLDER_HOST = "PLACEHOLDER.lan"
PLACEHOLDER_TOKEN = "PLACEHOLDER_TOKEN"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=PLACEHOLDER_HOST, help="host (placeholder by default)")
    parser.add_argument("--port", type=int, default=9120)
    parser.add_argument(
        "--inject-tokens",
        action="store_true",
        help="embed real tokens from --token-file / --control-token-file (do not publish this build)",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path("~/.config/hermes-kindle-dashboard/token").expanduser(),
    )
    parser.add_argument(
        "--control-token-file",
        type=Path,
        default=Path("~/.config/hermes-kindle-dashboard/control_token").expanduser(),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "hermes-dashboard-kual-v0.4.0.zip",
    )
    parser.add_argument(
        "--no-control-token",
        action="store_true",
        help="omit the control token even in personal builds (disables /control endpoints)",
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
    if host == PLACEHOLDER_HOST or not SAFE_HOST.fullmatch(host):
        if host != PLACEHOLDER_HOST:
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
    inject_tokens: bool,
    token_file: Path,
    control_token_file: Path | None,
    include_control_token: bool,
    output: Path,
) -> tuple[Path, dict]:
    host = _validate_host(host)
    port = _validate_port(port)

    if inject_tokens:
        read_token = _read_token(token_file, "read token")
        control_token = ""
        if include_control_token and control_token_file is not None:
            expanded = control_token_file.expanduser()
            if expanded.exists():
                control_token = _read_token(expanded, "control token")
    else:
        read_token = PLACEHOLDER_TOKEN
        control_token = ""

    metadata = {
        "schema_version": 1,
        "mode": "personal" if inject_tokens else "template",
        "host": host,
        "port": port,
        "tokens_embedded": inject_tokens,
        "control_token_embedded": bool(control_token),
        "read_token_sha256_prefix": _token_prefix(read_token),
    }
    if control_token:
        metadata["control_token_sha256_prefix"] = _token_prefix(control_token)

    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hermes-kual-") as temporary:
        extension = Path(temporary) / "hermes_dashboard"
        shutil.copytree(SOURCE, extension)
        # Bundle the interactive Python client + launcher scripts.
        interactive_client = ROOT / "kindle" / "client" / "interactive.py"
        if interactive_client.exists():
            (extension / "bin" / "interactive_client.py").write_bytes(
                interactive_client.read_bytes()
            )
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
        inject_tokens=args.inject_tokens,
        token_file=args.token_file,
        control_token_file=args.control_token_file if not args.no_control_token else None,
        include_control_token=not args.no_control_token,
        output=args.output,
    )
    print(json.dumps({"path": str(output), **metadata}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())