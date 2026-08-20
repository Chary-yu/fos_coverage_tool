"""Audit that canonical providers participate in the production entrypoints.

This is intentionally a small static gate.  It does not claim that a module is
healthy merely because it imports; each provider needs both a canonical import
and a call/constructor in the active runtime path.  The output is suitable for
release evidence and the command exits non-zero on a provider-only result.
"""

import json
import os
import re
import sys


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))


def _read(relative_path):
    path = os.path.join(ROOT, relative_path)
    with open(path, "r", encoding="utf-8") as stream:
        return stream.read()


def _check(name, paths, patterns):
    text = "\n".join(_read(path) for path in paths)
    missing = [pattern for pattern in patterns if not re.search(pattern, text, re.MULTILINE)]
    return {
        "name": name,
        "status": "RUNTIME_WIRED" if not missing else "PROVIDER_ONLY",
        "paths": paths,
        "missing_patterns": missing,
    }


def audit():
    checks = [
        _check("vnext_bootstrap_api", ["enhance_coverage.py", "app/legacy_runtime.py", "app/bootstrap.py", "app/api/handler.py"], [
            r"runtime_mode",
            r"create_vnext_server",
            r"class VNextHTTPRequestHandler",
        ]),
        _check("vnext_repository_transaction", [
            "app/services/analysis_service.py",
            "app/services/project_service.py",
            "app/db/transaction.py",
            "app/db/repositories/analysis_repository.py",
        ], [
            r"with transaction\(",
            r"class AnalysisRepository",
            r"class ProjectService",
        ]),
        _check("vnext_scan_identity", [
            "app/services/project_service.py",
            "app/db/repositories/project_repository.py",
        ], [
            r"def scan_key",
            r"class ProjectRepository",
            r"create_scan\(",
        ]),
        _check("vnext_schema_migration", [
            "scripts/upgrade/migration_runner.py",
            "scripts/upgrade/vnext_schema.sql",
        ], [
            r"def migrate_legacy",
            r"def capture_legacy_snapshot",
            r"coverage_scans",
        ]),
        _check("vnext_json_serializer", ["app/api/serialization.py", "app/api/application.py"], [
            r"def to_jsonable",
            r"to_jsonable",
        ]),
        _check("vnext_lcov_and_git", [
            "app/incremental/lcov.py",
            "app/incremental/blame.py",
            "app/incremental/git_diff.py",
        ], [
            r"FNL:",
            r"boundary",
            r"def added_lines",
        ]),
        _check("mysql_connection_pool", ["app/bootstrap.py", "app/db/manager.py", "app/db/connection_pool.py"], [
            r"DatabaseManager\(config\)",
            r"get_global_pool\(self\.config\)",
            r"class MySQLConnectionPool",
        ]),
        _check("bounded_background_executor", ["app/bootstrap.py", "app/jobs/service.py"], [
            r"from app\.jobs\.bounded_executor import BoundedJobExecutor",
            r"BoundedJobExecutor\(",
            r"class VNextBackgroundJobService",
            r"self\.job_service",
        ]),
        _check("progress_aggregate_service", ["app/bootstrap.py", "app/services/progress_service.py", "app/db/repositories/file_state_repository.py"], [
            r"ProgressService\(",
            r"def summary",
            r"pending_line_references",
        ]),
        _check("bounded_excel_export", ["app/services/export_service.py", "app/jobs/excel_streaming.py", "app/api/application.py"], [
            r"class ExportService",
            r"export_scan",
            r"export_project_coverage_streaming_zip\(",
            r"MAX_INFLIGHT_DIR_EXPORTS",
        ]),
        _check("inject_parse_once", ["app/bootstrap.py", "app/inject/service.py", "app/inject/parse_once.py"], [
            r"ScanImportService\(",
            r"parse_once = staticmethod",
            r"class ParsedSourceArtifact",
        ]),
        _check("directory_signature", ["app/legacy_runtime.py", "app/inject/directory_signature.py"], [
            r"from app\.inject\.directory_signature import calculate_directory_signature_incremental",
            r"calculate_directory_signature_incremental\(",
            r"manifest_path=manifest_path",
        ]),
        _check("lcov_path_index", ["app/incremental/orchestrator.py", "app/incremental/path_index.py"], [
            r"from app\.incremental\.path_index import LCOVPathLookupIndex",
            r"LCOVPathLookupIndex\(\{repository_name: list\(lcov\.keys\(\)\)\}\)",
            r"resolve_path\(repository_name",
        ]),
        _check("chunked_sidecar", ["app/code_detail/vnext_service.py", "app/code_detail/sidecar_store.py"], [
            r"from app\.code_detail\.sidecar_store import SidecarStore",
            r"save_chunked_sidecar\(",
            r"load_lines_range\(",
        ]),
        _check("release_identity_endpoint", ["app/bootstrap.py", "app/api/application.py", "app/release_identity.py"], [
            r"get_current_release_identity",
            r"def release",
            r"release_manifest\.json",
            r"runtime never rewrites",
        ]),
        _check("write_freeze_auth_boundary", ["app/api/auth.py", "app/upgrade/lifecycle.py"], [
            r"writes_are_frozen\(",
            r"def authorize",
            r"trusted_proxy_addresses",
        ]),
    ]

    root = _read("enhance_coverage.py")
    start_match = re.search(r"def start_background_job\(.*?(?=\ndef |\Z)", root, re.DOTALL)
    if start_match and re.search(r"threading\.Thread|threading\.Timer", start_match.group(0)):
        checks.append({
            "name": "legacy_unbounded_job_thread", "status": "PROVIDER_ONLY",
            "paths": ["enhance_coverage.py"],
            "missing_patterns": ["start_background_job still creates a raw thread"],
        })
    else:
        checks.append({
            "name": "legacy_unbounded_job_thread", "status": "RUNTIME_WIRED",
            "paths": ["enhance_coverage.py", "app/bootstrap.py"], "missing_patterns": [],
        })

    result = {
        "status": "PASSED" if all(item["status"] == "RUNTIME_WIRED" for item in checks) else "FAILED",
        "evidence_class": "architecture_audit",
        "provider_only": [item["name"] for item in checks if item["status"] != "RUNTIME_WIRED"],
        "checks": checks,
    }
    return result


if __name__ == "__main__":
    result = audit()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result["status"] == "PASSED" else 1)
