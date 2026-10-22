"""Deprecated alias for :mod:`hermes_eink_dashboard`.

The project was renamed from ``hermes-kindle-dashboard`` to
``hermes-eink-dashboard`` when the Kindle client became one adapter among
several possible E-Ink devices.  The old import path keeps working: every
submodule imported through this package resolves to the *same module object*
as its ``hermes_eink_dashboard`` counterpart, so ``isinstance`` checks and
module-level state stay consistent no matter which name was imported.

New code should import :mod:`hermes_eink_dashboard` directly.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
import warnings
from types import ModuleType

_NEW_PACKAGE = "hermes_eink_dashboard"
_OLD_PACKAGE = "hermes_kindle_dashboard"

__all__ = ["__version__"]

warnings.warn(
    f"{_OLD_PACKAGE} is deprecated; import {_NEW_PACKAGE} instead.",
    DeprecationWarning,
    stacklevel=2,
)


class _AliasLoader(importlib.abc.Loader):
    """Loader that returns the new module object under the old name."""

    def __init__(self) -> None:
        self._original_spec: importlib.machinery.ModuleSpec | None = None

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType:
        target = _NEW_PACKAGE + spec.name[len(_OLD_PACKAGE) :]
        module = importlib.import_module(target)
        self._original_spec = module.__spec__
        return module

    def exec_module(self, module: ModuleType) -> None:
        # ``importlib`` overwrites ``__spec__`` while wiring the alias up;
        # restore the real one so the shared module object stays untouched.
        if self._original_spec is not None:
            module.__spec__ = self._original_spec


class _AliasFinder(importlib.abc.MetaPathFinder):
    """Resolve ``hermes_kindle_dashboard.*`` to ``hermes_eink_dashboard.*``."""

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: object = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if not fullname.startswith(_OLD_PACKAGE + "."):
            return None
        real_name = _NEW_PACKAGE + fullname[len(_OLD_PACKAGE) :]
        try:
            real_spec = importlib.util.find_spec(real_name)
        except (ImportError, AttributeError, ValueError):
            return None
        if real_spec is None:
            return None
        is_package = real_spec.submodule_search_locations is not None
        return importlib.util.spec_from_loader(
            fullname, _AliasLoader(), is_package=is_package
        )


if not any(isinstance(finder, _AliasFinder) for finder in sys.meta_path):
    sys.meta_path.append(_AliasFinder())

_new = importlib.import_module(_NEW_PACKAGE)
__version__ = getattr(_new, "__version__", "0.0.0")


def __getattr__(name: str) -> ModuleType:
    """Expose submodules of the new package as attributes of this one."""
    if name.startswith("__"):
        raise AttributeError(name)
    try:
        module = importlib.import_module(f"{_NEW_PACKAGE}.{name}")
    except ImportError as error:
        raise AttributeError(
            f"module {_OLD_PACKAGE!r} has no attribute {name!r}"
        ) from error
    globals()[name] = module
    return module
