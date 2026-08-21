"""Audit canonical source ownership and generated compatibility copies."""

import hashlib
import os
import re
from typing import Dict, Any

try:
    from scripts.diagnostics.contract import with_contract
except ModuleNotFoundError:
    from contract import with_contract


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def audit_canonical_ownership(repo_root: str) -> Dict[str, Any]:
    mappings = {
        "coverage_enhance.js": "web/assets/js/coverage_enhance.js",
        "coverage_progress.js": "web/assets/js/coverage_progress.js",
        "incremental_coverage.js": "web/assets/js/incremental_coverage.js",
        "incremental_developer_tasks.js": "web/assets/js/incremental_developer_tasks.js",
        "coverage_enhance.css": "web/assets/css/coverage_enhance.css",
        "coverage_progress.html": "web/templates/coverage_progress.html",
    }
    violations = []
    copies = []
    for compatibility, canonical in mappings.items():
        compat_path = os.path.join(repo_root, compatibility)
        canonical_path = os.path.join(repo_root, canonical)
        if not os.path.isfile(canonical_path):
            violations.append("missing canonical source: {}".format(canonical))
            continue
        if os.path.isfile(compat_path):
            if _sha(compat_path) != _sha(canonical_path):
                violations.append("generated compatibility copy drift: {}".format(compatibility))
            else:
                    copies.append(compatibility)
    required_modules = [
        "app/bootstrap.py",
        "app/api/handler.py",
        "app/api/router.py",
        "app/api/auth.py",
        "app/api/endpoints/inheritance.py",
        "app/api/serialization.py",
        "app/db/transaction.py",
        "app/db/repositories/project_repository.py",
        "app/db/repositories/analysis_repository.py",
        "app/db/repositories/line_index_repository.py",
        "app/db/repositories/project_state_repository.py",
        "app/db/repositories/file_state_repository.py",
        "app/db/repositories/job_repository.py",
        "app/reports/registry.py",
        "app/services/project_service.py",
        "app/services/analysis_service.py",
        "app/services/inheritance_review_service.py",
        "app/services/progress_service.py",
        "app/services/export_service.py",
        "app/services/incremental_service.py",
        "app/incremental/orchestrator.py",
        "app/incremental/path_index.py",
        "app/incremental/lcov.py",
        "app/incremental/git_diff.py",
        "app/incremental/blame.py",
        "app/inject/service.py",
        "app/jobs/service.py",
        "scripts/upgrade/vnext_schema.sql",
        "scripts/upgrade/migration_runner.py",
    ]
    missing_modules = [
        path for path in required_modules
        if not os.path.isfile(os.path.join(repo_root, path))
    ]
    transitional = []
    shim_specs = {
        "enhance_coverage.py": ("app.legacy_runtime", ("runpy.run_module",)),
        "coverage_check.py": ("app.incremental.legacy", ("sys.modules[__name__]",)),
        "code_detail_service.py": ("app.code_detail.service", ("from app.code_detail.service",)),
        "code_region.py": ("app.code_detail.code_region", ("from app.code_detail.code_region",)),
        "source_reader.py": ("app.code_detail.source_reader", ("from app.code_detail import source_reader",)),
    }
    shim_results = {}
    for relative_path, (owner, required_patterns) in shim_specs.items():
        path = os.path.join(repo_root, relative_path)
        if not os.path.isfile(path):
            transitional.append("missing_compatibility_shim:{}".format(relative_path))
            shim_results[relative_path] = {"owner": owner, "status": "missing"}
            continue
        with open(path, "r", encoding="utf-8") as stream:
            root_text = stream.read()
        missing = [pattern for pattern in required_patterns if pattern not in root_text]
        if missing:
            transitional.append("non_delegating_compatibility_shim:{}".format(relative_path))
        shim_results[relative_path] = {
            "owner": owner,
            "status": "valid" if not missing else "invalid",
            "missing_patterns": missing,
        }
    root_path = os.path.join(repo_root, "enhance_coverage.py")
    if os.path.isfile(root_path):
        with open(root_path, "r", encoding="utf-8") as stream:
            root_text = stream.read()
        for name, pattern in (
            ("root_api_handler", r"class\s+CoverageHTTPRequestHandler"),
            ("root_database_owner", r"class\s+_LegacyDatabaseManager"),
            ("root_report_registry_owner", r"def\s+(?:register_report_directory|load_report_registry)"),
            ("root_job_recovery_owner", r"def\s+recover_background_jobs"),
            ("root_incremental_owner", r"def\s+generate_(?:multi_repo_)?incremental_review"),
        ):
            if re.search(pattern, root_text):
                transitional.append(name)
    violations.extend("missing VNext canonical module: {}".format(path)
                      for path in missing_modules)
    status = "PASSED" if not violations and not transitional else "FAILED"
    return with_contract({"status": status,
            "canonical_sources": sorted(mappings.values()),
            "compatibility_copies": copies, "violations": violations,
            "transitional_root_owners": transitional,
            "compatibility_shims": shim_results,
            "missing_vnext_modules": missing_modules,
            "is_valid": not violations and not transitional})


if __name__ == "__main__":
    import json
    import sys

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    result = audit_canonical_ownership(root)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result["status"] == "PASSED" else 1)
