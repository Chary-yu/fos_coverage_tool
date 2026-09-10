"""Manifest-derived Candidate Gateway contract for the air-gapped release path.

The canonical non-air-gapped upgrade controller historically expects one
preconfigured concrete Candidate HTML URL.  That is not a sound contract for
an immutable publication whose exact report path is selected from the release
manifest at runtime.  This module installs a narrowly-scoped adapter used only
by the air-gapped production entrypoint:

* configuration pins only the external Candidate Gateway origin;
* early preflight validates the gateway Nginx alias/API/auth boundary without
  pretending a report filename is known;
* browser/auth evidence must carry the real manifest-derived HTML URL;
* that real URL must remain on the configured Candidate Gateway origin; and
* the original canonical validators still perform all identity, artifact,
  Chromium, mutation, and publication-binding checks against that real URL.

No release gate is converted to PASS or bypassed by this adapter.
"""

from __future__ import print_function

import re
import tempfile
try:
    from urllib.parse import urlparse
except ImportError:  # pragma: no cover
    from urlparse import urlparse

from scripts.upgrade import run_upgrade as core
from scripts.upgrade.vfoswind_production_lifecycle import (
    VfoswindProductionLifecycle as BaseVfoswindProductionLifecycle,
    _literal,
)


_ORIGINAL_VALIDATE_BROWSER_URL = core._validate_external_candidate_browser_url
_ORIGINAL_VALIDATE_BROWSER_EVIDENCE = core._validate_candidate_browser_evidence
_ORIGINAL_VALIDATE_AUTH_EVIDENCE = core._validate_candidate_authenticated_mutation_evidence
_ORIGINAL_LIFECYCLE = core.VfoswindProductionLifecycle
_INSTALLED = False


def _origin(value):
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError("Candidate Gateway origin must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("Candidate Gateway origin may not contain credentials/query/fragment")
    hostname = str(parsed.hostname or "").strip().lower()
    if not hostname or hostname in ("localhost", "127.0.0.1", "::1"):
        raise RuntimeError("Candidate Gateway origin must not use loopback")
    if str(parsed.path or "").rstrip("/"):
        raise RuntimeError("Candidate Gateway origin must not include a path")
    return "{}://{}".format(parsed.scheme.lower(), parsed.netloc)


def _same_origin(url, configured_origin):
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    try:
        observed = _origin("{}://{}".format(parsed.scheme, parsed.netloc))
        expected = _origin(configured_origin)
    except Exception:
        return False
    return observed == expected


def _real_candidate_html_url(value, configured_origin):
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if not _same_origin(raw, configured_origin):
        return False
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    path = str(parsed.path or "")
    if not path or path == "/" or path.lower().startswith("/api/"):
        return False
    return path.lower().endswith((".html", ".htm"))


def _validate_airgapped_browser_contract(value):
    """Accept only an external origin at configuration time.

    A concrete HTML URL remains valid for compatibility, but the production
    air-gapped config should use an origin so no report filename is guessed.
    """
    raw = str(value or "").strip()
    try:
        _origin(raw)
        return []
    except Exception:
        return _ORIGINAL_VALIDATE_BROWSER_URL(raw)


def _validate_airgapped_browser_evidence(
        path, payload, identity, expected_url, expected_session_id="",
        expected_candidate_artifact_sha256="", expected_served_root_sha256=""):
    candidate_url = ""
    if isinstance(payload, dict):
        candidate_url = str(payload.get("candidate_url") or payload.get("page_url") or "").strip()
    try:
        configured_origin = _origin(expected_url)
    except Exception:
        return _ORIGINAL_VALIDATE_BROWSER_EVIDENCE(
            path, payload, identity, expected_url,
            expected_session_id=expected_session_id,
            expected_candidate_artifact_sha256=expected_candidate_artifact_sha256,
            expected_served_root_sha256=expected_served_root_sha256,
        )
    if not _real_candidate_html_url(candidate_url, configured_origin):
        return (["Candidate browser URL is not a manifest-derived HTML URL on the configured Candidate Gateway origin"], {})
    return _ORIGINAL_VALIDATE_BROWSER_EVIDENCE(
        path, payload, identity, candidate_url,
        expected_session_id=expected_session_id,
        expected_candidate_artifact_sha256=expected_candidate_artifact_sha256,
        expected_served_root_sha256=expected_served_root_sha256,
    )


def _validate_airgapped_auth_evidence(
        path, payload, identity, expected_url, expected_session_id="",
        expected_candidate_artifact_sha256="", expected_served_root_sha256="",
        expected_auth_mode="", expected_user_header="",
        expected_gateway_config_sha256=""):
    candidate_url = ""
    if isinstance(payload, dict):
        candidate_url = str(payload.get("candidate_url") or payload.get("page_url") or "").strip()
    try:
        configured_origin = _origin(expected_url)
    except Exception:
        return _ORIGINAL_VALIDATE_AUTH_EVIDENCE(
            path, payload, identity, expected_url,
            expected_session_id=expected_session_id,
            expected_candidate_artifact_sha256=expected_candidate_artifact_sha256,
            expected_served_root_sha256=expected_served_root_sha256,
            expected_auth_mode=expected_auth_mode,
            expected_user_header=expected_user_header,
            expected_gateway_config_sha256=expected_gateway_config_sha256,
        )
    if not _real_candidate_html_url(candidate_url, configured_origin):
        return (["Candidate authenticated mutation URL is not a manifest-derived HTML URL on the configured Candidate Gateway origin"], {})
    return _ORIGINAL_VALIDATE_AUTH_EVIDENCE(
        path, payload, identity, candidate_url,
        expected_session_id=expected_session_id,
        expected_candidate_artifact_sha256=expected_candidate_artifact_sha256,
        expected_served_root_sha256=expected_served_root_sha256,
        expected_auth_mode=expected_auth_mode,
        expected_user_header=expected_user_header,
        expected_gateway_config_sha256=expected_gateway_config_sha256,
    )


class ManifestDerivedGatewayLifecycle(BaseVfoswindProductionLifecycle):
    """Origin-only Candidate Gateway preflight for air-gapped production."""

    def _candidate_gateway_preflight(
            self, candidate_application_root="", candidate_ports=None,
            expected_browser_url=""):
        gateway = self.config.get("candidate_gateway") or {}
        if not isinstance(gateway, dict) or not gateway:
            raise RuntimeError("production Candidate Gateway configuration is required")
        path = self._candidate_gateway_path()
        if not path:
            raise RuntimeError("candidate_gateway.config_path is required")
        if path == self._nginx_file_path():
            raise RuntimeError("Candidate Gateway must use a separate Nginx config from production")
        text = self._read_file(path, "candidate_gateway_config")

        configured_origin = str(
            gateway.get("origin") or gateway.get("browser_origin") or
            gateway.get("browser_url") or expected_browser_url or ""
        ).strip()
        configured_origin = _origin(configured_origin)
        if expected_browser_url:
            expected_origin = _origin(expected_browser_url)
            if configured_origin != expected_origin:
                raise RuntimeError("Candidate Gateway origin does not match upgrade.candidate_browser_url")

        reports_root = _literal(
            gateway.get("reports_root") or self.validation_current_reports
        )
        if reports_root != self.validation_current_reports:
            raise RuntimeError(
                "candidate Gateway reports_root must be publish_root/VALIDATION_CURRENT/reports"
            )
        if not self._contains_alias(text, reports_root):
            raise RuntimeError(
                "Candidate Gateway Nginx alias is not bound to VALIDATION_CURRENT/reports"
            )
        static_location = str(gateway.get("static_location") or "/coverage/").strip()
        if not static_location.startswith("/") or not static_location.endswith("/"):
            raise RuntimeError("candidate_gateway.static_location must be a slash-delimited path")
        if not re.search(
                r"(?m)^[ \t]*location[ \t]+(?:=\s*)?{}[ \t]*\{{".format(
                    re.escape(static_location)
                ), text):
            raise RuntimeError("Candidate Gateway static location is not explicitly configured")

        ports = [int(port) for port in (candidate_ports or [])]
        if not ports:
            raise RuntimeError("Candidate Gateway requires an owned validation port")
        expected_proxy = str(gateway.get("proxy_pass") or "").strip()
        if not expected_proxy:
            expected_proxy = "http://127.0.0.1:{}".format(ports[0])
        if expected_proxy not in text:
            raise RuntimeError("Candidate Gateway proxy_pass does not match the validation API")
        api_location = str(gateway.get("api_location") or "/api/coverage").strip()
        if not re.search(
                r"(?m)^[ \t]*location[ \t]+(?:=\s*)?{}(?:[/$ \t{{]|$)".format(
                    re.escape(api_location)
                ), text):
            raise RuntimeError("Candidate Gateway API location is not explicitly configured")
        auth_bridge = self._validate_auth_bridge(text, "Candidate Gateway", api_location)
        with tempfile.TemporaryDirectory(prefix="coverage-candidate-gateway-check-") as stage:
            nginx_check = self._bootstrap_nginx_static_probe(text, stage)
        return {
            "status": "PASSED",
            "config_path": path,
            "config_sha256": self._sha256_bytes(text.encode("utf-8")),
            "browser_url_contract": "manifest_derived_same_origin",
            "candidate_gateway_origin": configured_origin,
            "browser_url": configured_origin,
            "static_location": static_location,
            "api_location": api_location,
            "reports_root": reports_root,
            "proxy_pass": expected_proxy,
            "validation_ports": ports,
            "auth_bridge": auth_bridge,
            "nginx_test": nginx_check,
            "read_only": True,
            "command": "read and syntax-check isolated Candidate Gateway Nginx config; report path manifest-derived at runtime",
            "exit_code": 0,
        }


def install_airgapped_gateway_contract():
    global _INSTALLED
    if _INSTALLED:
        return
    core._validate_external_candidate_browser_url = _validate_airgapped_browser_contract
    core._validate_candidate_browser_evidence = _validate_airgapped_browser_evidence
    core._validate_candidate_authenticated_mutation_evidence = _validate_airgapped_auth_evidence
    core.VfoswindProductionLifecycle = ManifestDerivedGatewayLifecycle
    _INSTALLED = True


def restore_canonical_gateway_contract():
    global _INSTALLED
    core._validate_external_candidate_browser_url = _ORIGINAL_VALIDATE_BROWSER_URL
    core._validate_candidate_browser_evidence = _ORIGINAL_VALIDATE_BROWSER_EVIDENCE
    core._validate_candidate_authenticated_mutation_evidence = _ORIGINAL_VALIDATE_AUTH_EVIDENCE
    core.VfoswindProductionLifecycle = _ORIGINAL_LIFECYCLE
    _INSTALLED = False
