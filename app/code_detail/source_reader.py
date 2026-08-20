"""Compatibility import surface for the canonical Code Detail package.

The extraction of the historical parser is deliberately isolated here first;
runtime callers can depend on app.code_detail without importing a root module.
The root module remains a compatibility source until the final Gate 3
deactivation commit moves its implementation verbatim.
"""

from source_reader import *  # noqa: F401,F403
