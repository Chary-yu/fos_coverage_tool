"""Compatibility adapter for the retired root runtime name.

The VNext runtime is assembled by app.bootstrap. This module keeps the
historical app.legacy_runtime import/CLI surface, but owns no server,
database, HTML, progress, or incremental business logic.
"""

import runpy
import sys

from app.compat.telemetry import record as _record_legacy_usage


if __name__ == "__main__":
    _record_legacy_usage("app.legacy_runtime:cli")
    runpy.run_module("app.compat.legacy_runtime_impl", run_name="__main__")
else:
    _record_legacy_usage("app.legacy_runtime:import")
    from app.compat import legacy_runtime_impl as _canonical

    # Preserve legacy monkey-patching and introspection semantics.
    sys.modules[__name__] = _canonical
