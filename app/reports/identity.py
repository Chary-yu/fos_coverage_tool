"""Report identity validation and deterministic derivation."""

import hashlib
import re


REPORT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_report_id(report_id):
    value = str(report_id or "").strip()
    if not REPORT_ID_RE.match(value):
        raise ValueError("invalid report_id")
    return value


def derive_report_id(output_root, source_signature, scan_key=""):
    payload = "{}|{}|{}".format(output_root or "", source_signature or "", scan_key or "")
    return "report_{}".format(hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32])
