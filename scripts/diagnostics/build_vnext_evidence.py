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
from scripts.diagnostics.contract import CONTRACT_VERSION
from scripts.diagnostics.canonical_ownership_audit import audit_canonical_ownership
from scripts.diagnostics.runtime_legacy_dependency_audit import audit as audit_legacy
from scripts.diagnostics.runtime_participation_audit import audit as audit_participation
from scripts.diagnostics.frontend_vnext_api_contract_audit import audit as audit_frontend_contract
from scripts.diagnostics.scan_immutability_audit import audit as audit_scan_immutability
from scripts.diagnostics.active_runtime_audit import audit as audit_active_runtime
from scripts.diagnostics.configured_runtime_audit import audit as audit_configured_runtime
from scripts.diagnostics.legacy_retirement_audit import audit as audit_legacy_retirement
from scripts.upgrade.evidence_manifest import EvidenceManifestV2
from app.inheritance.toolchain import parser_toolchain_preflight


CAPABILITIES = [
    ("DB CRUD", "app/db/repositories/*",
     ["app/bootstrap.py", "app/api/application.py"], "app/db/repositories/*", "root compatibility shims", "wired"),
    ("Analysis", "app/services/analysis_service.py",
     ["app/api/application.py"], "app/services/analysis_service.py", "root compatibility shims", "wired"),
    ("Inheritance Review", "app/services/inheritance_review_service.py",
     ["app/api/application.py", "app/inheritance/rejections.py"],
     "app/services/inheritance_review_service.py", "root compatibility shims", "wired"),
    ("Line Index", "app/db/repositories/line_index_repository.py",
     ["app/services/project_service.py", "app/services/analysis_service.py"], "app/db/repositories/line_index_repository.py", "root compatibility shims", "wired"),
    ("Project State", "app/db/repositories/project_state_repository.py",
     ["app/services/project_service.py", "app/services/progress_service.py"], "app/db/repositories/project_state_repository.py", "root compatibility shims", "wired"),
    ("Progress", "app/services/progress_service.py",
     ["app/api/application.py", "app/bootstrap.py"], "app/services/progress_service.py", "root compatibility shims", "wired"),
    ("Background Job", "app/jobs/service.py",
     ["app/bootstrap.py", "app/api/application.py"], "app/jobs/service.py + app/db/repositories/job_repository.py", "root compatibility shims", "wired"),
    ("Export", "app/services/export_service.py",
     ["app/api/application.py", "app/bootstrap.py"], "app/services/export_service.py", "root compatibility shims", "wired"),
    ("Code Detail", "app/code_detail/*",
     ["app/bootstrap.py", "app/api/application.py"], "app/code_detail/vnext_service.py", "root compatibility shims", "wired"),
    ("Sidecar", "app/code_detail/sidecar_store.py",
     ["app/code_detail/vnext_service.py"], "app/code_detail/sidecar_store.py", "root compatibility shims", "wired"),
    ("Report Registry", "app/reports/registry.py",
     ["app/bootstrap.py", "app/inject/service.py", "app/code_detail/vnext_service.py"], "app/reports/registry.py", "root compatibility shims", "wired"),
    ("Inject", "app/inject/service.py",
     ["app/bootstrap.py", "app/api/application.py"], "app/inject/service.py", "root compatibility shims", "wired"),
    ("Incremental", "app/services/incremental_service.py",
     ["app/bootstrap.py", "app/api/application.py"], "app/services/incremental_service.py + app/incremental/orchestrator.py", "coverage_check.py CLI shim", "wired"),
    ("Git Diff/Blame", "app/incremental/git_diff.py + app/incremental/blame.py",
     ["app/incremental/orchestrator.py"], "app/incremental/git_diff.py + blame.py", "coverage_check.py CLI shim", "wired"),
    ("Path Mapping", "app/incremental/path_index.py",
     ["app/incremental/orchestrator.py"], "app/incremental/path_index.py", "coverage_check.py CLI shim", "wired"),
    ("Release Identity", "app/release_identity.py",
     ["app/bootstrap.py", "app/api/application.py"], "app/release_identity.py", "enhance_coverage.py CLI shim", "wired"),
    ("Upgrade Write Freeze", "app/api/auth.py + app/upgrade/lifecycle.py",
     ["app/api/application.py"], "app/api/auth.py", "enhance_coverage.py CLI shim", "wired"),
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
        "contract_version": CONTRACT_VERSION,
        "revision": revision,
        "release_identity": identity,
        "schema_version": 1,
        "asset_pairs": asset_pairs,
        "capabilities": matrix,
        "canonical_ownership": audit_canonical_ownership(repo_root),
        "runtime_participation": audit_participation(),
        "runtime_legacy_dependency": audit_legacy(repo_root),
        "legacy_retirement": audit_legacy_retirement(repo_root),
        "frontend_vnext_api_contract": audit_frontend_contract(repo_root),
        "scan_immutability": audit_scan_immutability(),
        "configured_runtime": audit_configured_runtime(repo_root),
        # A checkout has no live process by definition; keep this separate
        # from the configuration evidence so it cannot be read as proof that
        # a service is running.
        "active_runtime": audit_active_runtime(repo_root),
        "parser_toolchain": parser_toolchain_preflight(),
    }


def main(argv=None):
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    output = (argv or sys.argv[1:] or ["artifacts/vnext/architecture_ownership_matrix.json"])[0]
    if not os.path.isabs(output):
        output = os.path.join(root, output)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    matrix = build(root)
    # Keep the architecture matrix and its Gate evidence manifest together so
    # CI artifacts remain self-describing after the runner is gone.  A
    # transitional legacy owner is recorded as INCOMPLETE, never relabelled as
    # a production PASS.
    with open(output, "w", encoding="utf-8") as stream:
        json.dump(matrix, stream, ensure_ascii=False, indent=2, sort_keys=True)
    legacy_status = (matrix.get("legacy_retirement") or {}).get("gate_status")
    evidence_status = "PASSED" if legacy_status == "PASSED" else "INCOMPLETE"
    manifest = EvidenceManifestV2(
        root, "gate-1-3", candidate_revision=matrix.get("revision") or "",
        release_identity=matrix.get("release_identity") or {},
        manifest_path=os.path.join(
            os.path.dirname(output), "evidence-manifest-v2.json"
        ),
    )
    manifest.record(
        "architecture-ownership-matrix", "static", evidence_status,
        command_or_action="python scripts/diagnostics/build_vnext_evidence.py",
        exit_code=0, artifact_path=output,
        source_inputs_sha256=[], synthetic=False,
        legacy_retirement_status=legacy_status or "UNKNOWN",
    )
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
