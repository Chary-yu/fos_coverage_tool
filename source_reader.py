"""Compatibility alias for the canonical source-reader module."""

import sys

from app.code_detail import source_reader as _canonical


sys.modules[__name__] = _canonical
