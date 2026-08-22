"""Report and gate the remaining legacy implementation owners."""

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.diagnostics.contract import with_contract
from scripts.diagnostics.runtime_legacy_dependency_audit import audit as audit_boundary


RETIREMENT_CONDITIONS = [
    "the per-capability retirement matrix is present and complete",
    "no supported deployment selects runtime_mode=legacy",
    "compatibility CLI/import tests pass from the shim surface",
    "no VNext module imports a transitional implementation",
    "legacy usage telemetry is zero for the agreed deprecation window",
    "the release manifest records the removal commit and rollback plan",
]

_FULL_GIT_SHA = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_PLACEHOLDER_ROLLBACK_PLANS = {
    "n/a", "na", "none", "pending", "tbd", "todo", "to be determined",
}
_RETIREMENT_MATRIX = os.path.join(
    "docs", "architecture", "legacy-retirement-matrix.md"
)
_MATRIX_CAPABILITIES = (
    "HTTP server/runtime composition", "Auth and mutation policy",
    "Jobs and recovery", "Progress aggregation", "Export",
    "Incremental analysis", "Inject / report binding", "Static assets",
    "`inherit` CLI mutation",
)


def _load_json(path):
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _revision(repo_root):
    try:
        return subprocess.check_output(
            ["git", "-C", os.path.abspath(repo_root), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode("ascii").strip()
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return ""


def _usage_telemetry(path=None):
    path = str(path or os.environ.get("COVERAGE_LEGACY_USAGE_FILE") or "").strip()
    if not path:
        return {"configured": False, "path": "", "available": False,
                "usage_counts": {}, "usage_total": None,
                "window_started_at": "", "window_ends_at": "",
                "window_complete": False}
    values = _load_json(path)
    if values is None:
        return {"configured": True, "path": os.path.abspath(path),
                "available": False, "usage_counts": {}, "usage_total": None,
                "window_started_at": "", "window_ends_at": "",
                "window_complete": False}
    counts = values.get("usage_counts") if isinstance(values.get("usage_counts"), dict) else values
    # Metadata keys are not usage counters. The adapter itself writes only
    # surface->count pairs, while an operator may add a deprecation-window
    # envelope to the same evidence file.
    counts = {
        key: value for key, value in counts.items()
        if key not in {"window_started_at", "window_ends_at", "usage_counts"}
    }
    try:
        usage_total = sum(int(item or 0) for item in counts.values())
    except (TypeError, ValueError):
        usage_total = None
    window_ends_at = str(values.get("window_ends_at") or "")
    window_complete = bool(window_ends_at and window_ends_at <= time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
    ))
    return {
        "configured": True, "path": os.path.abspath(path),
        "available": True, "usage_counts": counts,
        "usage_total": usage_total,
        "window_started_at": str(values.get("window_started_at") or ""),
        "window_ends_at": window_ends_at,
        "window_complete": window_complete,
    }


def _runtime_mode_checks(repo_root):
    paths = [
        os.path.join(repo_root, "coverage_config.json"),
        os.path.join(repo_root, "config", "coverage_config.example.json"),
        os.path.join(repo_root, "config", "coverage_config.staging.example.json"),
    ]
    configured = os.environ.get("COVERAGE_CONFIG_PATH")
    if configured:
        if not os.path.isabs(configured):
            configured = os.path.join(repo_root, configured)
        paths.append(configured)
    modes = []
    errors = []
    for path in dict.fromkeys(paths):
        values = _load_json(path)
        if values is None:
            errors.append(os.path.relpath(path, repo_root))
            continue
        modes.append({
            "path": os.path.relpath(path, repo_root),
            "runtime_mode": str(values.get("runtime_mode") or "legacy").lower(),
        })
    return {
        "passed": bool(modes) and not errors and all(
            item["runtime_mode"] != "legacy" for item in modes
        ),
        "configs": modes,
        "unreadable": errors,
    }


def _manifest_check(path, required_keys, expected_values=None,
                    expected_candidate_revision=""):
    values = _load_json(path)
    expected_values = dict(expected_values or {})
    missing_keys = [key for key in required_keys if not values or not values.get(key)]
    for key, expected in expected_values.items():
        if not values or values.get(key) != expected:
            missing_keys.append("{}={}".format(key, expected))
    if expected_candidate_revision and (
            not values or values.get("candidate_revision") != expected_candidate_revision
    ):
        missing_keys.append(
            "candidate_revision={}".format(expected_candidate_revision)
        )
    return {
        "configured": bool(path),
        "path": os.path.abspath(path) if path else "",
        "available": values is not None,
        "passed": bool(values is not None and not missing_keys),
        "missing_keys": missing_keys,
    }


def _retirement_manifest_check(path, expected_candidate_revision=""):
    """Validate retirement evidence without accepting placeholder values."""
    result = _manifest_check(
        path, ("candidate_revision", "removal_commit", "rollback_plan"),
        expected_candidate_revision=expected_candidate_revision,
    )
    values = _load_json(path)
    if values is not None:
        removal_commit = str(values.get("removal_commit") or "").strip()
        if not _FULL_GIT_SHA.fullmatch(removal_commit):
            result["missing_keys"].append("removal_commit=full_git_sha")

        rollback_plan = values.get("rollback_plan")
        if isinstance(rollback_plan, str):
            normalized = rollback_plan.strip().lower()
            rollback_valid = bool(normalized) and normalized not in _PLACEHOLDER_ROLLBACK_PLANS
        elif isinstance(rollback_plan, (dict, list, tuple)):
            rollback_valid = bool(rollback_plan)
        else:
            rollback_valid = False
        if not rollback_valid:
            result["missing_keys"].append("rollback_plan=non_placeholder")
    result["passed"] = bool(values is not None and not result["missing_keys"])
    return result


def _retirement_matrix_check(repo_root):
    path = os.path.join(repo_root, _RETIREMENT_MATRIX)
    try:
        with open(path, "r", encoding="utf-8") as stream:
            text = stream.read()
    except OSError as exc:
        return {
            "path": os.path.relpath(path, repo_root), "available": False,
            "passed": False, "missing": ["matrix: {}".format(exc)],
        }
    missing = [item for item in _MATRIX_CAPABILITIES if item not in text]
    for marker in ("VNext authoritative owner", "Current status", "Rollback path"):
        if marker not in text:
            missing.append("column: {}".format(marker))
    return {
        "path": os.path.relpath(path, repo_root), "available": True,
        "passed": not missing, "missing": missing,
    }


def audit(repo_root=ROOT, compatibility_manifest=None, retirement_manifest=None):
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    candidate_revision = _revision(repo_root)
    boundary = audit_boundary(repo_root)
    classification = boundary.get("classification", {})
    observed_transitional = list(classification.get("TRANSITIONAL_LEGACY", []) or [])
    observed_retired = list(classification.get("RETIRED", []) or [])
    expected_legacy = observed_transitional + observed_retired
    missing_files = [item["path"] for item in observed_retired]
    compatibility_manifest = compatibility_manifest or os.environ.get(
        "COVERAGE_LEGACY_COMPAT_TESTS_MANIFEST", ""
    )
    retirement_manifest = retirement_manifest or os.environ.get(
        "COVERAGE_LEGACY_RETIREMENT_MANIFEST", ""
    )
    telemetry = _usage_telemetry()
    runtime_modes = _runtime_mode_checks(repo_root)
    retirement_matrix = _retirement_matrix_check(repo_root)
    compatibility = _manifest_check(
        compatibility_manifest, ("status", "candidate_revision"),
        expected_values={"status": "PASSED"},
        expected_candidate_revision=candidate_revision,
    )
    release = _retirement_manifest_check(
        retirement_manifest, expected_candidate_revision=candidate_revision,
    )
    removal_proven = bool(
        expected_legacy and not observed_transitional and
        release["passed"] and
        {item.get("path") for item in observed_retired} == {
            item.get("path") for item in expected_legacy
        }
    )
    # A missing implementation is not silently treated as retired.  It only
    # transitions to RETIRED after the exact-SHA removal/rollback manifest is
    # present.  This keeps accidental deletion visible while making the final
    # retirement gate reachable.
    transitional = observed_transitional
    if not transitional and expected_legacy and not removal_proven:
        transitional = [
            dict(item, classification="TRANSITIONAL_LEGACY")
            for item in expected_legacy
        ]
    checks = {
        "retirement_matrix": retirement_matrix["passed"],
        "no_legacy_deployment": runtime_modes["passed"],
        "compatibility_tests": compatibility["passed"],
        "no_vnext_transitional_import": boundary.get("status") == "PASSED",
        "legacy_usage_zero_for_window": bool(
            telemetry.get("available") and telemetry.get("usage_total") == 0 and
            telemetry.get("window_complete")
        ),
        "release_manifest_records_removal_and_rollback": release["passed"],
    }
    boundary_clean = boundary.get("status") == "PASSED" and (
        not missing_files or removal_proven
    )
    conditions_satisfied = all(checks.values())
    gate_complete = not transitional and boundary_clean and conditions_satisfied
    return with_contract({
        "status": "PASSED" if gate_complete else (
            "FAILED" if not boundary_clean else "INCOMPLETE"
        ),
        "candidate_revision": candidate_revision,
        "evidence_class": "legacy_retirement_gate",
        "synthetic": False,
        "host_identity": {
            "hostname": platform.node(),
            "platform": platform.platform(),
        },
        "command_or_action": "python scripts/diagnostics/legacy_retirement_audit.py",
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gate_status": "PASSED" if gate_complete else "INCOMPLETE",
        "legacy_implementation_status": "TRANSITIONAL_LEGACY" if transitional else "RETIRED",
        "transitional_owners": transitional,
        "observed_retired_owners": observed_retired,
        "removal_proven": removal_proven,
        "missing_files": missing_files,
        "retirement_conditions": RETIREMENT_CONDITIONS,
        "retirement_checks": checks,
        "runtime_modes": runtime_modes,
        "retirement_matrix": retirement_matrix,
        "compatibility_evidence": compatibility,
        "retirement_manifest": release,
        "usage_telemetry": telemetry,
        "compatibility_tests": [
            "scripts.diagnostics.legacy_compatibility_smoke",
            "tests.vnext.test_runtime_config",
            "tests.code_detail.test_phase2_core",
            "tests.incremental.test_phase5_inject_path",
        ],
    })


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--compatibility-manifest")
    parser.add_argument("--retirement-manifest")
    parser.add_argument(
        "--output",
        help="write the exact-SHA retirement audit JSON to this path",
    )
    args = parser.parse_args(argv)
    result = audit(
        compatibility_manifest=args.compatibility_manifest,
        retirement_manifest=args.retirement_manifest,
    )
    exit_code = 0 if result["status"] == "PASSED" or not args.strict else 1
    result["exit_code"] = exit_code
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = args.output if os.path.isabs(args.output) else os.path.join(os.getcwd(), args.output)
        directory = os.path.dirname(os.path.abspath(output))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(output, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.write("\n")
    print(encoded)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
