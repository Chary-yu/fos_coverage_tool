"""Verify that default and Candidate configuration select VNext."""

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.config.runtime_config import load_application_config


def audit(repo_root=ROOT):
    violations = []
    default = load_application_config(None, base_dir=repo_root)
    if str(default.get("runtime_mode") or "").lower() != "vnext":
        violations.append("default configuration does not select runtime_mode=vnext")
    candidate_path = os.path.join(repo_root, "config/coverage_config.staging.example.json")
    candidate = load_application_config(candidate_path, base_dir=repo_root)
    if str(candidate.get("runtime_mode") or "").lower() != "vnext":
        violations.append("Candidate configuration does not select runtime_mode=vnext")
    if (candidate.get("mysql") or {}).get("database") != "coverage_candidate":
        violations.append("Candidate database is not coverage_candidate")
    if int((candidate.get("server") or {}).get("port") or 0) != 19528:
        violations.append("Candidate server port is not 19528")
    return {"status": "PASSED" if not violations else "FAILED",
            "evidence_class": "configuration_audit",
            "default_runtime_mode": default.get("runtime_mode"),
            "candidate_runtime_mode": candidate.get("runtime_mode"),
            "candidate_database": (candidate.get("mysql") or {}).get("database"),
            "candidate_port": (candidate.get("server") or {}).get("port"),
            "violations": violations}


if __name__ == "__main__":
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["status"] == "PASSED" else 1)
