"""The pre-rename import path must keep working during the deprecation window."""

from __future__ import annotations

import importlib
import sys
import warnings

import pytest


def test_old_package_emits_deprecation_warning() -> None:
    for name in [n for n in list(sys.modules) if n.startswith("hermes_kindle_dashboard")]:
        del sys.modules[name]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.import_module("hermes_kindle_dashboard")
    assert any(issubclass(item.category, DeprecationWarning) for item in caught)
    assert any("hermes_eink_dashboard" in str(item.message) for item in caught)


def test_old_package_resolves_to_the_same_module_objects() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        old = importlib.import_module("hermes_kindle_dashboard.state")
        new = importlib.import_module("hermes_eink_dashboard.state")
    assert old is new
    assert old.__name__ == "hermes_eink_dashboard.state"


def test_old_submodule_import_works() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from hermes_kindle_dashboard.contract import PanelCache
        from hermes_eink_dashboard.contract import PanelCache as NewPanelCache
    assert PanelCache is NewPanelCache


def test_old_package_exposes_version() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import hermes_kindle_dashboard as legacy
        import hermes_eink_dashboard as current
    assert legacy.__version__ == current.__version__


def test_unknown_attribute_raises_attribute_error() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import hermes_kindle_dashboard as legacy
    with pytest.raises(AttributeError):
        legacy.does_not_exist
