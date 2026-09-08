"""Build a bounded, priority-first summary from an upgrade evidence manifest.

The R7 incident collector recursively walked a multi-million-byte report list
and exhausted its output budget before reaching the actual failure records.
This helper uses an explicit allowlist for failure-critical sections and emits
only aggregate report metadata.  It also redacts credential values before
serialization.
"""

from __future__ import print_function

import argparse
import json
import os
import re
import sys


PRIORITY_KEYS = (
    "validation_failure",
    "validation_runtime_preflight",
    "api_start",
    "candidate_release_endpoint",
    "validation_session_manifest",
    "validation_teardown",
    "disposable_target",
    "disposable_target_cleanup",
    "production_integration_rollback",
    "pre_cutover_ready",
    "file_cutover",
    "release_identity",
)
_SECRET_KEY = re.compile(
    r"password|passwd|secret|token|authorization|cookie|api[_-]?key|private[_-]?key",
    re.I,
)


def _redact_text(value):
    text = str(value or "")
    text = re.sub(
        r"(?i)(IDENTIFIED\s+BY\s+PASSWORD\s+)(?:'[^']*'|\S+)",
        r"\1'<REDACTED>'", text,
    )
    text = re.sub(
        r"(?i)(IDENTIFIED\s+BY\s+)(?:'[^']*'|\S+)",
        r"\1'<REDACTED>'", text,
    )
    text = re.sub(
        r"(?i)((?:password|passwd|secret|token|authorization|cookie)\s*[:=]\s*)"
        r"(?:'[^']*'|\"[^\"]*\"|[^\s,;]+)",
        r"\1<REDACTED>", text,
    )
    return text


def _safe(value, key=""):
    if _SECRET_KEY.search(str(key or "")):
        return "<REDACTED>"
    if isinstance(value, dict):
        return {str(item_key): _safe(item_value, item_key)
                for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_safe(item, key) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _report_summary(manifest):
    prepared = manifest.get("candidate_release_prepared") or {}
    release_manifest = prepared.get("release_manifest") or {}
    reports = release_manifest.get("reports") or []
    if not isinstance(reports, list):
        return {"report_count": 0, "invalid_reports_shape": True}
    modes = {}
    sidecar_schemas = {}
    report_ids_present = 0
    for report in reports:
        if not isinstance(report, dict):
            continue
        mode = str(report.get("report_mode") or report.get("mode") or "<missing>")
        modes[mode] = modes.get(mode, 0) + 1
        schema = str(report.get("sidecar_schema") or "0")
        sidecar_schemas[schema] = sidecar_schemas.get(schema, 0) + 1
        if report.get("report_id"):
            report_ids_present += 1
    return {
        "report_count": len(reports),
        "report_modes": dict(sorted(modes.items())),
        "sidecar_schema_counts": dict(sorted(sidecar_schemas.items())),
        "report_ids_present": report_ids_present,
        "entries_expanded": False,
    }


def build_summary(manifest):
    if not isinstance(manifest, dict):
        raise ValueError("upgrade evidence manifest must be a JSON object")
    priority = {}
    for key in PRIORITY_KEYS:
        value = manifest.get(key)
        if value is not None:
            priority[key] = _safe(value, key)
    return {
        "schema_version": 1,
        "status": manifest.get("status", ""),
        "release_decision": manifest.get("release_decision", ""),
        "priority": priority,
        "report_summary": _report_summary(manifest),
        "credentials_written_to_evidence": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    with open(args.manifest, "r", encoding="utf-8") as stream:
        manifest = json.load(stream)
    summary = build_summary(manifest)
    output = os.path.abspath(args.output)
    directory = os.path.dirname(output)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    temporary = "{}.tmp-{}".format(output, os.getpid())
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    os.replace(temporary, output)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
