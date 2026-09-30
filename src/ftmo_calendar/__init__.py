"""Deprecated import name — the package is `prop_firm_calendar` since 0.9.0.

`import ftmo_calendar.cli` (or any other submodule) keeps working and yields the
very same module object as `prop_firm_calendar.cli`, not a copy, so
monkeypatching, isinstance checks and module-level state behave identically
through either name. The `ftmo-calendar` console script is likewise kept as an
alias of `prop-firm-calendar`.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import sys
import warnings
from collections.abc import Sequence
from types import ModuleType

from prop_firm_calendar import __version__

__all__ = ["__version__"]

_OLD = __name__
_NEW = "prop_firm_calendar"

warnings.warn(
    f"the '{_OLD}' package was renamed to '{_NEW}'; import that instead",
    DeprecationWarning,
    stacklevel=2,
)


class _AliasLoader(importlib.abc.Loader):
    """Hands back the already-importable real module instead of a new one."""

    def __init__(self, target: str) -> None:
        self._target = target

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType:
        module = importlib.import_module(self._target)
        self._real_spec = module.__spec__
        return module

    def exec_module(self, module: ModuleType) -> None:
        # The import system has just stamped the alias spec onto the real
        # module; put its own back so reloads and pickling still see the
        # real name.
        module.__spec__ = self._real_spec


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if not fullname.startswith(_OLD + "."):
            return None
        return importlib.machinery.ModuleSpec(fullname, _AliasLoader(_NEW + fullname[len(_OLD) :]))


if not any(isinstance(finder, _AliasFinder) for finder in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())
