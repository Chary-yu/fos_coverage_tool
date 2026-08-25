"""Thin adapter for the explicitly retained previous-release runtime.

Current VNext composition never imports this module's implementation. The
historical code lives in ``legacy_runtime_previous_release`` so retirement
audits can distinguish a rollback surface from the active runtime boundary.
"""

from __future__ import absolute_import

import importlib
import runpy
import sys

from app.compat.telemetry import record as _record_legacy_usage


_PREVIOUS_RELEASE_MODULE = "app.compat.legacy_runtime_previous_release"
_previous_release = None


def _implementation():
    global _previous_release
    if _previous_release is None:
        _record_legacy_usage(
            "app.compat.legacy_runtime_previous_release:lazy-import"
        )
        _previous_release = importlib.import_module(_PREVIOUS_RELEASE_MODULE)
    return _previous_release


def dispatch_cli(argv=None):
    if argv is not None:
        sys.argv = [sys.argv[0]] + list(argv)
    runpy.run_module(_PREVIOUS_RELEASE_MODULE, run_name="__main__")


def get_legacy_attribute(name):
    """Read an old symbol without relying on module-level PEP 562."""
    if str(name).startswith("__"):
        raise AttributeError(name)
    return getattr(_implementation(), str(name))


def set_legacy_attribute(name, value):
    """Write an old symbol where the historical functions resolve globals."""
    setattr(_implementation(), str(name), value)


def delete_legacy_attribute(name):
    """Delete an old symbol for mock.patch cleanup semantics."""
    delattr(_implementation(), str(name))


def __getattr__(name):
    # Retain the modern-runtime fallback for direct imports, but all
    # compatibility boundaries use the explicit helpers above so Python 3.6
    # never needs module-level __getattr__ support.
    return get_legacy_attribute(name)


if __name__ == "__main__":
    _record_legacy_usage("app.compat.legacy_runtime_impl:cli")
    dispatch_cli()
