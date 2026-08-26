"""Run the repository-local exact-SHA source review for Gate F.

This audit is deliberately limited to the checked-out source tree.  It proves
that the source/canonical/runtime contracts are clean for one exact commit;
it does not claim that a production process, database, parser host, or proxy
has been validated.
"""

from __future__ import print_function

import argparse
import json
import os
import platform
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.release_identity import generate_release_identity
from app.time_utils import utc_iso
from scripts.diagnostics.canonical_ownership_audit import audit_canonical_ownership
from scripts.diagnostics.configured_runtime_audit import audit as audit_configured_runtime
from scripts.diagnostics.contract_artifact_audit import audit as audit_contract_artifacts
from scripts.diagnostics.contract import with_contract
from scripts.diagnostics.frontend_vnext_api_contract_audit import audit as audit_frontend
from scripts.diagnostics.job_lifecycle_audit import audit as audit_jobs
from scripts.diagnostics.runtime_legacy_dependency_audit import audit as audit_legacy
from scripts.diagnostics.runtime_participation_audit import audit as audit_participation
from scripts.diagnostics.scan_immutability_audit import audit as audit_immutability
from scripts.diagnostics.task_manifest_audit import audit as audit_tasks
from scripts.diagnostics.connection_pool_audit import audit as audit_pool


def _git(repo_root, *arguments):
    return subprocess.check_output(
        ["git", "-C", repo_root] + list(arguments),
        stderr=subprocess.STDOUT,
    ).decode("utf-8", "replace").strip()


def _run_check(name, callback):
    try:
        result = callback() or {}
        status = str(result.get("status") or "INCOMPLETE").upper()
        return {"name": name, "status": status, "result": result}
    except Exception as exc:  # pragma: no cover - defensive audit boundary
        return {
            "name": name,
            "status": "FAILED",
            "result": {
                "status": "FAILED",
                "violations": ["{}: {}".format(type(exc).__name__, exc)],
            },
        }


def _audit_release_identity(repo_root):
    identity = generate_release_identity(repo_root=repo_root)
    return {"status": "PASSED", "identity": identity}


def audit(repo_root=ROOT):
    repo_root = os.path.abspath(repo_root)
    started_at = utc_iso()
    violations = []
    try:
        revision = _git(repo_root, "rev-parse", "HEAD")
        dirty = _git(repo_root, "status", "--porcelain", "--untracked-files=all")
    except (OSError, subprocess.CalledProcessError, UnicodeError) as exc:
        revision = ""
        dirty = "unavailable"
        violations.append("cannot inspect exact checkout: {}".format(exc))

    checks = [
        _run_check("canonical_ownership", lambda: audit_canonical_ownership(repo_root)),
        _run_check("runtime_participation", audit_participation),
        _run_check("legacy_dependency_boundary", lambda: audit_legacy(repo_root)),
        _run_check("frontend_api_contract", lambda: audit_frontend(repo_root)),
        _run_check("scan_immutability", audit_immutability),
        _run_check("configured_runtime", lambda: audit_configured_runtime(repo_root)),
        _run_check("contract_artifacts", lambda: audit_contract_artifacts(repo_root)),
        _run_check("task_manifest", lambda: audit_tasks(repo_root)),
        _run_check("connection_pool", audit_pool),
        _run_check("job_lifecycle", audit_jobs),
        _run_check("release_identity", lambda: _audit_release_identity(repo_root)),
    ]
    if not revision:
        violations.append("candidate revision is unavailable")
    if dirty:
        violations.append("exact source review requires a clean worktree")
    for check in checks:
        if check["status"] != "PASSED":
            violations.append("{} audit is {}".format(check["name"], check["status"]))

    release_check = next(
        (item for item in checks if item["name"] == "release_identity"),
        {"result": {}},
    )
    identity = (release_check.get("result") or {}).get("identity") or {}
    if identity and revision and identity.get("commit_sha") != revision:
        violations.append("release identity commit does not match HEAD")
    result = with_contract({
        "status": "PASSED" if not violations else "INCOMPLETE",
        "evidence_class": "exact_sha_source_review",
        "synthetic": False,
        "candidate_revision": revision,
        "release_identity": identity,
        "host_identity": {
            "hostname": platform.node(),
            "platform": platform.platform(),
        },
        "started_at": started_at,
        "finished_at": utc_iso(),
        "command_or_action": "python scripts/diagnostics/final_source_review.py",
        "exit_code": 0 if not violations else 1,
        "scope": "repository source/canonical/runtime contracts only",
        "external_validation_required": [
            "production process and service identity",
            "target database/runtime fingerprint",
            "traffic-closed read-only verification",
        ],
        "worktree_clean": not bool(dirty),
        "checks": checks,
        "violations": violations,
    })
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=ROOT)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    result = audit(args.repo_root)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = os.path.abspath(args.output)
        directory = os.path.dirname(output)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(output, "w", encoding="utf-8") as stream:
            stream.write(encoded)
    print(encoded)
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
