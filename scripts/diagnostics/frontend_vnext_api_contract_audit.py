"""Fail-closed static contract gate for the canonical Code Detail frontend."""

import json
import os
import re
import subprocess
import sys


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
ASSET = os.path.join(ROOT, "web/assets/js/coverage_enhance.js")


def audit(repo_root=ROOT):
    path = os.path.join(repo_root, "web/assets/js/coverage_enhance.js")
    violations = []
    if not os.path.isfile(path):
        return {"status": "FAILED", "evidence_class": "contract_audit",
                "violations": ["canonical Code Detail asset is missing"]}
    with open(path, "r", encoding="utf-8") as stream:
        text = stream.read()

    required = {
        "identity_query": r"function\s+codeDetailIdentity",
        "layout_endpoint": r"/code-layout",
        "batch_endpoint": r"/code-lines/batch",
        "chunk_endpoint": r"/code-lines\?",
        "analysis_endpoint": r"requestCoverageApi\('/analysis'",
        "progress_details_endpoint": r"/progress/details",
        "scan_identity": r"scan_id:\s*currentScanId",
        "repository_identity": r"repository_name:\s*currentRepositoryName",
        "range_save": r"line_start:\s*panel\.block\.startLine",
    }
    missing = [name for name, pattern in required.items()
               if not re.search(pattern, text)]
    violations.extend("missing canonical contract: {}".format(item) for item in missing)
    forbidden = {
        "legacy_batch_save": r"requestCoverageApi\(['\"]/(?:batch|analysis/batch)",
        "legacy_root_records": r"requestCoverageApi\(`\?",
        "dead_api_override": r"EXPLICIT_API_URL|URL_PARAMS\.get\(['\"]api",
        "legacy_progress_start": r"/progress/start",
    }
    violations.extend("forbidden legacy contract remains: {}".format(name)
                      for name, pattern in forbidden.items()
                      if re.search(pattern, text))
    try:
        subprocess.check_call(["node", "--check", path], cwd=repo_root,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        syntax = "PASSED"
    except (OSError, subprocess.CalledProcessError) as exc:
        syntax = "FAILED"
        violations.append("canonical JavaScript syntax check failed: {}".format(exc))
    return {
        "status": "PASSED" if not violations else "FAILED",
        "evidence_class": "contract_audit",
        "asset": os.path.relpath(path, repo_root),
        "syntax": syntax,
        "violations": violations,
    }


if __name__ == "__main__":
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["status"] == "PASSED" else 1)
