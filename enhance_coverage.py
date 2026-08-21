#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility entrypoint for the canonical application runtime.

The historical import and CLI name are retained for existing integrations;
the compatibility implementation is housed under ``app.compat`` and
the VNext server path is assembled by ``app.bootstrap``.
"""

import runpy
import sys


if __name__ == "__main__":
    runpy.run_module("app.legacy_runtime", run_name="__main__")
else:
    from app import legacy_runtime as _canonical

    # Return the canonical module object so legacy monkey-patching and imports
    # keep the same semantics while root remains a thin compatibility shim.
    sys.modules[__name__] = _canonical
