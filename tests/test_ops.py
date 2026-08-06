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
    assert not (tmp_path / ".config/hermes-eink-dashboard").exists()


def test_systemd_unit_uses_private_config_and_restart_policy() -> None:
    unit = (ROOT / "systemd/hermes-eink-dashboard.service").read_text()
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(unit)
    service = parser["Service"]
    assert service["EnvironmentFile"] == "%h/.config/hermes-eink-dashboard/host.env"
    assert service["Environment"] == "PYTHONPATH="
    assert "%h/.local/share/hermes-eink-dashboard/venv/bin/hermes-eink-dashboard" in service["ExecStart"]
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
    config_dir = tmp_path / ".config/hermes-eink-dashboard"
    assert (config_dir / "token").exists()
    assert (config_dir / "control_token").exists()
    # The control token must be hex (we use secrets.token_hex(32) → 64 chars).
    ctrl = (config_dir / "control_token").read_text().strip()
    assert len(ctrl) == 64
    int(ctrl, 16)  # parses as hex
    env_text = (config_dir / "host.env").read_text()
    assert "HERMES_DASHBOARD_CONTROL_TOKEN_FILE" in env_text
    assert str(config_dir / "control_token") in env_text


def test_host_installer_migrates_pre_consolidation_config(tmp_path: Path) -> None:
    """A pre-consolidation ~/.config/hermes-kindle-dashboard install is migrated.

    The old config dir (tokens + host.env) must move to the new
    hermes-eink-dashboard location, preserving the existing tokens instead of
    generating fresh ones, and the old dir must no longer exist.
    """
    old_config = tmp_path / ".config/hermes-kindle-dashboard"
    old_config.mkdir(parents=True)
    old_config.chmod(0o700)
    (old_config / "token").write_text("preexisting-read-token")
    (old_config / "control_token").write_text("a" * 64)

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

    new_config = tmp_path / ".config/hermes-eink-dashboard"
    # Old location is gone; new location carries the *original* tokens.
    assert not old_config.exists()
    assert (new_config / "token").read_text() == "preexisting-read-token"
    assert (new_config / "control_token").read_text() == "a" * 64
    # host.env is regenerated to point at the new location.
    env_text = (new_config / "host.env").read_text()
    assert str(new_config / "token") in env_text



def test_post_install_then_start_interactive_e2e(tmp_path: Path) -> None:
    """Bundle → post_install → start_interactive end-to-end on the local filesystem.

    Builds a template bundle, extracts it, runs post_install.sh with real
    token args, then runs start_interactive.sh against a stub python3 and
    verifies the launcher invokes the client with the right argv. The
    client itself exits cleanly because the input devices don't exist on
    pdi; we just verify the launcher constructed the right command line.
    """
    import subprocess
    import zipfile

    bundle = tmp_path / "bundle.zip"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_kual_bundle.py"), "--output", str(bundle)],
        check=True,
    )
    extract_dir = tmp_path / "extracted"
    with zipfile.ZipFile(bundle) as archive:
        archive.extractall(extract_dir)
    ext = extract_dir / "hermes_dashboard"
    assert (ext / "bin" / "interactive_client.py").exists()
    assert (ext / "bin" / "start_interactive.sh").exists()
    assert (ext / "bin" / "stop_interactive.sh").exists()

    # Replace /mnt paths with tmp paths so the scripts work without root.
    docs = tmp_path / "documents"
    state = tmp_path / "state"
    docs.mkdir()
    state.mkdir()
    start_script = (ext / "bin" / "start_interactive.sh").read_text()
    start_script = start_script.replace("/mnt/us/extensions/hermes_dashboard", str(ext))
    start_script = start_script.replace("/mnt/us/documents", str(docs))
    start_script = start_script.replace("/tmp/hermes_dashboard", str(state))
    # Inject a stub python3 by using the venv's python3 (works as a stand-in
    # because we're only validating the launcher logic).
    stub_python = sys.executable
    # Replace the list of python3 candidates with a guaranteed-miss list
    # followed by the real interpreter, so the launcher picks it up.
    start_script = start_script.replace(
        '"/mnt/us/python3/bin/python3"',
        '"/nonexistent/python3"',
    )
    (ext / "bin" / "start_interactive.sh").write_text(start_script)

    # Populate config.sh with real-looking tokens via post_install.sh.
    subprocess.run(
        [
            "sh",
            str(ext / "bin" / "post_install.sh"),
            "--host", "10.0.0.42",
            "--read-token", "rt",
            "--control-token", "ct",
        ],
        cwd=str(ext),
        check=True,
    )
    config_text = (ext / "config.sh").read_text()
    assert 'HOST_IP="10.0.0.42"' in config_text
    assert 'DASHBOARD_TOKEN="rt"' in config_text
    assert 'CONTROL_TOKEN="ct"' in config_text

    # Run the launcher. It will exit because /dev/input/event0 doesn't exist
    # in the test env, but the LOG should show the launcher constructed
    # the right argv. We verify via the log, not by waiting for the process.
    subprocess.run(
        ["sh", str(ext / "bin" / "start_interactive.sh")],
        check=False,
    )
    log_text = (docs / "hermes-dashboard.log").read_text()
    assert "starting interactive client" in log_text
    assert "10.0.0.42" in log_text
    assert "interactive_client.py" in log_text
    assert "--control-token ct" in log_text
