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
        _check("mysql_connection_pool", ["enhance_coverage.py", "app/db/manager.py"], [
            r"from app\.db\.connection_pool import get_global_pool",
            r"get_global_pool\(self\.config\)",
            r"DatabaseManager\(config, exit_on_error=False, init_schema=False\)",
        ]),
        _check("bounded_background_executor", ["enhance_coverage.py", "app/jobs/service.py"], [
            r"from app\.jobs\.bounded_executor import BoundedJobExecutor",
            r"BoundedJobExecutor\(",
            r"_get_background_executor\(\)\.submit_job",
            r"class BackgroundJobService",
        ]),
        _check("progress_aggregate_service", ["enhance_coverage.py", "app/progress/service.py"], [
            r"from app\.progress\.service import ProgressService",
            r"ProgressService\(connection\)\.project_summary",
            r"query_project_progress_aggregated",
        ]),
        _check("bounded_excel_export", ["enhance_coverage.py", "app/jobs/excel_streaming.py"], [
            r"from app\.jobs\.excel_streaming import export_project_coverage_streaming_zip",
            r"export_project_coverage_streaming_zip\(",
            r"MAX_INFLIGHT_DIR_EXPORTS",
        ]),
        _check("inject_parse_once", ["enhance_coverage.py", "app/inject/service.py", "app/inject/parse_once.py"], [
            r"from app\.inject\.service import InjectService",
            r"InjectService\.parse_once\(",
            r"class ParsedSourceArtifact",
        ]),
        _check("directory_signature", ["enhance_coverage.py", "app/inject/directory_signature.py"], [
            r"from app\.inject\.directory_signature import calculate_directory_signature_incremental",
            r"calculate_directory_signature_incremental\(",
            r"manifest_path=manifest_path",
        ]),
        _check("lcov_path_index", ["coverage_check.py", "app/incremental/path_index.py"], [
            r"from app\.incremental\.service import IncrementalService",
            r"IncrementalService\(\{\"repo\": list\(coverage_data\.keys\(\)\)\}\)",
            r"class LCOVPathLookupIndex",
        ]),
        _check("chunked_sidecar", ["enhance_coverage.py", "app/code_detail/sidecar_store.py"], [
            r"from app\.code_detail\.sidecar_store import SidecarStore",
            r"save_chunked_sidecar\(",
            r"load_lines_range\(",
        ]),
        _check("release_identity_endpoint", ["enhance_coverage.py", "app/release_identity.py"], [
            r"verify_release_identity\(SCRIPT_DIR\)",
            r"release_manifest\.json",
            r"runtime never rewrites",
        ]),
        _check("write_freeze_auth_boundary", ["enhance_coverage.py", "app/upgrade/lifecycle.py"], [
            r"writes_are_frozen\(",
            r"def _authorize_mutation",
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
            "paths": ["enhance_coverage.py"], "missing_patterns": [],
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
