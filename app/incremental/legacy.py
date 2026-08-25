"""Lazy compatibility adapter for the historical incremental module."""

from __future__ import absolute_import

import importlib
import runpy
import sys
from types import ModuleType

from app.compat.telemetry import record as _record_legacy_usage


_LEGACY_MODULE = "app.compat.incremental_impl"
_legacy_module = None


def _legacy_impl():
    global _legacy_module
    if _legacy_module is None:
        _record_legacy_usage("app.compat.incremental_impl:lazy-import")
        _legacy_module = importlib.import_module(_LEGACY_MODULE)
    return _legacy_module


def get_legacy_attribute(name):
    """Read old incremental symbols through an explicit Python 3.6 API."""
    if str(name).startswith("__"):
        raise AttributeError(name)
    return _legacy_impl().get_legacy_attribute(name)


def set_legacy_attribute(name, value):
    """Write old symbols into their historical module globals."""
    return _legacy_impl().set_legacy_attribute(name, value)


def delete_legacy_attribute(name):
    """Delete old symbols for mock.patch cleanup semantics."""
    return _legacy_impl().delete_legacy_attribute(name)


def dispatch_cli(argv=None):
    # The historical module owns the complete previous-release CLI parser;
    # importing this adapter alone must not load that implementation.
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(item in ("-h", "--help") for item in arguments):
        print("usage: coverage_check.py --info INFO (--repo REPO --oldgit OLD --newgit NEW | --repos-config CONFIG)")
        print("       legacy incremental compatibility surface; VNext imports use app.incremental services")
        return
    if argv is not None:
        sys.argv = [sys.argv[0]] + arguments
    runpy.run_module(_LEGACY_MODULE, run_name="__main__")


class _IncrementalLegacyModule(ModuleType):
    """Python 3.6-compatible read/write proxy for old incremental symbols."""

    _OWNED_NAMES = frozenset((
        "dispatch_cli", "get_legacy_attribute", "set_legacy_attribute",
        "delete_legacy_attribute",
        "_LEGACY_MODULE", "_legacy_module", "_record_legacy_usage",
        "_IncrementalLegacyModule", "_module",
    ))

    def __setattr__(self, name, value):
        if name.startswith("__") or name in self._OWNED_NAMES:
            return ModuleType.__setattr__(self, name, value)
        return set_legacy_attribute(name, value)

    def __delattr__(self, name):
        if name.startswith("__") or name in self._OWNED_NAMES:
            return ModuleType.__delattr__(self, name)
        return delete_legacy_attribute(name)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return get_legacy_attribute(name)


_module = sys.modules.get(__name__)
if _module is not None:
    _module.__class__ = _IncrementalLegacyModule


if __name__ == "__main__":
    _record_legacy_usage("app.incremental.legacy:cli")
    dispatch_cli()
