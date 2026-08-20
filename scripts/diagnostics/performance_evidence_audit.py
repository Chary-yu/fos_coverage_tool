"""Validate browser performance evidence without fabricating server metrics."""

import argparse
import json
import math
import os
import sys

try:
    from scripts.diagnostics.contract import with_contract
except ModuleNotFoundError:
    from contract import with_contract


def _finite_number(value):
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def audit(path, allow_partial=False):
    violations = []
    missing_cross_layer = []
    if not os.path.isfile(path):
        return with_contract({
            "status": "FAILED",
            "evidence_class": "performance_evidence_audit",
            "path": os.path.abspath(path),
            "violations": ["performance evidence file is missing"],
            "missing_cross_layer_metrics": [],
        })
    try:
        with open(path, "r", encoding="utf-8") as stream:
            evidence = json.load(stream)
    except Exception as exc:
        return with_contract({
            "status": "FAILED",
            "evidence_class": "performance_evidence_audit",
            "path": os.path.abspath(path),
            "violations": ["performance evidence is not valid JSON: {}".format(exc)],
            "missing_cross_layer_metrics": [],
        })

    workload = evidence.get("coverage_virtual_scroll_100k") or {}
    required = (
        "request_count", "response_bytes", "max_response_bytes",
        "time_to_first_visible_ms", "time_to_target_line_ms",
        "logical_line_count", "resident_js_lines", "dom_line_count",
    )
    missing = [name for name in required if not _finite_number(workload.get(name))]
    if missing:
        violations.append("missing browser performance fields: {}".format(", ".join(missing)))
    if workload.get("status") != "PASSED":
        violations.append("100k virtual-scroll workload did not pass")
    if workload.get("logical_line_count") != 100000:
        violations.append("100k workload does not report logical_line_count=100000")
    if _finite_number(workload.get("resident_js_lines")) and workload["resident_js_lines"] >= 2000:
        violations.append("resident JS lines exceeded the 2000-line budget")
    if _finite_number(workload.get("dom_line_count")) and workload["dom_line_count"] >= 1500:
        violations.append("DOM line count exceeded the 1500-line budget")

    telemetry = workload.get("telemetry_after_scroll") or workload.get("telemetry") or {}
    for name in ("api_requests", "network_chunks", "network_lines", "max_dom_lines"):
        if not _finite_number(telemetry.get(name)):
            violations.append("missing browser telemetry field: {}".format(name))

    # These fields must only be marked collected when an actual server-side
    # instrumented workload supplies them. The current browser A/B fixture is
    # intentionally browser-only; keeping this list explicit prevents a DOM
    # benchmark from being misreported as DB/Sidecar/RSS evidence.
    for name in (
        "overlay_db_queries", "overlay_db_rows", "sidecar_decode_count",
        "p95_expand_ms", "peak_rss_bytes",
    ):
        if name not in workload:
            missing_cross_layer.append(name)

    browser_status = "PASSED" if not violations else "FAILED"
    status = browser_status if not missing_cross_layer else "PARTIAL"
    if status == "PARTIAL" and not allow_partial:
        violations.append(
            "cross-layer performance metrics are not present: {}".format(
                ", ".join(missing_cross_layer)
            )
        )
        status = "FAILED"
    return with_contract({
        "status": status,
        "evidence_class": "performance_evidence_audit",
        "path": os.path.abspath(path),
        "browser_status": browser_status,
        "browser_workload": workload.get("workload_id", ""),
        "missing_cross_layer_metrics": missing_cross_layer,
        "violations": violations,
    })


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument(
        "--allow-partial", action="store_true",
        help="allow browser-only evidence while reporting missing server metrics",
    )
    args = parser.parse_args(argv)
    result = audit(args.path, allow_partial=args.allow_partial)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["browser_status"] == "PASSED" and (
        result["status"] == "PASSED" or args.allow_partial
    ) else 1


if __name__ == "__main__":
    sys.exit(main())
