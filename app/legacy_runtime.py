"""Thin adapter for the historical ``app.legacy_runtime`` import name."""

from __future__ import absolute_import

import sys
from types import ModuleType

from app.compat import legacy_runtime_adapter as _adapter
from app.compat.telemetry import record as _record_legacy_usage


SCRIPT_DIR = _adapter.SCRIPT_DIR
CONFIG_PATH = _adapter.CONFIG_PATH
DEFAULT_PROJECT_NAME = _adapter.DEFAULT_PROJECT_NAME
load_config = _adapter.load_config
get_arg_value = _adapter.get_arg_value
has_arg = _adapter.has_arg
run_server = _adapter.run_server
print_help = _adapter.print_help
dispatch_cli = _adapter.dispatch_cli


class _LegacyRuntimeModule(ModuleType):
    """Module proxy retaining old monkey-patch assignment behavior."""

    _OWNED_NAMES = frozenset((
        "SCRIPT_DIR", "CONFIG_PATH", "DEFAULT_PROJECT_NAME", "load_config",
        "get_arg_value", "has_arg", "run_server", "print_help", "dispatch_cli",
    ))

    def __setattr__(self, name, value):
        if name.startswith("_") or name in self._OWNED_NAMES:
            return ModuleType.__setattr__(self, name, value)
        return _adapter.set_legacy_attribute(name, value)

    def __getattr__(self, name):
        """Resolve old-only symbols without relying on PEP 562.

        ``app.legacy_runtime`` is also imported by the Python 3.6 support
        lane.  Module-level ``__getattr__`` was introduced in Python 3.7;
        putting the read proxy on the ``ModuleType`` subclass keeps lazy
        compatibility reads working on both runtimes.
        """
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(_adapter, name)


_module = sys.modules.get(__name__)
if _module is not None:
    _module.__class__ = _LegacyRuntimeModule


if __name__ == "__main__":
    _record_legacy_usage("app.legacy_runtime:cli")
    dispatch_cli()
