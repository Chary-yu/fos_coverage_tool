"""Validate browser performance evidence without fabricating server metrics."""

import argparse
import hashlib
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


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit(path, allow_partial=False, require_cross_layer=False,
          require_release_eligible=False):
    violations = []
    missing_cross_layer = []
    if not os.path.isfile(path):
        return with_contract({
            "status": "FAILED",
            "evidence_class": "performance_evidence_audit",
            "path": os.path.abspath(path),
            "browser_status": "FAILED",
            "gate": "cross_layer_performance" if require_cross_layer else "browser_functional",
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
            "browser_status": "FAILED",
            "gate": "cross_layer_performance" if require_cross_layer else "browser_functional",
            "violations": ["performance evidence is not valid JSON: {}".format(exc)],
            "missing_cross_layer_metrics": [],
        })

    # A complete set of measurements is not enough to make an artifact safe
    # for release decisions.  CI's browser fixture deliberately contains
    # server-shaped counters, but it is still generated from a disposable
    # fixture and must remain outside the release gate.  The manual
    # cross-layer lane and the real Candidate lane opt into this stricter
    # provenance check explicitly.
    if require_release_eligible:
        if evidence.get("synthetic") is not False:
            violations.append(
                "release-eligible performance evidence must explicitly set synthetic=false"
            )
        if evidence.get("release_eligible") is not True:
            violations.append(
                "release-eligible performance evidence must explicitly set release_eligible=true"
            )

    # A release A/B artifact has a stronger contract than the browser-only
    # functional fixture: two independent exact-revision inputs, one workload
    # identity, one comparable environment, and hashed source artifacts.
    if evidence.get("evidence_class") == "release_performance_ab":
        if evidence.get("comparison_type") != "release_revision_ab":
            violations.append("release performance comparison type is not release_revision_ab")
        if not evidence.get("baseline_commit") or not evidence.get("candidate_commit") or \
                evidence.get("baseline_commit") == evidence.get("candidate_commit"):
            violations.append("release performance baseline/candidate commit identity is invalid")
        if not evidence.get("workload_hash") or not isinstance(evidence.get("environment_identity"), dict):
            violations.append("release performance workload/environment identity is incomplete")
        for tier_name in ("Tier_A_1k", "Tier_B_10k", "Tier_C_50k", "Tier_D_100k"):
            tier = evidence.get(tier_name) or {}
            if tier.get("status") != "PASSED" or not _finite_number(tier.get("baseline_ms")) or \
                    not _finite_number(tier.get("candidate_ms")):
                violations.append("release performance tier is incomplete: {}".format(tier_name))
        source_artifacts = evidence.get("source_artifacts") or {}
        for role in ("baseline", "candidate"):
            source = source_artifacts.get(role) if isinstance(source_artifacts, dict) else None
            source_path = source.get("path") if isinstance(source, dict) else ""
            source_sha = source.get("sha256") if isinstance(source, dict) else ""
            if not source_path or not os.path.isabs(str(source_path)) or not os.path.isfile(str(source_path)):
                violations.append("release performance {} source artifact is missing".format(role))
            elif not source_sha or _sha256(str(source_path)) != str(source_sha):
                violations.append("release performance {} source artifact SHA256 mismatch".format(role))

    workload = evidence.get("coverage_virtual_scroll_100k") or {}
    required = (
        "request_count", "response_bytes", "max_response_bytes",
        "time_to_first_visible_ms", "time_to_target_line_ms",
        "logical_line_count", "resident_js_lines", "resident_js_lines_peak",
        "dom_line_count",
    )
    missing = [name for name in required if not _finite_number(workload.get(name))]
    if missing:
        violations.append("missing browser performance fields: {}".format(", ".join(missing)))
    if workload.get("status") != "PASSED":
        violations.append("100k virtual-scroll workload did not pass")
    if workload.get("logical_line_count") != 100000:
        violations.append("100k workload does not report logical_line_count=100000")
    if _finite_number(workload.get("resident_js_lines_peak")) and workload["resident_js_lines_peak"] > 8000:
        violations.append("resident JS lines exceeded the 8000-line sustained-scroll budget")
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
        if not _finite_number(workload.get(name)):
            missing_cross_layer.append(name)

    browser_status = "PASSED" if not violations else "FAILED"
    status = browser_status if not missing_cross_layer else "PARTIAL"
    if status == "PARTIAL" and (require_cross_layer or not allow_partial):
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
        "release_eligible": evidence.get("synthetic") is False and
            evidence.get("release_eligible") is True,
        "browser_status": browser_status,
        "gate": "cross_layer_performance" if require_cross_layer else "browser_functional",
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
    parser.add_argument(
        "--require-cross-layer", action="store_true",
        help="fail unless DB/Sidecar/latency/RSS evidence is present",
    )
    parser.add_argument(
        "--require-release-eligible", action="store_true",
        help="fail unless evidence is explicitly non-synthetic and release-eligible",
    )
    args = parser.parse_args(argv)
    result = audit(
        args.path, allow_partial=args.allow_partial,
        require_cross_layer=args.require_cross_layer,
        require_release_eligible=args.require_release_eligible,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["browser_status"] == "PASSED" and (
        result["status"] == "PASSED" or (args.allow_partial and not args.require_cross_layer)
    ) else 1


if __name__ == "__main__":
    sys.exit(main())
