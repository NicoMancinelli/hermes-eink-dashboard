"""The deprecated ``hermes_kindle_dashboard`` import alias must keep working.

After the eink/kindle consolidation the package was renamed to
``hermes_eink_dashboard``. A compatibility shim preserves the old import path
(with a :class:`DeprecationWarning`) so existing installs and integrations do
not break. These tests lock in that contract.
"""
from __future__ import annotations

import importlib
import warnings


def test_old_package_alias_is_the_new_package() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        old = importlib.import_module("hermes_kindle_dashboard")
        new = importlib.import_module("hermes_eink_dashboard")
    assert old is new


def test_old_submodule_alias_is_the_new_submodule() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        old_server = importlib.import_module("hermes_kindle_dashboard.server")
        new_server = importlib.import_module("hermes_eink_dashboard.server")
    # Same module object -> same functions, no duplicate singletons.
    assert old_server is new_server
    assert old_server.main is new_server.main


def test_importing_old_name_warns() -> None:
    # Force a fresh import so the shim's module-level warning fires again.
    import sys

    for name in list(sys.modules):
        if name == "hermes_kindle_dashboard" or name.startswith(
            "hermes_kindle_dashboard."
        ):
            del sys.modules[name]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.import_module("hermes_kindle_dashboard")
    assert any(
        issubclass(w.category, DeprecationWarning)
        and "hermes_eink_dashboard" in str(w.message)
        for w in caught
    )
