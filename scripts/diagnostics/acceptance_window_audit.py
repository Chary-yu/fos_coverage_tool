"""Validate the non-negotiable Gate F acceptance-window exit conditions."""

from __future__ import print_function

import argparse
import datetime
import json
import os
import re
import sys

# The tool is intentionally executable both as a module and as the documented
# repository-root script.  Establish the repository import root before
# loading the shared UTC/evidence helper.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.time_utils import utc_iso


REQUIRED_SKILLS = (
    "fos-coverage-maintainer",
    "fos-coverage-change-review",
    "fos-coverage-release-governance",
    "fos-coverage-runtime-reliability",
    "fos-coverage-performance-ui",
)


def _parse_time(value):
    value = str(value or "").strip()
    if not value:
        return None
    timezone = datetime.timezone.utc
    if value.endswith("Z"):
        normalized = value[:-1]
    else:
        offset_match = re.search(r"([+-])(\d{2}):(\d{2})$", value)
        if offset_match:
            sign = 1 if offset_match.group(1) == "+" else -1
            hours = int(offset_match.group(2))
            minutes = int(offset_match.group(3))
            if hours > 23 or minutes > 59:
                return None
            timezone = datetime.timezone(sign * datetime.timedelta(
                hours=hours, minutes=minutes
            ))
            normalized = value[:offset_match.start()]
        else:
            normalized = value
    for pattern in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.datetime.strptime(normalized[:26], pattern)
            return parsed.replace(tzinfo=timezone).astimezone(
                datetime.timezone.utc
            )
        except (TypeError, ValueError):
            continue
    return None


def audit(payload, now=None):
    payload = dict(payload or {})
    now_value = _parse_time(now or payload.get("now")) or datetime.datetime.now(
        datetime.timezone.utc
    )
    started = _parse_time(payload.get("window_started_at"))
    ended = _parse_time(payload.get("window_ends_at"))
    violations = []
    if started is None:
        violations.append("window_started_at is missing or invalid")
    if ended is None:
        violations.append("window_ends_at is missing or invalid")
    duration_hours = 0
    if started and ended:
        duration_hours = (ended - started).total_seconds() / 3600.0
        if duration_hours < 48:
            violations.append("acceptance window is shorter than 48 hours")
        if now_value < ended:
            violations.append("acceptance window has not elapsed")
    scans = payload.get("successful_scans") or []
    if not isinstance(scans, list):
        scans = []
    if len(scans) < 3:
        violations.append("at least 3 successful scans are required")
    if not any(bool(item.get("inheritance")) for item in scans
               if isinstance(item, dict)):
        violations.append("at least one successful scan must exercise inheritance")
    if not any(bool(item.get("ordinary_pending")) or
               bool(item.get("no_inheritance")) for item in scans
               if isinstance(item, dict)):
        violations.append("at least one scan must exercise ordinary pending/no inheritance")
    restart_recovery_passes = _nonnegative_int(
        payload.get("restart_recovery_passes"), "restart_recovery_passes", violations
    )
    large_file_checks = _nonnegative_int(
        payload.get("large_file_checks"), "large_file_checks", violations
    )
    db_integrity_failures = _nonnegative_int(
        payload.get("db_integrity_failures"), "db_integrity_failures", violations
    )
    semantic_hash_failures = _nonnegative_int(
        payload.get("semantic_hash_failures"), "semantic_hash_failures", violations
    )
    if restart_recovery_passes < 1:
        violations.append("durable recovery after a normal restart is missing")
    if large_file_checks < 1:
        violations.append("large-file Code Detail acceptance is missing")
    open_findings = payload.get("open_p0_p1") or []
    if open_findings:
        violations.append("P0/P1 findings remain open")
    if payload.get("technical_failure_trend") not in (None, "stable", "decreasing"):
        violations.append("critical error/technical_failure trend is not stable")
    if db_integrity_failures != 0:
        violations.append("database authoritative integrity checks failed")
    if semantic_hash_failures != 0:
        violations.append("authoritative semantic checks failed")
    return {
        "status": "PASSED" if not violations else "INCOMPLETE",
        "evidence_class": "acceptance_window",
        "synthetic": False,
        "checked_at": utc_iso(),
        "window_duration_hours": round(duration_hours, 3),
        "successful_scan_count": len(scans),
        "required_skills": list(REQUIRED_SKILLS),
        "violations": violations,
        "command_or_action": "python scripts/diagnostics/acceptance_window_audit.py",
        "exit_code": 0 if not violations else 1,
    }


def _nonnegative_int(value, field_name, violations):
    """Parse a counter without allowing malformed evidence to crash the audit."""
    if value in (None, ""):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        violations.append("{} must be a non-negative integer".format(field_name))
        return 0
    if parsed < 0:
        violations.append("{} must be a non-negative integer".format(field_name))
        return 0
    return parsed


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        with open(args.input, "r", encoding="utf-8") as stream:
            payload = json.load(stream)
        result = audit(payload)
    except (OSError, TypeError, ValueError) as exc:
        # An evidence parser failure is itself an incomplete audit, not an
        # unstructured traceback that can be mistaken for an infrastructure
        # outage or silently omitted from the release bundle.
        result = audit({})
        result["violations"].insert(
            0, "acceptance-window input could not be read: {}".format(exc)
        )
        result["exit_code"] = 1
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = os.path.abspath(args.output)
        directory = os.path.dirname(output)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(output, "w", encoding="utf-8") as stream:
            stream.write(encoded)
    print(encoded)
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
