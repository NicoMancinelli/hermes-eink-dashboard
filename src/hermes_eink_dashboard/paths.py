"""Filesystem locations used by the host service.

The project was renamed from ``hermes-kindle-dashboard`` to
``hermes-eink-dashboard`` (the Kindle client is one adapter among several
possible E-Ink devices), and the runtime directories moved with it.

Existing installations keep their tokens and state under the legacy
directory.  :func:`resolve_config_dir` prefers the new directory but falls
back to the legacy one when only that exists, so upgrading the Python
package never locks a user out of their tokens.  ``scripts/install_host.sh``
migrates the legacy directory explicitly.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "hermes-eink-dashboard"
LEGACY_APP_NAME = "hermes-kindle-dashboard"

#: Name of the systemd --user unit that runs the host service.
SERVICE_NAME = "hermes-eink-dashboard"
LEGACY_SERVICE_NAME = "hermes-kindle-dashboard"


def config_home() -> Path:
    """Return the XDG config home directory."""
    override = os.getenv("XDG_CONFIG_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config"


def data_home() -> Path:
    """Return the XDG data home directory."""
    override = os.getenv("XDG_DATA_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share"


def app_config_dir() -> Path:
    """Return the current config directory (``~/.config/hermes-eink-dashboard``)."""
    return config_home() / APP_NAME


def legacy_app_config_dir() -> Path:
    """Return the pre-rename config directory."""
    return config_home() / LEGACY_APP_NAME


def resolve_config_dir() -> Path:
    """Return the config directory to read and write.

    The new directory wins whenever it exists.  Otherwise a pre-rename
    installation keeps working from the legacy directory until the host
    installer migrates it.
    """
    new_dir = app_config_dir()
    if new_dir.exists():
        return new_dir
    legacy_dir = legacy_app_config_dir()
    if legacy_dir.exists():
        return legacy_dir
    return new_dir


def app_data_dir() -> Path:
    """Return the current data directory (``~/.local/share/hermes-eink-dashboard``)."""
    return data_home() / APP_NAME


def legacy_app_data_dir() -> Path:
    """Return the pre-rename data directory."""
    return data_home() / LEGACY_APP_NAME
