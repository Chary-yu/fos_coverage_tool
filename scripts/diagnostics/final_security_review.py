"""Run the repository-local exact-SHA security/trust-boundary review."""

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

from app.incremental.path_index import LCOVPathLookupIndex
from app.release_identity import generate_release_identity
from app.time_utils import utc_iso
from scripts.diagnostics.contract import with_contract
from scripts.diagnostics.security_scanner import scan_directory


def _revision(repo_root):
    try:
        return subprocess.check_output(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            stderr=subprocess.STDOUT,
        ).decode("utf-8", "replace").strip()
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return ""


def _path_boundary_check():
    index = LCOVPathLookupIndex({"repo-a": ["src/a.c", "src/lib/a.c"]})
    checks = {}
    resolved, classification = index.resolve_path("repo-a", "src/a.c")
    checks["exact_lcov_path"] = resolved == "src/a.c" and classification == "exact"
    resolved, classification = index.resolve_path("repo-a", "../src/a.c")
    checks["parent_traversal_fail_closed"] = resolved is None and classification == "invalid_path"
    resolved, classification = index.resolve_path("repo-a", "a.c")
    checks["basename_ambiguity_fail_closed"] = (
        resolved is None and classification == "basename_only_rejected"
    )
    return checks


def audit(repo_root=ROOT):
    repo_root = os.path.abspath(repo_root)
    started_at = utc_iso()
    scanner = scan_directory(repo_root)
    path_checks = _path_boundary_check()
    violations = []
    if not scanner.get("is_safe"):
        violations.append("static security scanner reported high/critical findings")
    violations.extend(
        "path boundary check failed: {}".format(name)
        for name, passed in path_checks.items() if not passed
    )
    revision = _revision(repo_root)
    if not revision:
        violations.append("candidate revision is unavailable")
    return with_contract({
        "status": "PASSED" if not violations else "INCOMPLETE",
        "evidence_class": "exact_sha_security_review",
        "synthetic": False,
        "candidate_revision": revision,
        "release_identity": generate_release_identity(repo_root=repo_root),
        "host_identity": {
            "hostname": platform.node(),
            "platform": platform.platform(),
        },
        "started_at": started_at,
        "finished_at": utc_iso(),
        "command_or_action": "python scripts/diagnostics/final_security_review.py",
        "exit_code": 0 if not violations else 1,
        "checks": {
            "static_scanner": scanner,
            "path_boundary": path_checks,
        },
        "scope": "repository static trust-boundary checks only",
        "external_validation_required": [
            "target host filesystem and service policy",
            "reverse-proxy/auth trust configuration",
            "production credentials and database identity",
        ],
        "violations": violations,
    })


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
