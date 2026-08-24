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


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    return getattr(_implementation(), name)


if __name__ == "__main__":
    _record_legacy_usage("app.compat.incremental_impl:cli")
    dispatch_cli()
