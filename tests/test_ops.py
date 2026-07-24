import configparser
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_host_installer_rejects_environment_file_injection(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "sh",
            str(ROOT / "scripts/install_host.sh"),
            "--bind",
            "127.0.0.1\nINJECTED=true",
            "--no-start",
        ],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert not (tmp_path / ".config/hermes-kindle-dashboard").exists()


def test_systemd_unit_uses_private_config_and_restart_policy() -> None:
    unit = (ROOT / "systemd/hermes-kindle-dashboard.service").read_text()
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(unit)
    service = parser["Service"]
    assert service["EnvironmentFile"] == "%h/.config/hermes-kindle-dashboard/host.env"
    assert service["Environment"] == "PYTHONPATH="
    assert "%h/.local/share/hermes-kindle-dashboard/venv/bin/hermes-kindle-dashboard" in service["ExecStart"]
    assert service["Restart"] == "on-failure"
    assert service["NoNewPrivileges"] == "true"


def test_host_installer_configures_independent_refresh_interval() -> None:
    installer = (ROOT / "scripts/install_host.sh").read_text()

    assert "--refresh-seconds" in installer
    assert "HERMES_DASHBOARD_REFRESH_SECONDS=$REFRESH_SECONDS" in installer
    assert "HERMES_DASHBOARD_CACHE_SECONDS=" not in installer


def test_bundle_builder_injects_host_and_token_only_into_zip(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("abc123secure")
    output = tmp_path / "hermes-dashboard-kual.zip"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/build_kual_bundle.py"),
            "--host",
            "10.0.0.8",
            "--port",
            "9999",
            "--token-file",
            str(token_file),
            "--output",
            str(output),
        ],
        check=True,
    )

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert "hermes_dashboard/menu.json" in names
        assert "hermes_dashboard/bin/fetch.sh" in names
        config = archive.read("hermes_dashboard/config.sh").decode()
        assert 'HOST_IP="10.0.0.8"' in config
        assert 'HOST_PORT="9999"' in config
        assert 'DASHBOARD_TOKEN="abc123secure"' in config
        mode = archive.getinfo("hermes_dashboard/bin/fetch.sh").external_attr >> 16
        assert mode & 0o111

    assert output.stat().st_mode % 0o1000 == 0o600
    assert "abc123secure" not in (ROOT / "kindle/hermes_dashboard/config.sh.example").read_text()
