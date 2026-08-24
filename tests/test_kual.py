import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
EXTENSION = ROOT / "kindle" / "hermes_dashboard"


def test_kual_extension_contains_required_actions_and_scripts() -> None:
    # Files that MUST be present in the source tree (the bundle builder
    # copies these verbatim).
    required_source = {
        "config.xml",
        "menu.json",
        "config.sh.example",
        "bin/start.sh",
        "bin/fetch.sh",
        "bin/refresh.sh",
        "bin/stop.sh",
        "bin/start_interactive.sh",
        "bin/stop_interactive.sh",
        "bin/post_install.sh",
        "bin/start_wizard.sh",
    }
    present = {str(path.relative_to(EXTENSION)) for path in EXTENSION.rglob("*") if path.is_file()}
    assert required_source <= present

    menu = json.loads((EXTENSION / "menu.json").read_text())
    root = menu["items"][0]
    assert root["name"] == "Hermes Dashboard"
    actions = {item["name"]: item["params"] for item in root["items"]}
    # Required menu entries.
    assert actions["Setup Wizard (pair Kindle)"] == "/mnt/us/extensions/hermes_dashboard/bin/start_wizard.sh"
    assert actions["Start Dashboard (read-only)"] == "/mnt/us/extensions/hermes_dashboard/bin/start.sh"
    assert actions["Start Interactive Dashboard"] == "/mnt/us/extensions/hermes_dashboard/bin/start_interactive.sh"
    assert actions["Manual Refresh"] == "/mnt/us/extensions/hermes_dashboard/bin/refresh.sh"
    assert actions["Stop Dashboard"] == "/mnt/us/extensions/hermes_dashboard/bin/stop.sh"
    assert actions["Stop Interactive Dashboard"] == "/mnt/us/extensions/hermes_dashboard/bin/stop_interactive.sh"
    assert actions["Run Post-Install (configure host + tokens)"] == "/mnt/us/extensions/hermes_dashboard/bin/post_install.sh"


def test_kindle_scripts_are_posix_shell_and_have_lifecycle_guards() -> None:
    for path in sorted((EXTENSION / "bin").glob("*.sh")):
        assert path.read_text().startswith("#!/bin/sh")
        result = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, f"{path}: {result.stderr}"

    start = (EXTENSION / "bin/start.sh").read_text()
    fetch = (EXTENSION / "bin/fetch.sh").read_text()
    stop = (EXTENSION / "bin/stop.sh").read_text()
    assert "preventScreenSaver 1" in start
    assert "nohup" in start and "PIDFILE" in start
    assert "stop framework" in start
    assert "wget" in fetch and "fbink" in fetch.lower()
    assert "OFFLINE" in fetch and "-t" in fetch
    assert "preventScreenSaver 0" in stop
    assert "start framework" in stop


def test_committed_kindle_config_is_generic_and_secret_free() -> None:
    config = (EXTENSION / "config.sh.example").read_text()
    assert "HOST_IP" in config
    assert "CHANGE_ME" in config
    assert "192.168." not in config
    assert "100.100." not in config
    assert "neek" not in config.lower()



def test_built_bundle_includes_interactive_client() -> None:
    """The bundle builder must include the Python interactive client."""
    import subprocess
    import sys
    import tempfile
    import zipfile

    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "test-bundle.zip"
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/build_kual_bundle.py"), "--output", str(output)],
            check=True,
        )
        with zipfile.ZipFile(output) as archive:
            names = set(archive.namelist())
            assert "hermes_dashboard/bin/interactive_client.py" in names
            assert "hermes_dashboard/bin/start_interactive.sh" in names
            assert "hermes_dashboard/bin/stop_interactive.sh" in names
            assert "hermes_dashboard/bin/post_install.sh" in names
