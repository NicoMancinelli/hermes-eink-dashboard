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


def test_bundle_builder_template_mode_uses_placeholders(tmp_path: Path) -> None:
    """Default mode produces a bundle with placeholder tokens; safe to publish."""
    output = tmp_path / "hermes-dashboard-kual.zip"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/build_kual_bundle.py"),
            "--output",
            str(output),
        ],
        check=True,
    )

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert "hermes_dashboard/menu.json" in names
        assert "hermes_dashboard/bin/fetch.sh" in names
        assert "hermes_dashboard/bin/post_install.sh" in names
        config = archive.read("hermes_dashboard/config.sh").decode()
        assert 'HOST_IP="PLACEHOLDER.lan"' in config
        assert 'DASHBOARD_TOKEN="PLACEHOLDER_TOKEN"' in config
        assert 'CONTROL_TOKEN=""' in config
        # The real pdi host must NEVER appear in a template bundle.
        assert "192.168.1.119" not in config
        mode = archive.getinfo("hermes_dashboard/bin/fetch.sh").external_attr >> 16
        assert mode & 0o111

    assert output.stat().st_mode % 0o1000 == 0o600


def test_bundle_builder_personal_mode_embeds_real_tokens(tmp_path: Path) -> None:
    """--inject-tokens mode embeds real tokens; not safe to publish."""
    token_file = tmp_path / "token"
    token_file.write_text("abc123secure")
    output = tmp_path / "hermes-dashboard-kual.zip"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/build_kual_bundle.py"),
            "--inject-tokens",
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
        config = archive.read("hermes_dashboard/config.sh").decode()
        assert 'HOST_IP="10.0.0.8"' in config
        assert 'HOST_PORT="9999"' in config
        assert 'DASHBOARD_TOKEN="abc123secure"' in config
        # Personal mode should never leak the placeholder default.
        assert 'HOST_IP="PLACEHOLDER.lan"' not in config

    assert "abc123secure" not in (ROOT / "kindle/hermes_dashboard/config.sh.example").read_text()


def test_post_install_script_replaces_placeholders(tmp_path: Path) -> None:
    """The bundle ships a post_install.sh that replaces placeholders in config.sh."""
    output = tmp_path / "hermes-dashboard-kual.zip"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_kual_bundle.py"), "--output", str(output)],
        check=True,
    )
    with zipfile.ZipFile(output) as archive:
        archive.extractall(tmp_path / "bundle")
    config_path = tmp_path / "bundle" / "hermes_dashboard" / "config.sh"
    assert config_path.exists()
    script = tmp_path / "bundle" / "hermes_dashboard" / "bin" / "post_install.sh"
    assert script.exists()
    subprocess.run(
        [
            "sh",
            str(script),
            "--host", "10.0.0.42",
            "--port", "9120",
            "--read-token", "tok",
            "--control-token", "ctrl",
        ],
        cwd=str(tmp_path / "bundle" / "hermes_dashboard"),
        check=True,
    )
    config_text = config_path.read_text()
    assert 'HOST_IP="10.0.0.42"' in config_text
    assert 'DASHBOARD_TOKEN="tok"' in config_text
    assert 'CONTROL_TOKEN="ctrl"' in config_text
    assert 'PLACEHOLDER_TOKEN' not in config_text



def test_host_installer_generates_control_token(tmp_path: Path) -> None:
    """The installer must create ~/.config/.../control_token and wire it into host.env."""
    subprocess.run(
        [
            "sh",
            str(ROOT / "scripts/install_host.sh"),
            "--bind", "127.0.0.1",
            "--no-start",
        ],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    config_dir = tmp_path / ".config/hermes-kindle-dashboard"
    assert (config_dir / "token").exists()
    assert (config_dir / "control_token").exists()
    # The control token must be hex (we use secrets.token_hex(32) → 64 chars).
    ctrl = (config_dir / "control_token").read_text().strip()
    assert len(ctrl) == 64
    int(ctrl, 16)  # parses as hex
    env_text = (config_dir / "host.env").read_text()
    assert "HERMES_DASHBOARD_CONTROL_TOKEN_FILE" in env_text
    assert str(config_dir / "control_token") in env_text
