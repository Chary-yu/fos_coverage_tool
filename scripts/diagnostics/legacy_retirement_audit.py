"""Report and gate the remaining legacy implementation owners."""

import argparse
import json
import os
import platform
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.diagnostics.contract import with_contract
from scripts.diagnostics.runtime_legacy_dependency_audit import audit as audit_boundary


RETIREMENT_CONDITIONS = [
    "no supported deployment selects runtime_mode=legacy",
    "compatibility CLI/import tests pass from the shim surface",
    "no VNext module imports a transitional implementation",
    "legacy usage telemetry is zero for the agreed deprecation window",
    "the release manifest records the removal commit and rollback plan",
]


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


def audit(repo_root=ROOT, compatibility_manifest=None, retirement_manifest=None):
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    candidate_revision = _revision(repo_root)
    boundary = audit_boundary(repo_root)
    transitional = boundary.get("classification", {}).get("TRANSITIONAL_LEGACY", [])
    missing_files = [
        item["path"] for item in transitional
        if not os.path.isfile(os.path.join(repo_root, item["path"]))
    ]
    compatibility_manifest = compatibility_manifest or os.environ.get(
        "COVERAGE_LEGACY_COMPAT_TESTS_MANIFEST", ""
    )
    retirement_manifest = retirement_manifest or os.environ.get(
        "COVERAGE_LEGACY_RETIREMENT_MANIFEST", ""
    )
    telemetry = _usage_telemetry()
    runtime_modes = _runtime_mode_checks(repo_root)
    compatibility = _manifest_check(
        compatibility_manifest, ("status", "candidate_revision"),
        expected_values={"status": "PASSED"},
        expected_candidate_revision=candidate_revision,
    )
    release = _manifest_check(
        retirement_manifest,
        ("candidate_revision", "removal_commit", "rollback_plan"),
        expected_candidate_revision=candidate_revision,
    )
    checks = {
        "no_legacy_deployment": runtime_modes["passed"],
        "compatibility_tests": compatibility["passed"],
        "no_vnext_transitional_import": boundary.get("status") == "PASSED",
        "legacy_usage_zero_for_window": bool(
            telemetry.get("available") and telemetry.get("usage_total") == 0 and
            telemetry.get("window_complete")
        ),
        "release_manifest_records_removal_and_rollback": release["passed"],
    }
    boundary_clean = boundary.get("status") == "PASSED" and not missing_files
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
        "missing_files": missing_files,
        "retirement_conditions": RETIREMENT_CONDITIONS,
        "retirement_checks": checks,
        "runtime_modes": runtime_modes,
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
    return 0 if result["status"] == "PASSED" or not args.strict else 1


if __name__ == "__main__":
    sys.exit(main())
