#!/usr/bin/env python3
"""Pre-release gate for the built artifacts.

This runs in CI against ``dist/`` and must stay dependency-free, because it is
used to decide whether an artifact may be published. It checks the things that
are expensive to discover after a tag is pushed:

* the wheel and sdist expose exactly the intended console scripts
* no retired ``hermes-kindle-dashboard`` alias or ``hermes_kindle_dashboard``
  import package survived into the artifacts
* no credential-shaped string was baked into the distribution
* the Kindle extension version matches the Python distribution version

Usage::

    python scripts/verify_release.py dist
"""

from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

RETIRED_IMPORT = "hermes_kindle_dashboard"
RETIRED_CONSOLE_SCRIPT = "hermes-kindle-dashboard"
EXPECTED_CONSOLE_SCRIPTS = {"hermes-eink-dashboard", "hermes-dashboard-pair"}

# High-confidence patterns: a hit is always a release blocker, so they are
# applied to every shipped file including tests.
HIGH_CONFIDENCE_PATTERNS: dict[str, re.Pattern[str]] = {
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "AWS access key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
}

# Heuristic pattern for anything assigned to a credential-shaped key. It fires
# on documentation placeholders and test fixtures, so it is only applied to
# runtime files and additionally filtered by PLACEHOLDER_MARKERS and entropy.
CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(?:dashboard_token|control_token|password|secret|api[_-]?key)\b"
    r"\s*[:=]\s*[\"']?([^\s\"'\n]{16,})"
)
PLACEHOLDER_MARKERS = re.compile(
    r"(?i)(CHANGE_ME|PLACEHOLDER|REPLACE|EXAMPLE|YOUR[_-]|CHANGEME|"
    r"XXXX|\.\.\.|<[^>]*>|\$\{|\{[A-Za-z_]|token_hex|token_urlsafe|"
    r"^[-a-z_]+$|^[A-Z_]+$|read-token|control-token|ctrl-token|cli-control|"
    r"-456\b|-abc)"
)

# Text files that legitimately carry placeholder tokens or documentation.
TEXT_SUFFIXES = {
    ".py",
    ".sh",
    ".md",
    ".txt",
    ".toml",
    ".cfg",
    ".ini",
    ".yaml",
    ".yml",
    ".json",
    ".xml",
    ".example",
    "",
}
SKIP_PARTS = {"__pycache__"}


class Failure(Exception):
    """A release-blocking problem found in the artifacts."""


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def wheel_paths(dist: Path) -> list[Path]:
    wheels = sorted(dist.glob("*.whl"))
    if not wheels:
        raise Failure(f"no wheel found in {dist}")
    return wheels


def sdist_paths(dist: Path) -> list[Path]:
    tarballs = sorted(dist.glob("*.tar.gz"))
    if not tarballs:
        raise Failure(f"no sdist found in {dist}")
    return tarballs


def wheel_entries(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        return archive.namelist()


def sdist_entries(tarball: Path) -> list[str]:
    with tarfile.open(tarball) as archive:
        return archive.getnames()


def _declared_console_scripts(entry_points_text: str) -> set[str]:
    """Parse the ``[console_scripts]`` table out of entry_points.txt."""
    declared: set[str] = set()
    in_section = False
    for line in entry_points_text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_section = line == "[console_scripts]"
            continue
        if not in_section or not line or line.startswith("#"):
            continue
        name = line.split("=", 1)[0].strip()
        if name:
            declared.add(name)
    return declared


def check_console_scripts(wheel: Path) -> None:
    """The wheel metadata must declare exactly the supported entry points."""
    entry_points = next(
        (name for name in wheel_entries(wheel) if name.endswith(".dist-info/entry_points.txt")),
        None,
    )
    if entry_points is None:
        raise Failure(f"{wheel.name}: no entry_points.txt in the wheel")

    with zipfile.ZipFile(wheel) as archive:
        declared = _declared_console_scripts(archive.read(entry_points).decode("utf-8", "replace"))
    if declared != EXPECTED_CONSOLE_SCRIPTS:
        raise Failure(
            f"{wheel.name}: console scripts {sorted(declared)} != "
            f"expected {sorted(EXPECTED_CONSOLE_SCRIPTS)}"
        )


def check_retired_names(paths_by_artifact: dict[str, list[str]]) -> None:
    """The pre-rename alias must be gone from every shipped artifact."""
    offenders: list[str] = []
    for artifact, names in paths_by_artifact.items():
        for name in names:
            # Legacy *runtime directory* names are deliberately still migrated
            # by paths.py, so only the import package and console script count.
            if name.endswith(f"{RETIRED_CONSOLE_SCRIPT}.dist-info") or name.endswith(
                f"{RETIRED_CONSOLE_SCRIPT}/__init__.py"
            ):
                continue
            if RETIRED_IMPORT in name:
                offenders.append(f"{artifact}: {name}")
    if offenders:
        raise Failure(
            f"retired import package {RETIRED_IMPORT!r} still present:\n  " + "\n  ".join(offenders)
        )


def check_console_script_names(paths_by_artifact: dict[str, list[str]]) -> None:
    offenders = [
        f"{artifact}: {name}"
        for artifact, names in paths_by_artifact.items()
        for name in names
        if name.endswith(f"bin/{RETIRED_CONSOLE_SCRIPT}")
    ]
    if offenders:
        raise Failure(
            f"retired console script {RETIRED_CONSOLE_SCRIPT!r} still installed:\n  "
            + "\n  ".join(offenders)
        )


def _looks_like_a_real_credential(value: str) -> bool:
    """Filter the heuristic pattern down to values worth blocking a release."""
    if PLACEHOLDER_MARKERS.search(value):
        return False
    # A real tokens.token_urlsafe()/token_hex() value is long and mixes
    # character classes; identifiers and prose do not.
    if len(value) < 32:
        return False
    return (
        any(char.isdigit() for char in value)
        and any(char.isupper() for char in value)
        and any(char.islower() for char in value)
    )


def _scan_text(artifact: str, name: str, text: str, found: list[str]) -> None:
    for label, pattern in HIGH_CONFIDENCE_PATTERNS.items():
        for match in pattern.finditer(text):
            found.append(f"{artifact}:{name}: possible {label}: {match.group(0)[:60]}")

    # The heuristic is only meaningful for shipped runtime files; tests contain
    # deliberate fixtures and docs contain deliberate placeholders.
    is_runtime = not name.startswith("tests/") and "/tests/" not in name
    if not is_runtime:
        return
    for match in CREDENTIAL_ASSIGNMENT.finditer(text):
        value = match.group(1)
        if _looks_like_a_real_credential(value):
            found.append(f"{artifact}:{name}: possible assigned credential: {value[:40]}")


def check_no_secrets(dist: Path) -> None:
    found: list[str] = []
    for wheel in wheel_paths(dist):
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                if any(part in SKIP_PARTS for part in Path(name).parts):
                    continue
                if Path(name).suffix not in TEXT_SUFFIXES:
                    continue
                _scan_text(wheel.name, name, archive.read(name).decode("utf-8", "replace"), found)
    for tarball in sdist_paths(dist):
        with tarfile.open(tarball) as archive:
            for member in archive.getmembers():
                if not member.isfile() or Path(member.name).suffix not in TEXT_SUFFIXES:
                    continue
                if any(part in SKIP_PARTS for part in Path(member.name).parts):
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                _scan_text(tarball.name, member.name, _read_via(handle), found)
    if found:
        raise Failure("possible secrets in the artifacts:\n  " + "\n  ".join(found))


def _read_via(handle) -> str:
    return handle.read().decode("utf-8", "replace")


def check_versions_agree() -> None:
    """The Python distribution, the package and the Kindle extension must match."""
    sources = {
        "pyproject.toml": REPO_ROOT / "pyproject.toml",
        "__init__.py": REPO_ROOT / "src" / "hermes_eink_dashboard" / "__init__.py",
        "config.xml": REPO_ROOT / "kindle" / "hermes_dashboard" / "config.xml",
    }
    missing = [label for label, path in sources.items() if not path.is_file()]
    if missing:
        raise Failure(f"missing version source(s): {', '.join(missing)}")

    text = {label: _read(path) for label, path in sources.items()}

    declared = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text["pyproject.toml"])
    module = re.search(r'(?m)^__version__\s*=\s*"([^"]+)"', text["__init__.py"])
    kindle = re.search(r"<version>([^<]+)</version>", text["config.xml"])
    if not (declared and module and kindle):
        raise Failure("could not read all three version strings")

    versions = {
        "pyproject.toml": declared.group(1),
        "__init__.py": module.group(1),
        "config.xml": kindle.group(1),
    }
    if len(set(versions.values())) != 1:
        raise Failure(f"version mismatch: {versions}")


def main(argv: list[str]) -> int:
    dist = Path(argv[1]) if len(argv) > 1 else REPO_ROOT / "dist"
    if not dist.is_dir():
        print(f"error: {dist} is not a directory", file=sys.stderr)
        return 2

    checks = [
        ("console scripts", lambda: check_console_scripts(wheel_paths(dist)[0])),
        (
            "retired import package",
            lambda: check_retired_names(
                {wheel.name: wheel_entries(wheel) for wheel in wheel_paths(dist)}
                | {tar.name: sdist_entries(tar) for tar in sdist_paths(dist)}
            ),
        ),
        (
            "retired console script",
            lambda: check_console_script_names(
                {wheel.name: wheel_entries(wheel) for wheel in wheel_paths(dist)}
                | {tar.name: sdist_entries(tar) for tar in sdist_paths(dist)}
            ),
        ),
        ("no secrets", lambda: check_no_secrets(dist)),
        ("versions agree", check_versions_agree),
    ]

    failed = False
    for label, check in checks:
        try:
            check()
        except Failure as error:
            failed = True
            print(f"FAIL  {label}\n  {error}", file=sys.stderr)
        else:
            print(f"ok    {label}")

    if failed:
        return 1
    print(f"\nAll release checks passed for {dist}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
