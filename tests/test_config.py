"""Tests for the declarative configuration schema and the ``ConfigManager``.

``test_config_api.py`` exercises ``ConfigManager`` only through the HTTP layer
and with ``load_template()`` stubbed out, so the schema validators, template
resolution and YAML round-tripping were untested. These cover them directly.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from hermes_eink_dashboard.config import (
    DEFAULT_TEMPLATE,
    ConfigManager,
    ConfigSchema,
)

MINIMAL = {"host_ip": "192.168.1.10", "dashboard_token": "read-token"}


def _schema(**overrides) -> ConfigSchema:
    return ConfigSchema(**{**MINIMAL, **overrides})


class TestSchemaDefaults:
    def test_required_keys_only_uses_documented_defaults(self) -> None:
        config = _schema()
        assert config.host_port == 9120
        assert config.control_token == ""
        assert config.refresh_interval == 45
        assert config.download_timeout == 12
        assert config.full_refresh_every == 10
        assert config.keep_awake == 1
        assert config.stop_framework == 1
        assert config.fbink == ""

    def test_missing_required_key_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ConfigSchema(host_ip="192.168.1.10")
        with pytest.raises(ValidationError):
            ConfigSchema(dashboard_token="read-token")

    def test_numeric_bounds_are_enforced(self) -> None:
        for field, bad in [
            ("host_port", 0),
            ("host_port", 70000),
            ("refresh_interval", 4),
            ("refresh_interval", 3601),
            ("download_timeout", 0),
            ("download_timeout", 121),
            ("full_refresh_every", 0),
            ("keep_awake", 2),
            ("stop_framework", -1),
        ]:
            with pytest.raises(ValidationError):
                _schema(**{field: bad})


class TestHostIpValidator:
    @pytest.mark.parametrize(
        "value",
        ["192.168.1.10", "host.local", "my-host", "a_b", "fe80::1", "10.0.0.5:9120"],
    )
    def test_accepts_valid_addresses(self, value: str) -> None:
        assert _schema(host_ip=value).host_ip == value

    @pytest.mark.parametrize("value", ["has space", "slash/es", "q?uery", "semi;colon", ""])
    def test_rejects_unsafe_addresses(self, value: str) -> None:
        with pytest.raises(ValidationError):
            _schema(host_ip=value)

    def test_injection_attempt_is_rejected(self) -> None:
        """A shell metacharacter must never reach config.sh."""
        with pytest.raises(ValidationError):
            _schema(host_ip='192.168.1.10"; rm -rf /; x="')


class TestTokenValidator:
    def test_empty_read_token_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _schema(dashboard_token="")

    def test_empty_control_token_is_allowed(self) -> None:
        """An empty control token disables the write endpoints, not the schema."""
        assert _schema(control_token="").control_token == ""

    @pytest.mark.parametrize("value", ["abc123", "a.b_c~d-e", "x" * 64])
    def test_url_safe_tokens_are_accepted(self, value: str) -> None:
        assert _schema(dashboard_token=value).dashboard_token == value

    @pytest.mark.parametrize(
        "value", ["with space", 'quote"', "dollar$", "semi;colon", "new\nline"]
    )
    def test_non_url_safe_tokens_are_rejected(self, value: str) -> None:
        with pytest.raises(ValidationError):
            _schema(dashboard_token=value)

    def test_control_token_is_validated_too(self) -> None:
        with pytest.raises(ValidationError):
            _schema(control_token="bad token")


class TestDashboardUrl:
    def test_computed_from_host_port_and_token(self) -> None:
        url = _schema(host_ip="10.0.0.7", host_port=8080, dashboard_token="tok").dashboard_url
        assert url == "http://10.0.0.7:8080/dashboard.png?token=tok"

    def test_appears_in_the_model_dump(self) -> None:
        dumped = _schema().model_dump()
        assert dumped["dashboard_url"] == "http://192.168.1.10:9120/dashboard.png?token=read-token"


class TestToConfigSh:
    def test_substitutes_every_documented_key(self) -> None:
        config = _schema(
            host_ip="10.1.2.3",
            host_port=9999,
            dashboard_token="read-tok",
            control_token="write-tok",
            refresh_interval=60,
            download_timeout=30,
            full_refresh_every=5,
            keep_awake=0,
            stop_framework=0,
            fbink="/mnt/us/fbink/fbink",
        )
        rendered = config.to_config_sh()
        assert 'HOST_IP="10.1.2.3"' in rendered
        assert 'HOST_PORT="9999"' in rendered
        assert 'DASHBOARD_TOKEN="read-tok"' in rendered
        assert 'CONTROL_TOKEN="write-tok"' in rendered
        assert 'REFRESH_INTERVAL="60"' in rendered
        assert 'DOWNLOAD_TIMEOUT="30"' in rendered
        assert 'FULL_REFRESH_EVERY="5"' in rendered
        assert 'KEEP_AWAKE="0"' in rendered
        assert 'STOP_FRAMEWORK="0"' in rendered
        assert 'FBINK="/mnt/us/fbink/fbink"' in rendered

    def test_leaves_no_placeholder_behind(self) -> None:
        rendered = _schema().to_config_sh()
        assert 'HOST_IP="HOST_IP"' not in rendered
        assert 'DASHBOARD_TOKEN="CHANGE_ME"' not in rendered

    def test_custom_template_is_used_when_supplied(self) -> None:
        template = 'HOST_IP="HOST_IP"\nDASHBOARD_TOKEN="CHANGE_ME"\nEXTRA="untouched"\n'
        rendered = _schema(host_ip="10.0.0.9", dashboard_token="tok").to_config_sh(template)
        assert 'HOST_IP="10.0.0.9"' in rendered
        assert 'DASHBOARD_TOKEN="tok"' in rendered
        assert 'EXTRA="untouched"' in rendered

    def test_template_without_known_keys_is_returned_unchanged(self) -> None:
        template = "# nothing to substitute\n"
        assert _schema().to_config_sh(template) == template

    def test_default_template_keeps_the_url_indirection(self) -> None:
        """DASHBOARD_URL must stay unexpanded: config.sh expands it at runtime."""
        rendered = _schema().to_config_sh()
        assert (
            'DASHBOARD_URL="http://${HOST_IP}:${HOST_PORT}/dashboard.png?token=${DASHBOARD_TOKEN}"'
            in rendered
        )


class TestTemplateResolution:
    def test_prefers_the_environment_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "kindle" / "hermes_dashboard"
        target.mkdir(parents=True)
        template = target / "config.sh.example"
        template.write_text("# from env\n", encoding="utf-8")

        monkeypatch.setenv("HERMES_DASHBOARD_PROJECT_ROOT", str(tmp_path))
        assert ConfigManager._resolve_template_path() == template.resolve()

    def test_env_override_pointing_nowhere_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HERMES_DASHBOARD_PROJECT_ROOT", str(tmp_path / "does-not-exist"))
        # The repo checkout has kindle/hermes_dashboard/config.sh.example, so the
        # upward walk finds it rather than returning the empty fallback path.
        resolved = ConfigManager._resolve_template_path()
        assert resolved.name == "config.sh.example"
        assert resolved.exists()

    def test_repo_template_is_the_real_one(self) -> None:
        resolved = ConfigManager._resolve_template_path()
        assert resolved.is_file()
        assert "HOST_IP" in resolved.read_text(encoding="utf-8")


class TestLoad:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        manager = ConfigManager(config_path=tmp_path / "absent.yaml")
        assert manager.load() is None

    def test_empty_file_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("", encoding="utf-8")
        assert ConfigManager(config_path=path).load() is None

    def test_yaml_null_document_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("---\n", encoding="utf-8")
        assert ConfigManager(config_path=path).load() is None

    def test_valid_file_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        manager = ConfigManager(config_path=path)
        manager.save(_schema(control_token="write-tok"))
        loaded = manager.load()
        assert loaded is not None
        assert loaded.host_ip == MINIMAL["host_ip"]
        assert loaded.control_token == "write-tok"

    def test_invalid_schema_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.dump({"host_ip": "bad host", "dashboard_token": "t"}), encoding="utf-8"
        )
        with pytest.raises(ValidationError):
            ConfigManager(config_path=path).load()

    def test_malformed_yaml_raises_runtime_error_naming_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("host_ip: [unclosed\n", encoding="utf-8")
        with pytest.raises(RuntimeError) as excinfo:
            ConfigManager(config_path=path).load()
        assert "Failed to load config" in str(excinfo.value)
        assert str(path) in str(excinfo.value)


class TestSave:
    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "deep" / "config.yaml"
        ConfigManager(config_path=path).save(_schema())
        assert path.is_file()

    def test_does_not_persist_the_fixed_url_template(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        ConfigManager(config_path=path).save(_schema())
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "dashboard_url_template" not in data

    def test_written_file_reloads_identically(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        manager = ConfigManager(config_path=path)
        original = _schema(fbink="/usr/bin/fbink", full_refresh_every=3)
        manager.save(original)
        reloaded = manager.load()
        assert reloaded is not None
        assert reloaded.model_dump(exclude={"dashboard_url"}) == original.model_dump(
            exclude={"dashboard_url"}
        )

    def test_tilde_in_paths_is_expanded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", "/home/tester")
        manager = ConfigManager(config_path=Path("~/config.yaml"))
        assert "~" not in str(manager.config_path)


class TestRegenerateConfigSh:
    def _manager(self, tmp_path: Path, template_text: str = DEFAULT_TEMPLATE) -> ConfigManager:
        template = tmp_path / "config.sh.example"
        template.write_text(template_text, encoding="utf-8")
        return ConfigManager(config_path=tmp_path / "config.yaml", template_path=template)

    def test_writes_the_rendered_file_and_returns_it(self, tmp_path: Path) -> None:
        out = tmp_path / "out" / "config.sh"
        manager = self._manager(tmp_path)
        rendered = manager.regenerate_config_sh(_schema(host_ip="10.0.0.1"), output_path=out)
        assert out.read_text(encoding="utf-8") == rendered
        assert 'HOST_IP="10.0.0.1"' in rendered

    def test_output_is_owner_only(self, tmp_path: Path) -> None:
        """The rendered file embeds tokens, so it must not be group/world readable."""
        out = tmp_path / "config.sh"
        self._manager(tmp_path).regenerate_config_sh(_schema(), output_path=out)
        mode = stat.S_IMODE(out.stat().st_mode)
        assert mode == 0o600

    def test_overwrites_a_stale_render(self, tmp_path: Path) -> None:
        out = tmp_path / "config.sh"
        out.write_text('HOST_IP="stale"\n', encoding="utf-8")
        manager = self._manager(tmp_path)
        manager.regenerate_config_sh(_schema(host_ip="10.0.0.2"), output_path=out)
        assert 'HOST_IP="10.0.0.2"' in out.read_text(encoding="utf-8")

    def test_default_output_path_is_outside_the_source_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_home = tmp_path / "config"
        config_home.mkdir()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
        out = self._manager(tmp_path).safe_output_path
        assert out == (config_home / "hermes-eink-dashboard" / "rendered" / "config.sh")
        assert out.parent.name == "rendered"

    def test_output_path_is_an_alias_of_safe_output_path(self, tmp_path: Path) -> None:
        manager = self._manager(tmp_path)
        assert manager.output_path == manager.safe_output_path

    def test_missing_template_raises_a_named_runtime_error(self, tmp_path: Path) -> None:
        manager = ConfigManager(
            config_path=tmp_path / "config.yaml",
            template_path=tmp_path / "no-such-template",
        )
        with pytest.raises(RuntimeError) as excinfo:
            manager.regenerate_config_sh(_schema())
        assert "Failed to load template" in str(excinfo.value)
        assert "no-such-template" in str(excinfo.value)


class TestExampleConfig:
    def test_is_valid_and_self_describing(self, tmp_path: Path) -> None:
        example = ConfigManager(config_path=tmp_path / "config.yaml").get_example_config()
        parsed = yaml.safe_load(example)
        assert parsed["host_ip"] == "192.168.1.100"
        assert "dashboard_url_template" not in parsed
        assert "dashboard_url" not in parsed

    def test_round_trips_through_the_schema(self, tmp_path: Path) -> None:
        manager = ConfigManager(config_path=tmp_path / "config.yaml")
        manager.config_path.write_text(manager.get_example_config(), encoding="utf-8")
        assert manager.load() is not None


class TestShippedExampleStaysInSync:
    """The bundled example and the embedded template are one artifact.

    They are two copies of the same file: ``config.sh.example`` ships to the
    Kindle, and ``DEFAULT_TEMPLATE`` is what a fresh install renders. If they
    drift, a new user gets a different config than an existing one.
    """

    def test_shipped_example_matches_the_embedded_template(self) -> None:
        example = (
            Path(__file__).resolve().parents[1]
            / "kindle"
            / "hermes_dashboard"
            / "config.sh.example"
        )
        assert example.read_text(encoding="utf-8") == DEFAULT_TEMPLATE

    def test_every_documented_key_is_referenced_by_the_shell_scripts(self) -> None:
        """A key documented in config.sh.example must actually be read."""
        example = (
            Path(__file__).resolve().parents[1]
            / "kindle"
            / "hermes_dashboard"
            / "config.sh.example"
        )
        documented = {
            line.split("=", 1)[0].strip()
            for line in example.read_text(encoding="utf-8").splitlines()
            if re.match(r"^[A-Z_][A-Z0-9_]*=", line)
        }
        scripts = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                Path(__file__).resolve().parents[1] / "kindle" / "hermes_dashboard" / "bin"
            ).glob("*.sh")
        )
        for key in documented:
            assert key in scripts, f"{key} is documented but never read by the Kindle scripts"
