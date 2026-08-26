"""Fail-closed static contract gate for the canonical Code Detail frontend."""

import json
import os
import re
import subprocess
import sys

try:
    from scripts.diagnostics.contract import with_contract
except ModuleNotFoundError:
    from contract import with_contract


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
ASSET = os.path.join(ROOT, "web/assets/js/coverage_enhance.js")
PROGRESS_ASSET = os.path.join(ROOT, "web/assets/js/coverage_progress.js")


def audit(repo_root=ROOT):
    path = os.path.join(repo_root, "web/assets/js/coverage_enhance.js")
    progress_path = os.path.join(repo_root, "web/assets/js/coverage_progress.js")
    violations = []
    if not os.path.isfile(path):
        return with_contract({"status": "FAILED", "evidence_class": "contract_audit",
                "violations": ["canonical Code Detail asset is missing"]})
    if not os.path.isfile(progress_path):
        return with_contract({"status": "FAILED", "evidence_class": "contract_audit",
                "violations": ["canonical Progress asset is missing"]})
    with open(path, "r", encoding="utf-8") as stream:
        text = stream.read()
    with open(progress_path, "r", encoding="utf-8") as stream:
        progress_text = stream.read()

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
    progress_required = {
        "details_endpoint": r"/progress/details",
        "top_level_details_renderer": r"renderDetailTable\(payload \|\| \{\}",
    }
    progress_missing = [
        name for name, pattern in progress_required.items()
        if not re.search(pattern, progress_text)
    ]
    violations.extend(
        "missing canonical Progress contract: {}".format(item)
        for item in progress_missing
    )
    if re.search(r"payload\.data", progress_text):
        violations.append(
            "canonical Progress asset must not unwrap the legacy payload.data envelope"
        )
    try:
        subprocess.check_call(["node", "--check", path], cwd=repo_root,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        syntax = "PASSED"
    except (OSError, subprocess.CalledProcessError) as exc:
        syntax = "FAILED"
        violations.append("canonical JavaScript syntax check failed: {}".format(exc))
    try:
        subprocess.check_call(["node", "--check", progress_path], cwd=repo_root,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        progress_syntax = "PASSED"
    except (OSError, subprocess.CalledProcessError) as exc:
        progress_syntax = "FAILED"
        violations.append("canonical Progress JavaScript syntax check failed: {}".format(exc))
    return with_contract({
        "status": "PASSED" if not violations else "FAILED",
        "evidence_class": "contract_audit",
        "assets": [os.path.relpath(path, repo_root),
                   os.path.relpath(progress_path, repo_root)],
        "syntax": syntax,
        "progress_syntax": progress_syntax,
        "violations": violations,
    })


if __name__ == "__main__":
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["status"] == "PASSED" else 1)
