"""Compatibility adapter for the historical incremental module."""

import runpy
import sys

from app.compat.telemetry import record as _record_legacy_usage


if __name__ == "__main__":
    _record_legacy_usage("app.incremental.legacy:cli")
    runpy.run_module("app.compat.incremental_impl", run_name="__main__")
else:
    _record_legacy_usage("app.incremental.legacy:import")
    from app.compat import incremental_impl as _canonical

    sys.modules[__name__] = _canonical
