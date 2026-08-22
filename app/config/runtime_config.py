"""Single, side-effect-free runtime configuration loader."""

import json
import os
import warnings
from typing import Any, Dict, Optional


CONFIG_SCHEMA_VERSION = 2


def _merge(base, overlay):
    result = dict(base or {})
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_runtime_config(path: str, defaults: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result = dict(defaults or {})
    if not os.path.isfile(path):
        return result
    with open(path, "r", encoding="utf-8-sig") as stream:
        loaded = json.load(stream)
    return _merge(result, loaded)


def default_runtime_config(base_dir: str, project_name: str = "Gemini-NOS") -> Dict[str, Any]:
    """Return the repository-relative defaults shared by CLI and services.

    Keeping defaults here prevents the historical root entrypoint from
    maintaining a second configuration contract.  Callers still decide which
    environment-specific file to load and may override any value in it.
    """
    base_dir = os.path.realpath(base_dir)
    return {
        "mysql": {
            "host": "localhost",
            "port": 3306,
            "user": "root",
            "password": "",
            "database": "coverage",
        },
        # A no-config start must be loopback-only. Deployments that need a
        # public bind must make that choice explicit in an environment config
        # and put the service behind its operator/read-auth policy.
        "server": {"host": "127.0.0.1", "port": 9528},
        "auth": {
            "mode": "reverse_proxy",
            "user_header": "X-Remote-User",
            "trusted_proxy_addresses": ["127.0.0.1", "::1"],
            "allowed_origins": [],
        },
        "ownership": {
            "enabled": True,
            "xlsx_path": os.path.join(base_dir, "代码目录归属模块统计.xlsx"),
        },
        "runtime_state": {
            "root": os.path.join(base_dir, ".runtime-state"),
            "jobs_dir": "jobs",
            "registry_dir": "report-registry",
        },
        # VNext is the canonical runtime.  Legacy remains available only
        # through an explicit compatibility configuration/entrypoint.
        "runtime_mode": "vnext",
        "schema_version": 1,
        "config_schema_version": CONFIG_SCHEMA_VERSION,
        "project_name": project_name,
    }


def load_application_config(path: Optional[str] = None, base_dir: Optional[str] = None,
                            project_name: str = "Gemini-NOS") -> Dict[str, Any]:
    """Load, normalize, and validate one application configuration."""
    base_dir = os.path.realpath(base_dir or os.getcwd())
    configured_path = path or os.environ.get("COVERAGE_CONFIG_PATH")
    if configured_path and not os.path.isabs(configured_path):
        configured_path = os.path.join(base_dir, configured_path)
    if configured_path and not os.path.isfile(configured_path):
        raise FileNotFoundError(configured_path)
    defaults = default_runtime_config(base_dir, project_name=project_name)
    raw_loaded = {}
    if configured_path:
        with open(configured_path, "r", encoding="utf-8-sig") as stream:
            raw_loaded = json.load(stream)
        if not isinstance(raw_loaded, dict):
            raise ValueError("runtime config root must be an object")
    result = _merge(defaults, raw_loaded) if configured_path else defaults
    result = _apply_compatibility_aliases(result)
    result["_config_metadata"] = {
        "path": configured_path or "",
        "source_config_schema_version": int(
            raw_loaded.get("config_schema_version") or 0
        ) if configured_path else CONFIG_SCHEMA_VERSION,
        "effective_config_schema_version": int(
            result.get("config_schema_version") or 0
        ),
    }
    result = normalize_candidate_paths(result, base_dir)
    validate_production_config(
        result, source_schema_version=(
            int(raw_loaded.get("config_schema_version") or 0)
            if configured_path else CONFIG_SCHEMA_VERSION
        )
    )
    return result


def resolve_runtime_path(config: Dict[str, Any], key: str, base_dir: str,
                         default: Optional[str] = None) -> str:
    """Resolve configured paths relative to the config/repository root."""
    value = config.get(key, default)
    if not value:
        return ""
    value = os.path.expandvars(os.path.expanduser(str(value)))
    if not os.path.isabs(value):
        value = os.path.join(base_dir, value)
    return os.path.realpath(value)


def normalize_candidate_paths(config: Dict[str, Any], base_dir: str) -> Dict[str, Any]:
    """Return a copy with runtime roots made explicit and auditable."""
    result = _merge({}, config)
    state = result.setdefault("runtime_state", {})
    state["root"] = resolve_runtime_path(
        state, "root", base_dir, os.path.join(base_dir, ".runtime-state")
    )
    state["jobs_dir"] = resolve_runtime_path(
        state, "jobs_dir", state["root"], "jobs"
    )
    state["registry_dir"] = resolve_runtime_path(
        state, "registry_dir", state["root"], "report-registry"
    )
    if "exports_dir" in state:
        state["exports_dir"] = resolve_runtime_path(
            state, "exports_dir", state["root"], "exports"
        )
    result["runtime_state"] = state
    result["input_roots"] = [
        resolve_runtime_path({"root": item}, "root", base_dir, base_dir)
        for item in (result.get("input_roots") or [])
    ]
    result["report_roots"] = [
        resolve_runtime_path({"root": item}, "root", base_dir, base_dir)
        for item in (result.get("report_roots") or [])
    ]
    return result


def _apply_compatibility_aliases(config: Dict[str, Any]) -> Dict[str, Any]:
    result = _merge({}, config)
    aliases = (
        (("database", "host"), ("mysql", "host")),
        (("database", "port"), ("mysql", "port")),
        (("database", "user"), ("mysql", "user")),
        (("database", "password"), ("mysql", "password")),
        (("database", "name"), ("mysql", "database")),
        (("server", "bind"), ("server", "host")),
        (("server", "bind_address"), ("server", "host")),
    )
    warnings_out = []
    for source_path, target_path in aliases:
        source = result
        target = result
        for key in source_path[:-1]:
            source = source.get(key) if isinstance(source, dict) else None
        for key in target_path[:-1]:
            target = target.setdefault(key, {})
        if isinstance(source, dict) and source_path[-1] in source and \
                target_path[-1] not in target:
            target[target_path[-1]] = source[source_path[-1]]
            warnings_out.append(
                "{} -> {}".format(".".join(source_path), ".".join(target_path))
            )
    if warnings_out:
        result["_config_metadata"] = dict(result.get("_config_metadata") or {})
        result["_config_metadata"]["compatibility_aliases"] = warnings_out
        for item in warnings_out:
            warnings.warn("deprecated runtime config alias: {}".format(item),
                          DeprecationWarning, stacklevel=2)
    return result


def validate_production_config(config: Dict[str, Any],
                               source_schema_version: Optional[int] = None) -> None:
    effective_source_schema_version = (
        config.get("config_schema_version")
        if source_schema_version is None else source_schema_version
    )
    if int(effective_source_schema_version or 0) < CONFIG_SCHEMA_VERSION:
        if str(os.environ.get("COVERAGE_ENV", "development")).lower() == "production":
            raise RuntimeError(
                "production runtime config requires config_schema_version >= {}".format(
                    CONFIG_SCHEMA_VERSION
                )
            )
    runtime_mode = str(config.get("runtime_mode") or "legacy").lower()
    if runtime_mode not in ("legacy", "vnext"):
        raise RuntimeError("runtime_mode must be 'legacy' or 'vnext'")
    if runtime_mode == "vnext" and int(config.get("schema_version") or 0) < 1:
        raise RuntimeError("VNext runtime requires schema_version >= 1")
    auth = config.get("auth") or {}
    host = str((config.get("server") or {}).get("host") or
               "127.0.0.1").strip().lower()
    loopback_hosts = {"127.0.0.1", "localhost", "::1", "[::1]"}
    if host not in loopback_hosts and str(auth.get("mode") or "").lower() == "disabled":
        raise RuntimeError("authentication cannot be disabled on a public bind")
    if str(os.environ.get("COVERAGE_ENV", "development")).lower() == "production":
        if auth.get("mode") == "disabled":
            raise RuntimeError("production authentication cannot be disabled")
        if not auth.get("trusted_proxy_addresses"):
            raise RuntimeError("production trusted proxy addresses are required")
        lifecycle = config.get("upgrade") or {}
        commands = lifecycle.get("commands") or {}
        if not commands:
            raise RuntimeError("production upgrade lifecycle commands are required")
        for command_name in (
            "freeze_traffic", "drain_jobs", "stop_api", "start_api",
            "start_previous_api", "open_traffic",
        ):
            if not commands.get(command_name):
                raise RuntimeError("production upgrade lifecycle command '{}' is required".format(command_name))
        if not lifecycle.get("release_endpoint") or not lifecycle.get("previous_release_endpoint"):
            raise RuntimeError("production release and previous-release endpoints are required")
        if not lifecycle.get("previous_release"):
            raise RuntimeError("production previous release identity is required")
