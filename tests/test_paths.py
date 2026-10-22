"""Runtime path resolution across the rename."""

from __future__ import annotations

from pathlib import Path

from hermes_eink_dashboard import paths


def _with_xdg(tmp_path: Path, monkeypatch) -> Path:
    config_home = tmp_path / ".config"
    data_home = tmp_path / ".local" / "share"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    return config_home


def test_config_dir_defaults_to_current_name(tmp_path: Path, monkeypatch) -> None:
    config_home = _with_xdg(tmp_path, monkeypatch)
    assert paths.app_config_dir() == config_home / "hermes-eink-dashboard"
    # Nothing exists yet: the current name wins.
    assert paths.resolve_config_dir() == config_home / "hermes-eink-dashboard"


def test_resolve_config_dir_falls_back_to_legacy_install(tmp_path: Path, monkeypatch) -> None:
    config_home = _with_xdg(tmp_path, monkeypatch)
    legacy = config_home / "hermes-kindle-dashboard"
    legacy.mkdir(parents=True)
    (legacy / "token").write_text("legacy")
    assert paths.resolve_config_dir() == legacy


def test_resolve_config_dir_prefers_current_install(tmp_path: Path, monkeypatch) -> None:
    config_home = _with_xdg(tmp_path, monkeypatch)
    (config_home / "hermes-kindle-dashboard").mkdir(parents=True)
    (config_home / "hermes-eink-dashboard").mkdir(parents=True)
    assert paths.resolve_config_dir() == config_home / "hermes-eink-dashboard"


def test_data_dir_uses_xdg_data_home(tmp_path: Path, monkeypatch) -> None:
    _with_xdg(tmp_path, monkeypatch)
    assert paths.app_data_dir() == tmp_path / ".local" / "share" / "hermes-eink-dashboard"
    assert paths.legacy_app_data_dir() == tmp_path / ".local" / "share" / "hermes-kindle-dashboard"


def test_server_token_default_uses_resolved_config_dir(tmp_path: Path, monkeypatch) -> None:
    """The CLI default token path must follow the resolved config directory."""
    from hermes_eink_dashboard.server import build_parser

    config_home = _with_xdg(tmp_path, monkeypatch)
    legacy = config_home / "hermes-kindle-dashboard"
    legacy.mkdir(parents=True)
    monkeypatch.delenv("HERMES_DASHBOARD_TOKEN_FILE", raising=False)
    monkeypatch.delenv("HERMES_DASHBOARD_CONTROL_TOKEN_FILE", raising=False)
    monkeypatch.delenv("HERMES_DASHBOARD_DEVICES_FILE", raising=False)

    args = build_parser().parse_args([])
    assert args.token_file == legacy / "token"
    assert args.control_token_file == legacy / "control_token"
    assert args.devices_file == legacy / "devices.json"

    current = config_home / "hermes-eink-dashboard"
    current.mkdir(parents=True)
    args = build_parser().parse_args([])
    assert args.token_file == current / "token"


def test_environment_overrides_still_win(tmp_path: Path, monkeypatch) -> None:
    from hermes_eink_dashboard.server import build_parser

    _with_xdg(tmp_path, monkeypatch)
    override = tmp_path / "custom-token"
    monkeypatch.setenv("HERMES_DASHBOARD_TOKEN_FILE", str(override))
    args = build_parser().parse_args([])
    assert args.token_file == override
