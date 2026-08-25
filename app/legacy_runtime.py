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


_FACADE_NAMES = frozenset((
    "SCRIPT_DIR", "CONFIG_PATH", "DEFAULT_PROJECT_NAME", "load_config",
    "get_arg_value", "has_arg", "run_server", "print_help", "dispatch_cli",
))
_facade_patch_state = {}


class _LegacyRuntimeModule(ModuleType):
    """Module proxy retaining old monkey-patch assignment behavior."""

    _OWNED_NAMES = frozenset((
        "SCRIPT_DIR", "CONFIG_PATH", "DEFAULT_PROJECT_NAME", "load_config",
        "get_arg_value", "has_arg", "run_server", "print_help", "dispatch_cli",
        "_adapter", "_record_legacy_usage", "_LegacyRuntimeModule", "_module",
    ))

    def __setattr__(self, name, value):
        if name in _FACADE_NAMES:
            state = _facade_patch_state.get(name)
            if state is None:
                state = (
                    ModuleType.__getattribute__(self, name),
                    _adapter.get_legacy_attribute(name),
                )
                _facade_patch_state[name] = state
            elif value is state[0]:
                # unittest.mock.patch restores the wrapper's original facade
                # value. Restore the historical global captured at patch
                # entry as well, rather than leaking the adapter function
                # into the previous-release module.
                _adapter.set_legacy_attribute(name, state[1])
                del _facade_patch_state[name]
                return ModuleType.__setattr__(self, name, value)
            _adapter.set_legacy_attribute(name, value)
            return ModuleType.__setattr__(self, name, value)
        if name.startswith("__") or name in self._OWNED_NAMES:
            return ModuleType.__setattr__(self, name, value)
        return _adapter.set_legacy_attribute(name, value)

    def __delattr__(self, name):
        if name in _FACADE_NAMES:
            state = _facade_patch_state.pop(name, None)
            if state is not None:
                _adapter.set_legacy_attribute(name, state[1])
            return ModuleType.__delattr__(self, name)
        if name.startswith("__") or name in self._OWNED_NAMES:
            return ModuleType.__delattr__(self, name)
        return _adapter.delete_legacy_attribute(name)

    def __getattr__(self, name):
        """Resolve old-only symbols without relying on PEP 562.

        ``app.legacy_runtime`` is also imported by the Python 3.6 support
        lane.  Module-level ``__getattr__`` was introduced in Python 3.7;
        putting the read proxy on the ``ModuleType`` subclass keeps lazy
        compatibility reads working on both runtimes.
        """
        if name.startswith("__"):
            raise AttributeError(name)
        return _adapter.get_legacy_attribute(name)


_module = sys.modules.get(__name__)
if _module is not None:
    _module.__class__ = _LegacyRuntimeModule


if __name__ == "__main__":
    _record_legacy_usage("app.legacy_runtime:cli")
    dispatch_cli()
