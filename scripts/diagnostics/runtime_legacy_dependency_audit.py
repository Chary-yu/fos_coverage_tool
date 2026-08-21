"""Audit whether VNext app runtime still imports root legacy business modules."""

import json
import os
import re
import sys

try:
    from scripts.diagnostics.contract import with_contract
except ModuleNotFoundError:
    from contract import with_contract


PATTERN = re.compile(
    r"^\s*(?:from|import)\s+(enhance_coverage|coverage_check|"
    r"code_detail_service|source_reader|code_region)\b",
    re.MULTILINE,
)

COMPATIBILITY_SHIMS = [
    "app/legacy_runtime.py",
    "app/incremental/legacy.py",
    "enhance_coverage.py",
    "coverage_check.py",
    "code_detail_service.py",
    "code_region.py",
    "source_reader.py",
]

TRANSITIONAL_LEGACY = [
    {
        "path": "app/compat/legacy_runtime_impl.py",
        "adapter": "app/legacy_runtime.py",
        "reason": "legacy CLI/server compatibility implementation remains available behind an adapter",
    },
    {
        "path": "app/compat/incremental_impl.py",
        "adapter": "app/incremental/legacy.py",
        "reason": "legacy incremental CLI/report generation remains available behind an adapter",
    },
]

CANONICAL_ONLY = [
    "app/bootstrap.py",
    "app/api/application.py",
    "app/api/endpoints/inheritance.py",
    "app/services/project_service.py",
    "app/services/analysis_service.py",
    "app/services/inheritance_review_service.py",
    "app/services/progress_service.py",
    "app/services/incremental_service.py",
    "app/incremental/orchestrator.py",
]


def audit(repo_root):
    findings = []
    app_root = os.path.join(repo_root, "app")
    for dirpath, _, filenames in os.walk(app_root):
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, "r", encoding="utf-8") as stream:
                text = stream.read()
            for match in PATTERN.finditer(text):
                findings.append({
                    "file": os.path.relpath(path, repo_root),
                    "module": match.group(1),
                    "line": text[:match.start()].count("\n") + 1,
                })
    transitional = []
    retired = []
    for item in TRANSITIONAL_LEGACY:
        entry = dict(item, classification="TRANSITIONAL_LEGACY")
        if os.path.isfile(os.path.join(repo_root, item["path"])):
            transitional.append(entry)
        else:
            retired.append(dict(item, classification="RETIRED"))

    classification = {
        "CANONICAL_ONLY": [
            {"path": path, "classification": "CANONICAL_ONLY"}
            for path in CANONICAL_ONLY
        ],
        "COMPATIBILITY_SHIM": [
            {"path": path, "classification": "COMPATIBILITY_SHIM"}
            for path in COMPATIBILITY_SHIMS
        ],
        "TRANSITIONAL_LEGACY": transitional,
        "RETIRED": retired,
    }
    return with_contract({
        "status": "PASSED" if not findings else "FAILED",
        "evidence_class": "architecture_audit",
        "legacy_imports": findings,
        "legacy_implementation_status": (
            "TRANSITIONAL_LEGACY" if transitional else "RETIRED"
        ),
        "classification": classification,
        "legacy_implementations": classification["TRANSITIONAL_LEGACY"],
        "is_valid": not findings,
    })


if __name__ == "__main__":
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    result = audit(root)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result["status"] == "PASSED" else 1)
