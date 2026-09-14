"""Normalize legacy vfoswind production config into an R8 attempt config.

Only deterministic, attempt-safe compatibility fields are materialized here.
Environment-specific release-gate expectations remain explicit operator input.
"""

from __future__ import print_function

import copy
import os
try:
    from urllib.parse import urlsplit, urlunsplit
except ImportError:  # pragma: no cover - Python 2 compatibility is not required
    from urlparse import urlsplit, urlunsplit


VFOSWIND_ADAPTER = "vfoswind"

_INTEGRATION_DEFAULTS = {
    "runtime_environment_file": "/etc/onesensor/coverage-runtime.env",
    "validation_systemd_unit": "onesensor-coverage-validation.service",
    "validation_systemd_unit_file": (
        "/etc/systemd/system/onesensor-coverage-validation.service"
    ),
    "validation_runtime_environment_file": "/etc/onesensor/coverage-validation.env",
    "validation_config_path": "/etc/onesensor/coverage-validation.json",
}

_SERVING_DEFAULTS = {
    "serving_session_id": "current-serving",
    "serving_session_manifest": "/home/zcyu/coverage_candidate/serving-session.json",
    "serving_teardown_evidence_path": "/home/zcyu/coverage_candidate/serving-teardown.json",
    "current_serving_state_path": "/home/zcyu/coverage_candidate/current-serving.json",
}

_DEFAULT_TRUSTED_PROXIES = ["127.0.0.1", "::1"]
_DEFAULT_USER_HEADER = "X-Remote-User"
_DEFAULT_BROWSER_DOCUMENT = "coverage_progress.html"


def _missing(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _normalize_browser_url(value, static_location):
    value = str(value or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return value
    if parsed.path not in ("", "/"):
        return value
    prefix = str(static_location or "").strip()
    if not prefix:
        return value
    path = "/" + prefix.strip("/") + "/" + _DEFAULT_BROWSER_DOCUMENT
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def normalize_vfoswind_attempt_config(config, candidate_application_root=""):
    """Return a deep-copied R8 attempt config for the vfoswind adapter.

    Known legacy ``auth.mode=disabled`` means the application previously
    trusted the local gateway.  The R8 production contract makes that trust
    explicit as ``reverse_proxy`` and restricts it to loopback proxies.
    Unknown non-empty auth modes fail closed.
    """
    result = copy.deepcopy(config or {})
    upgrade = dict(result.get("upgrade") or {})
    integration = dict(upgrade.get("production_integration") or {})

    lifecycle_adapter = str(upgrade.get("lifecycle_adapter") or "").strip()
    integration_adapter = str(integration.get("adapter") or "").strip()
    if lifecycle_adapter != VFOSWIND_ADAPTER and \
            integration_adapter != VFOSWIND_ADAPTER:
        return result
    if lifecycle_adapter and lifecycle_adapter != VFOSWIND_ADAPTER:
        raise RuntimeError("vfoswind attempt lifecycle adapter is inconsistent")
    if integration_adapter and integration_adapter != VFOSWIND_ADAPTER:
        raise RuntimeError("vfoswind production integration adapter is inconsistent")

    upgrade["lifecycle_adapter"] = VFOSWIND_ADAPTER
    integration["adapter"] = VFOSWIND_ADAPTER
    for key, value in _INTEGRATION_DEFAULTS.items():
        if _missing(integration.get(key)):
            integration[key] = value
    if candidate_application_root:
        integration["validation_application_root"] = os.path.realpath(
            os.path.abspath(candidate_application_root)
        )

    for key, value in _SERVING_DEFAULTS.items():
        if _missing(upgrade.get(key)):
            upgrade[key] = value

    auth = dict(result.get("auth") or {})
    mode = str(auth.get("mode") or "").strip().lower()
    if mode in ("", "disabled"):
        auth["mode"] = "reverse_proxy"
    elif mode != "reverse_proxy":
        raise RuntimeError(
            "vfoswind attempt auth.mode must be reverse_proxy-compatible"
        )
    else:
        auth["mode"] = "reverse_proxy"

    auth_bridge = dict(integration.get("auth_bridge") or {})
    expected_header = str(
        auth_bridge.get("user_header") or _DEFAULT_USER_HEADER
    ).strip()
    observed_header = str(auth.get("user_header") or expected_header).strip()
    if observed_header != expected_header:
        raise RuntimeError(
            "vfoswind attempt auth.user_header does not match auth bridge"
        )
    auth["user_header"] = expected_header

    trusted = auth.get("trusted_proxy_addresses")
    if not isinstance(trusted, list) or not any(str(item).strip() for item in trusted):
        auth["trusted_proxy_addresses"] = list(_DEFAULT_TRUSTED_PROXIES)
    if not isinstance(auth.get("allowed_origins"), list):
        auth["allowed_origins"] = []

    gateway = dict(integration.get("candidate_gateway") or {})
    static_location = gateway.get("static_location") or "/coverage/"
    candidate_browser_url = _normalize_browser_url(
        upgrade.get("candidate_browser_url"), static_location
    )
    gateway_browser_url = _normalize_browser_url(
        gateway.get("browser_url"), static_location
    )
    if candidate_browser_url:
        upgrade["candidate_browser_url"] = candidate_browser_url
    if gateway_browser_url:
        gateway["browser_url"] = gateway_browser_url
    if candidate_browser_url and gateway_browser_url and \
            candidate_browser_url != gateway_browser_url:
        raise RuntimeError(
            "vfoswind Candidate browser URL does not match gateway browser URL"
        )
    if gateway:
        integration["candidate_gateway"] = gateway

    upgrade["production_integration"] = integration
    result["auth"] = auth
    result["upgrade"] = upgrade
    return result
