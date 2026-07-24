#!/usr/bin/env python3
"""Build a ready-to-copy KUAL extension without modifying tracked sources."""

from __future__ import annotations

import argparse
import re
import shutil
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
    )
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "hermes-dashboard-kual.zip")
    return parser.parse_args()


def build(host: str, port: int, token_file: Path, output: Path) -> Path:
    if not SAFE_HOST.fullmatch(host):
        raise SystemExit("host may contain only letters, numbers, dots, colons, underscores, and hyphens")
    if not 1 <= port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    try:
        token = token_file.expanduser().read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"cannot read token file {token_file}: {exc}") from exc
    if not token or not SAFE_TOKEN.fullmatch(token):
        raise SystemExit("token must be non-empty and URL-safe")

    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hermes-kual-") as temporary:
        extension = Path(temporary) / "hermes_dashboard"
        shutil.copytree(SOURCE, extension)
        example = extension / "config.sh.example"
        config = example.read_text(encoding="utf-8")
        config = config.replace('HOST_IP="HOST_IP"', f'HOST_IP="{host}"')
        config = config.replace('HOST_PORT="9120"', f'HOST_PORT="{port}"')
        config = config.replace('DASHBOARD_TOKEN="CHANGE_ME"', f'DASHBOARD_TOKEN="{token}"')
        (extension / "config.sh").write_text(config, encoding="utf-8")
        for script in (extension / "bin").glob("*.sh"):
            script.chmod(0o755)

        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(extension.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(extension.parent))
    output.chmod(0o600)
    return output


def main() -> int:
    args = parse_args()
    output = build(args.host, args.port, args.token_file, args.output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
