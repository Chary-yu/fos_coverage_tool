"""Report identity validation and deterministic derivation."""

import hashlib
import re


REPORT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LEGACY_STATIC = "LEGACY_STATIC"
VNEXT_ARTIFACT_READY = "VNEXT_ARTIFACT_READY"
REPORT_MODES = (LEGACY_STATIC, VNEXT_ARTIFACT_READY)
# Sidecar v1 is retained for explicitly identified historical artifacts;
# newly generated VNext artifacts use the chunked v2 format.
SUPPORTED_SIDECAR_SCHEMA_VERSIONS = (1, 2)
DEFAULT_SIDECAR_SCHEMA_VERSION = 2


def validate_report_id(report_id):
    value = str(report_id or "").strip()
    if not REPORT_ID_RE.match(value):
        raise ValueError("invalid report_id")
    return value


def validate_report_mode(report_mode, default=LEGACY_STATIC):
    value = str(report_mode or default).strip().upper()
    if value not in REPORT_MODES:
        raise ValueError("invalid report_mode")
    return value


def derive_report_id(output_root, source_signature, scan_key=""):
    payload = "{}|{}|{}".format(output_root or "", source_signature or "", scan_key or "")
    return "report_{}".format(hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32])
