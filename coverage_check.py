#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility CLI for the canonical incremental implementation.

The implementation lives under :mod:`app.incremental.legacy` while the
historical root import and command name remain available to existing callers.
"""

import sys

from app.incremental import legacy as _canonical


if __name__ == "__main__":
    _canonical.main()
else:
    # Preserve patching/introspection semantics for legacy callers: importing
    # ``coverage_check`` returns the canonical module object itself.
    sys.modules[__name__] = _canonical
