"""Thin adapter for the explicitly retained previous-release incremental CLI."""

from __future__ import absolute_import

import importlib
import runpy
import sys

from app.compat.telemetry import record as _record_legacy_usage


_PREVIOUS_RELEASE_MODULE = "app.compat.incremental_previous_release"
_previous_release = None


def _implementation():
    global _previous_release
    if _previous_release is None:
        _record_legacy_usage(
            "app.compat.incremental_previous_release:lazy-import"
        )
        _previous_release = importlib.import_module(_PREVIOUS_RELEASE_MODULE)
    return _previous_release


def dispatch_cli(argv=None):
    if argv is not None:
        sys.argv = [sys.argv[0]] + list(argv)
    runpy.run_module(_PREVIOUS_RELEASE_MODULE, run_name="__main__")


def get_legacy_attribute(name):
    """Read a previous-release symbol without relying on module PEP 562."""
    if str(name).startswith("__"):
        raise AttributeError(name)
    return getattr(_implementation(), str(name))


def set_legacy_attribute(name, value):
    """Write a symbol in the module that owns historical function globals."""
    setattr(_implementation(), str(name), value)


def delete_legacy_attribute(name):
    """Delete a symbol for mock.patch cleanup semantics."""
    delattr(_implementation(), str(name))


def __getattr__(name):
    # Retain the modern-runtime fallback for direct imports; the public
    # compatibility boundary calls the explicit helpers above.
    return get_legacy_attribute(name)


if __name__ == "__main__":
    _record_legacy_usage("app.compat.incremental_impl:cli")
    dispatch_cli()
