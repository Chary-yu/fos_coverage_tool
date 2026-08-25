"""Thin compatibility boundary for the historical runtime entrypoint.

The previous-release implementation is deliberately loaded only when an
explicit legacy command or an old-only symbol is requested. Importing the
compatibility name must not construct the historical server/database runtime;
all VNext composition is delegated to :mod:`app.bootstrap`.
"""

from __future__ import absolute_import

import importlib
import os
import runpy
import sys

from app.compat.telemetry import record as _record_legacy_usage
from app.config.runtime_config import load_application_config


SCRIPT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "coverage_config.json")
DEFAULT_PROJECT_NAME = "Gemini-NOS"

_LEGACY_MODULE = "app.compat.legacy_runtime_impl"
_legacy_module = None


def _legacy_impl():
    global _legacy_module
    if _legacy_module is None:
        _record_legacy_usage("app.compat.legacy_runtime_impl:lazy-import")
        _legacy_module = importlib.import_module(_LEGACY_MODULE)
    return _legacy_module


def load_config(config_path=None):
    """Load the canonical config contract for compatibility callers."""
    explicit_path = config_path or os.environ.get("COVERAGE_CONFIG_PATH")
    if explicit_path:
        return load_application_config(
            explicit_path, base_dir=SCRIPT_DIR,
            project_name=DEFAULT_PROJECT_NAME,
        )
    try:
        return load_application_config(
            CONFIG_PATH, base_dir=SCRIPT_DIR,
            project_name=DEFAULT_PROJECT_NAME,
        )
    except Exception as error:
        print("[Warning] Failed to load config file: {}. Using defaults.".format(error))
        return load_application_config(
            None, base_dir=SCRIPT_DIR,
            project_name=DEFAULT_PROJECT_NAME,
        )


def get_arg_value(args, name):
    for index, argument in enumerate(args or ()):
        if argument == name and index + 1 < len(args):
            return args[index + 1]
    return None


def has_arg(args, name):
    return name in (args or ())


def get_legacy_attribute(name):
    """Read a previous-release symbol through an explicit Python 3.6 API."""
    if str(name).startswith("__"):
        raise AttributeError(name)
    implementation = _legacy_impl()
    getter = getattr(implementation, "get_legacy_attribute", None)
    if callable(getter):
        return getter(name)
    return getattr(implementation, str(name))


def set_legacy_attribute(name, value):
    """Preserve assignment semantics for the historical module surface."""
    implementation = _legacy_impl()
    setter = getattr(implementation, "set_legacy_attribute", None)
    if callable(setter):
        return setter(name, value)
    target_factory = getattr(implementation, "_implementation", None)
    target = target_factory() if callable(target_factory) else implementation
    setattr(target, str(name), value)


def delete_legacy_attribute(name):
    """Delete a previous-release symbol for mock.patch cleanup semantics."""
    implementation = _legacy_impl()
    deleter = getattr(implementation, "delete_legacy_attribute", None)
    if callable(deleter):
        return deleter(name)
    target_factory = getattr(implementation, "_implementation", None)
    target = target_factory() if callable(target_factory) else implementation
    delattr(target, str(name))


def _run_vnext_server(config):
    from app.bootstrap import create_vnext_server

    host = config["server"]["host"]
    port = int(config["server"]["port"])
    httpd = create_vnext_server((host, port), config, repo_root=SCRIPT_DIR)
    print("[Server] VNext runtime running on http://{}:{} ...".format(host, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[Server] Shutting down VNext runtime...")
        runtime = getattr(httpd, "vnext_runtime", None)
        if runtime:
            runtime.close()
        httpd.server_close()
        print("[Server] VNext runtime stopped.")


def run_server(config_path=None):
    """Delegate VNext server startup to the canonical composition root."""
    config = load_config(config_path)
    if str(config.get("runtime_mode") or "vnext").lower() == "vnext":
        return _run_vnext_server(config)
    return _legacy_impl().run_server(config_path)


def print_help():
    print("Usage:")
    print("  python enhance_coverage.py server [--config <config.json>]")
    print("    - Start the canonical VNext runtime for runtime_mode=vnext.")
    print("  python enhance_coverage.py inject --project <project_name> --dir <input_dir> --out <output_dir>")
    print("    - Use the previous-release compatibility injector explicitly.")
    print("  python enhance_coverage.py incremental --project <project_name> --repo <git_repo> --oldgit <old_commit> --newgit <new_commit> --info <coverage.info> --dir <lcov_html_dir> --out <output_dir>")
    print("    - Use the previous-release compatibility incremental surface.")
    print("  python enhance_coverage.py inherit --from <old_project> --to <new_project>")
    print("    - RETIRED: automatic inheritance is only available through VNext Scan Import.")


def dispatch_cli(argv=None):
    """Run only the requested compatibility command."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in ("--help", "-h", "help"):
        print_help()
        raise SystemExit(0 if arguments else 1)
    command = arguments[0]
    if command == "server":
        config_path = get_arg_value(arguments[1:], "--config")
        if "--config" in arguments[1:] and not config_path:
            print("[Error] server --config requires a path.")
            raise SystemExit(1)
        run_server(config_path)
        return
    if command == "inherit":
        print("[Error] legacy inherit is retired; use VNext Scan Import and its fixed predecessor.")
        raise SystemExit(2)
    # Preserve the exact old command parser for explicitly retained
    # previous-release surfaces without loading it for ordinary imports.
    runpy.run_module(_LEGACY_MODULE, run_name="__main__")


def __getattr__(name):
    # Kept for Python 3.7+ direct imports; compatibility callers use the
    # explicit get_legacy_attribute helper through a ModuleType subclass.
    return get_legacy_attribute(name)


__all__ = ("SCRIPT_DIR", "CONFIG_PATH", "DEFAULT_PROJECT_NAME",
           "load_config", "get_arg_value", "has_arg", "run_server",
           "print_help", "dispatch_cli")
