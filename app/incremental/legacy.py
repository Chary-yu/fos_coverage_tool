"""Lazy compatibility adapter for the historical incremental module."""

from __future__ import absolute_import

import importlib
import runpy
import sys

from app.compat.telemetry import record as _record_legacy_usage


_LEGACY_MODULE = "app.compat.incremental_impl"
_legacy_module = None


def _legacy_impl():
    global _legacy_module
    if _legacy_module is None:
        _record_legacy_usage("app.compat.incremental_impl:lazy-import")
        _legacy_module = importlib.import_module(_LEGACY_MODULE)
    return _legacy_module


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


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    return getattr(_legacy_impl(), name)


if __name__ == "__main__":
    _record_legacy_usage("app.incremental.legacy:cli")
    dispatch_cli()
