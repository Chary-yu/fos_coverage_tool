"""Build auditable Gate 1-3 architecture/participation evidence."""

import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.release_identity import generate_release_identity
from scripts.diagnostics.canonical_ownership_audit import audit_canonical_ownership
from scripts.diagnostics.runtime_legacy_dependency_audit import audit as audit_legacy
from scripts.diagnostics.runtime_participation_audit import audit as audit_participation


CAPABILITIES = [
    ("DB CRUD", "enhance_coverage.py::_LegacyDatabaseManager",
     ["enhance_coverage.py"], "app/db/repositories/*", "enhance_coverage.py", "transitional"),
    ("Analysis", "enhance_coverage.py::DatabaseManager",
     ["enhance_coverage.py"], "app/services/analysis_service.py", "enhance_coverage.py", "transitional"),
    ("Line Index", "enhance_coverage.py::sync_line_index",
     ["enhance_coverage.py"], "app/db/repositories/line_index_repository.py", "enhance_coverage.py", "partial"),
    ("Project State", "enhance_coverage.py::coverage_project_state",
     ["enhance_coverage.py"], "app/db/repositories/project_state_repository.py", "enhance_coverage.py", "partial"),
    ("Progress", "app/progress/file_state_service.py",
     ["enhance_coverage.py", "app/progress/service.py"], "app/services/progress_service.py", "enhance_coverage.py", "partial"),
    ("Background Job", "enhance_coverage.py::recover_background_jobs",
     ["enhance_coverage.py", "app/jobs/service.py"], "app/jobs/service.py", "enhance_coverage.py", "partial"),
    ("Export", "enhance_coverage.py::export_report",
     ["enhance_coverage.py"], "app/jobs/excel_streaming.py", "enhance_coverage.py", "partial"),
    ("Code Detail", "code_detail_service.py",
     ["code_detail_service.py", "source_reader.py", "code_region.py"], "app/code_detail", "enhance_coverage.py", "transitional"),
    ("Sidecar", "app/code_detail/sidecar_store.py",
     ["enhance_coverage.py"], "app/code_detail/sidecar_store.py", "enhance_coverage.py", "wired"),
    ("Report Registry", "enhance_coverage.py + code_detail_service.py",
     ["enhance_coverage.py", "code_detail_service.py"], "app/reports/registry.py", "enhance_coverage.py", "transitional"),
    ("Inject", "enhance_coverage.py::inject_coverage_report",
     ["enhance_coverage.py", "app/inject/service.py"], "app/inject/service.py", "enhance_coverage.py", "partial"),
    ("Incremental", "coverage_check.py",
     ["coverage_check.py", "app/incremental"], "app/incremental/orchestrator.py", "coverage_check.py", "partial"),
    ("Git Diff/Blame", "coverage_check.py",
     ["coverage_check.py"], "app/incremental/git_diff.py + blame.py", "coverage_check.py", "partial"),
    ("Path Mapping", "app/incremental/path_index.py",
     ["coverage_check.py", "app/incremental"], "app/incremental/path_index.py", "coverage_check.py", "wired"),
    ("Release Identity", "app/release_identity.py",
     ["enhance_coverage.py"], "app/release_identity.py", "enhance_coverage.py", "wired"),
    ("Upgrade Write Freeze", "app/upgrade/lifecycle.py",
     ["enhance_coverage.py"], "app/api/auth.py", "enhance_coverage.py", "partial"),
    ("Web Assets", "web/assets",
     ["web/assets"], "web/assets", "root compatibility copies", "wired"),
]


def _sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(repo_root):
    try:
        revision = subprocess.check_output(
            ["git", "-C", repo_root, "rev-parse", "HEAD"]
        ).decode("ascii").strip()
    except Exception:
        revision = ""
    identity = generate_release_identity(repo_root=repo_root)
    matrix = []
    for capability, current, entrypoints, target, compatibility, status in CAPABILITIES:
        matrix.append({
            "capability": capability,
            "current_owner": current,
            "current_entrypoints": entrypoints,
            "target_owner": target,
            "compatibility_entrypoint": compatibility,
            "migration_status": status,
        })
    asset_pairs = {}
    for root_name, canonical in (
        ("coverage_enhance.js", "web/assets/js/coverage_enhance.js"),
        ("coverage_progress.js", "web/assets/js/coverage_progress.js"),
        ("incremental_coverage.js", "web/assets/js/incremental_coverage.js"),
        ("incremental_developer_tasks.js", "web/assets/js/incremental_developer_tasks.js"),
        ("coverage_enhance.css", "web/assets/css/coverage_enhance.css"),
        ("coverage_progress.html", "web/templates/coverage_progress.html"),
    ):
        root_path = os.path.join(repo_root, root_name)
        canonical_path = os.path.join(repo_root, canonical)
        asset_pairs[root_name] = {
            "root_sha256": _sha(root_path) if os.path.isfile(root_path) else "",
            "canonical_sha256": _sha(canonical_path) if os.path.isfile(canonical_path) else "",
        }
    return {
        "evidence_class": "architecture_audit",
        "revision": revision,
        "release_identity": identity,
        "schema_version": 1,
        "asset_pairs": asset_pairs,
        "capabilities": matrix,
        "canonical_ownership": audit_canonical_ownership(repo_root),
        "runtime_participation": audit_participation(),
        "runtime_legacy_dependency": audit_legacy(repo_root),
    }


def main(argv=None):
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    output = (argv or sys.argv[1:] or ["artifacts/vnext/architecture_ownership_matrix.json"])[0]
    if not os.path.isabs(output):
        output = os.path.join(root, output)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, "w", encoding="utf-8") as stream:
        json.dump(build(root), stream, ensure_ascii=False, indent=2, sort_keys=True)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
