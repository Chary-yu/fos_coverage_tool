"""Single, side-effect-free runtime configuration loader."""

import json
import os
from typing import Any, Dict, Optional


def load_runtime_config(path: str, defaults: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result = dict(defaults or {})
    if not os.path.isfile(path):
        return result
    with open(path, "r", encoding="utf-8-sig") as stream:
        loaded = json.load(stream)
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            merged = dict(result[key])
            merged.update(value)
            result[key] = merged
        else:
            result[key] = value
    return result


def validate_production_config(config: Dict[str, Any]) -> None:
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
