"""Compatibility adapter for the historical incremental module."""

import runpy
import sys


if __name__ == "__main__":
    runpy.run_module("app.compat.incremental_impl", run_name="__main__")
else:
    from app.compat import incremental_impl as _canonical

    sys.modules[__name__] = _canonical
