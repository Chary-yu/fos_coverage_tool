"""Report and gate the remaining legacy implementation owners."""

import argparse
import json
import os
import sys

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


def audit(repo_root=ROOT):
    boundary = audit_boundary(repo_root)
    transitional = boundary.get("classification", {}).get("TRANSITIONAL_LEGACY", [])
    missing_files = [
        item["path"] for item in transitional
        if not os.path.isfile(os.path.join(repo_root, item["path"]))
    ]
    boundary_clean = boundary.get("status") == "PASSED" and not missing_files
    return with_contract({
        "status": "INCOMPLETE" if transitional else ("PASSED" if boundary_clean else "FAILED"),
        "evidence_class": "legacy_retirement_gate",
        "gate_status": "INCOMPLETE" if transitional else "PASSED",
        "legacy_implementation_status": "TRANSITIONAL_LEGACY" if transitional else "RETIRED",
        "transitional_owners": transitional,
        "missing_files": missing_files,
        "retirement_conditions": RETIREMENT_CONDITIONS,
        "compatibility_tests": [
            "tests.vnext.test_runtime_config",
            "tests.code_detail.test_phase2_core",
            "tests.incremental.test_phase5_inject_path",
        ],
    })


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASSED" or not args.strict else 1


if __name__ == "__main__":
    sys.exit(main())
