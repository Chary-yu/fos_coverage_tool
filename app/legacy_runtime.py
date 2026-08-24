"""Thin adapter for the historical ``app.legacy_runtime`` import name."""

from __future__ import absolute_import

from app.compat import legacy_runtime_adapter as _adapter
from app.compat.telemetry import record as _record_legacy_usage


SCRIPT_DIR = _adapter.SCRIPT_DIR
CONFIG_PATH = _adapter.CONFIG_PATH
DEFAULT_PROJECT_NAME = _adapter.DEFAULT_PROJECT_NAME
load_config = _adapter.load_config
get_arg_value = _adapter.get_arg_value
has_arg = _adapter.has_arg
run_server = _adapter.run_server
print_help = _adapter.print_help
dispatch_cli = _adapter.dispatch_cli


def __getattr__(name):
    return getattr(_adapter, name)


if __name__ == "__main__":
    _record_legacy_usage("app.legacy_runtime:cli")
    dispatch_cli()
