"""Deprecated import alias for :mod:`hermes_eink_dashboard`.

The Python package was renamed from ``hermes_kindle_dashboard`` to
``hermes_eink_dashboard`` when the ``hermes-kindle-dashboard`` and
``hermes-eink-dashboard`` repositories were consolidated (the project became a
multi-device E-Ink gateway with Kindle as one adapter). This module keeps the
old import path working -- ``import hermes_kindle_dashboard`` and
``from hermes_kindle_dashboard.<mod> import ...`` both resolve to the same
objects as the new package -- while emitting a :class:`DeprecationWarning`.

Update imports to ``hermes_eink_dashboard``; this alias will be removed in a
future release.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import warnings

_NEW_NAME = "hermes_eink_dashboard"

warnings.warn(
    "'hermes_kindle_dashboard' has been renamed to 'hermes_eink_dashboard'; "
    "update your imports. The old name is a temporary alias and will be "
    "removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)

_new_pkg = importlib.import_module(_NEW_NAME)

# Register the new package (and every submodule) under the old name so that
# both dotted paths return the *same* module object -- no duplicate instances,
# so ``isinstance`` checks and module-level singletons behave identically
# regardless of which import path a caller used.
sys.modules[__name__] = _new_pkg
for _module_info in pkgutil.walk_packages(_new_pkg.__path__, _new_pkg.__name__ + "."):
    _new_full = _module_info.name
    _old_full = __name__ + _new_full[len(_NEW_NAME):]
    sys.modules[_old_full] = importlib.import_module(_new_full)
