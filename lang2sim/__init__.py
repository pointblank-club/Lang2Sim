"""Lang2Sim: Natural Language to Executable Robot Simulation."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from collections.abc import Sequence
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType

__version__ = "2.0.0"


class _Sim2LangLoader(Loader):
    """Loader that returns the already imported lang2sim module object."""

    def __init__(self, target_mod: ModuleType) -> None:
        self.target_mod = target_mod

    def create_module(self, spec: ModuleSpec) -> ModuleType:
        return self.target_mod

    def exec_module(self, module: ModuleType) -> None:
        pass


class _Sim2LangRedirectFinder(MetaPathFinder):
    """Transparently redirects legacy 'sim2lang.*' imports to 'lang2sim.*'."""

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if fullname == "sim2lang" or fullname.startswith("sim2lang."):
            target_name = "lang2sim" + fullname[len("sim2lang") :]
            try:
                target_mod = importlib.import_module(target_name)
                sys.modules[fullname] = target_mod
                spec = importlib.util.spec_from_loader(fullname, _Sim2LangLoader(target_mod))
                return spec
            except ModuleNotFoundError:
                return None
        return None


if not any(isinstance(f, _Sim2LangRedirectFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _Sim2LangRedirectFinder())

if "sim2lang" not in sys.modules:
    sys.modules["sim2lang"] = sys.modules[__name__]
