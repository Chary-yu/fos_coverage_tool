"""Single, side-effect-free runtime configuration loader."""

import json
import os
from typing import Any, Dict, Optional


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
    result["runtime_state"] = state
    return result


def validate_production_config(config: Dict[str, Any]) -> None:
    runtime_mode = str(config.get("runtime_mode") or "legacy").lower()
    if runtime_mode not in ("legacy", "vnext"):
        raise RuntimeError("runtime_mode must be 'legacy' or 'vnext'")
    if runtime_mode == "vnext" and int(config.get("schema_version") or 0) < 1:
        raise RuntimeError("VNext runtime requires schema_version >= 1")
    auth = config.get("auth") or {}
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
