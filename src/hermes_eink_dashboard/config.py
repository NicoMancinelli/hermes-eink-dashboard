from __future__ import annotations

import re
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, computed_field, field_validator


class ConfigSchema(BaseModel):
    """Declarative configuration schema for the KUAL client config.sh."""

    # Required keys (must be provided)
    host_ip: str = Field(
        ...,
        description="LAN/tailnet address of the host running hermes-kindle-dashboard",
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    host_port: int = Field(default=9120, ge=1, le=65535, description="HTTP port on the host")
    dashboard_token: str = Field(
        ...,
        description="Read token for /dashboard.png and /dashboard.json",
        min_length=1,
        pattern=r"^[A-Za-z0-9._~-]+$",
    )

    # Optional keys (Phase 5+ interactive controls)
    control_token: str = Field(
        default="",
        description="Write token for /control and /control/events. Leave empty to disable write endpoints.",
        pattern=r"^[A-Za-z0-9._~-]*$",
    )

    # E-Ink/power settings
    refresh_interval: int = Field(default=45, ge=5, le=3600, description="Seconds between dashboard refreshes")
    download_timeout: int = Field(default=12, ge=1, le=120, description="Per-request timeout in seconds")
    full_refresh_every: int = Field(default=10, ge=1, le=1000, description="Force full refresh every N cycles")
    keep_awake: int = Field(default=1, ge=0, le=1, description="Keep Kindle awake while dashboard runs (0/1)")
    stop_framework: int = Field(default=1, ge=0, le=1, description="Stop Kindle framework while dashboard runs (0/1)")

    # Usually auto-detected
    fbink: str = Field(default="", description="Override path to fbink (auto-detected by default)")

    # Fixed values that should not be changed
    dashboard_url_template: str = Field(
        default="http://${HOST_IP}:${HOST_PORT}/dashboard.png?token=${DASHBOARD_TOKEN}",
        description="URL template (do not modify)",
    )

    @field_validator("host_ip")
    @classmethod
    def validate_host_ip(cls, v: str) -> str:
        if not re.fullmatch(r"^[A-Za-z0-9._:-]+$", v):
            raise ValueError("host_ip may contain only letters, numbers, dots, colons, underscores, and hyphens")
        return v

    @field_validator("dashboard_token", "control_token")
    @classmethod
    def validate_token(cls, v: str, info) -> str:
        field_name = info.field_name
        if field_name == "dashboard_token" and not v:
            raise ValueError("dashboard_token is required and must be non-empty")
        if v and not re.fullmatch(r"^[A-Za-z0-9._~-]+$", v):
            raise ValueError(f"{field_name} must be URL-safe (alphanumeric, dot, underscore, tilde, hyphen)")
        return v

    def to_config_sh(self, template: str | None = None) -> str:
        """Render the config.sh from template using this configuration."""
        if template is None:
            template = DEFAULT_TEMPLATE

        replacements = {
            'HOST_IP="HOST_IP"': f'HOST_IP="{self.host_ip}"',
            'HOST_PORT="9120"': f'HOST_PORT="{self.host_port}"',
            'DASHBOARD_TOKEN="CHANGE_ME"': f'DASHBOARD_TOKEN="{self.dashboard_token}"',
            'CONTROL_TOKEN=""': f'CONTROL_TOKEN="{self.control_token}"',
            'REFRESH_INTERVAL="45"': f'REFRESH_INTERVAL="{self.refresh_interval}"',
            'DOWNLOAD_TIMEOUT="12"': f'DOWNLOAD_TIMEOUT="{self.download_timeout}"',
            'FULL_REFRESH_EVERY="10"': f'FULL_REFRESH_EVERY="{self.full_refresh_every}"',
            'KEEP_AWAKE="1"': f'KEEP_AWAKE="{self.keep_awake}"',
            'STOP_FRAMEWORK="1"': f'STOP_FRAMEWORK="{self.stop_framework}"',
            'FBINK=""': f'FBINK="{self.fbink}"',
        }

        result = template
        for old, new in replacements.items():
            if old in result:
                result = result.replace(old, new)
            else:
                # Pattern might have already been replaced; try with the current value
                pass

        return result

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dashboard_url(self) -> str:
        """Computed full URL the Kindle client should hit."""
        return f"http://{self.host_ip}:{self.host_port}/dashboard.png?token={self.dashboard_token}"


DEFAULT_TEMPLATE = """# Hermes Dashboard Kindle client configuration.
# Copy to config.sh and replace HOST_IP and CHANGE_ME.
#
# Required keys:
#   HOST_IP          - LAN/tailnet address of the host running hermes-kindle-dashboard.
#   HOST_PORT        - HTTP port on the host (default 9120).
#   DASHBOARD_TOKEN  - Read token. Used for /dashboard.png and /dashboard.json.
#
# Optional keys (Phase 5+):
#   CONTROL_TOKEN    - Write token. Required for /control and /control/events.
#                      Generate with:
#                        python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
#                      and add to host.env as HERMES_DASHBOARD_CONTROL_TOKEN.
#                      Leave empty to disable interactive controls (write endpoints
#                      return 503 service unavailable).
#   REFRESH_INTERVAL - Seconds between dashboard refreshes (default 45).
#   DOWNLOAD_TIMEOUT - Per-request timeout in seconds (default 12).
#   FULL_REFRESH_EVERY - Force a full (non-partial) refresh every N cycles (default 10).
#   KEEP_AWAKE       - Set to 1 to keep the Kindle awake while the dashboard is running.
#   STOP_FRAMEWORK   - Set to 1 to stop the Kindle framework while the dashboard runs.
#   FBINK            - Override path to fbink (auto-detected by default).
#
HOST_IP="HOST_IP"
HOST_PORT="9120"
DASHBOARD_TOKEN="CHANGE_ME"
DASHBOARD_URL="http://${HOST_IP}:${HOST_PORT}/dashboard.png?token=${DASHBOARD_TOKEN}"

# Phase 5+ interactive controls (optional).
# Generate a separate token for write access with:
#   python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
# Leave empty to disable /control endpoints (they return 503 service unavailable).
CONTROL_TOKEN=""

# E-Ink/power settings.
REFRESH_INTERVAL="45"
DOWNLOAD_TIMEOUT="12"
FULL_REFRESH_EVERY="10"
KEEP_AWAKE="1"
STOP_FRAMEWORK="1"

# Usually auto-detected. Set an absolute path if FBInk lives elsewhere.
FBINK=""
"""


@dataclass
class ConfigManager:
    """Manages the declarative configuration file and config.sh regeneration."""

    config_path: Path = field(
        default_factory=lambda: Path("~/.config/hermes-kindle-dashboard/config.yaml").expanduser()
    )
    template_path: Path = field(
        default_factory=lambda: ConfigManager._resolve_template_path()
    )

    @property
    def safe_output_path(self) -> Path:
        """Path where regenerated config.sh is written in production.

        Lives outside the project tree so HTTP-driven regeneration cannot
        pollute the tracked source. The bundle builder overrides this with
        its own destination via ``regenerate_config_sh(output_path=...)``.
        """
        return Path("~/.config/hermes-kindle-dashboard/rendered/config.sh").expanduser().resolve()

    @property
    def output_path(self) -> Path:
        """Backwards-compat alias for the legacy on-tree output path."""
        return self.safe_output_path

    @staticmethod
    def _resolve_template_path() -> Path:
        """Find the config.sh.example template.

        Resolution order:
        1. ``HERMES_DASHBOARD_PROJECT_ROOT`` env var (set in production).
        2. Walk up from this file looking for a sibling ``kindle/`` directory
           (covers editable installs and source checkouts).
        3. Final fallback: relative to this file's grandparent.
        """
        import os
        env_root = os.environ.get("HERMES_DASHBOARD_PROJECT_ROOT")
        if env_root:
            candidate = Path(env_root).expanduser() / "kindle" / "hermes_dashboard" / "config.sh.example"
            if candidate.exists():
                return candidate.resolve()
        current = Path(__file__).resolve().parent
        for _ in range(8):
            candidate = current / "kindle" / "hermes_dashboard" / "config.sh.example"
            if candidate.exists():
                return candidate.resolve()
            current = current.parent
        return (Path(__file__).resolve().parent.parent.parent / "kindle" / "hermes_dashboard" / "config.sh.example")

    def __post_init__(self) -> None:
        self.config_path = self.config_path.expanduser()
        self.template_path = self.template_path.expanduser().resolve()

    def load(self) -> ConfigSchema | None:
        """Load configuration from YAML file."""
        if not self.config_path.exists():
            return None
        try:
            content = self.config_path.read_text(encoding="utf-8")
            data = yaml.safe_load(content)
            if data is None:
                return None
            return ConfigSchema(**data)
        except (yaml.YAMLError, OSError) as e:
            raise RuntimeError(f"Failed to load config from {self.config_path}: {e}")

    def load_template(self) -> str:
        """Load the config.sh.example template."""
        try:
            return self.template_path.read_text(encoding="utf-8")
        except OSError as e:
            raise RuntimeError(f"Failed to load template from {self.template_path}: {e}")

    def save(self, config: ConfigSchema) -> None:
        """Save configuration to YAML file."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        # Use model_dump with exclude_none to omit optional empty fields
        data = config.model_dump(exclude_none=True)
        # Don't save the template field
        data.pop("dashboard_url_template", None)
        content = yaml.dump(data, default_flow_style=False, sort_keys=False)
        self.config_path.write_text(content, encoding="utf-8")

    def regenerate_config_sh(self, config: ConfigSchema, *, output_path: Path | None = None) -> str:
        """Regenerate config.sh from template using the provided configuration.

        The default output path is :attr:`safe_output_path`, which lives outside
        the project tree. Callers may pass ``output_path`` to override the
        destination (used by the bundle builder, which writes into a temporary
        directory).
        """
        template = self.load_template()
        rendered = config.to_config_sh(template)
        target = output_path or self.safe_output_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
        target.chmod(0o600)
        return rendered

    def get_example_config(self) -> str:
        """Get an example YAML configuration."""
        example = ConfigSchema(
            host_ip="192.168.1.100",
            host_port=9120,
            dashboard_token="your-read-token-here",
            control_token="your-control-token-here",
            refresh_interval=45,
            download_timeout=12,
            full_refresh_every=10,
            keep_awake=1,
            stop_framework=1,
            fbink="",
        )
        data = example.model_dump(exclude_none=True)
        data.pop("dashboard_url_template", None)
        return yaml.dump(data, default_flow_style=False, sort_keys=False)