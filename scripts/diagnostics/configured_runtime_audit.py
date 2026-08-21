"""Audit configuration selection without claiming a live process is active."""

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.config.runtime_config import load_application_config

try:
    from scripts.diagnostics.contract import with_contract
except ModuleNotFoundError:
    from contract import with_contract


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
    upgrade = candidate.get("upgrade") or {}
    if not upgrade.get("previous_release_endpoint"):
        violations.append("Candidate previous_release_endpoint is missing")
    elif upgrade.get("previous_release_endpoint") == upgrade.get("release_endpoint"):
        violations.append("Candidate previous_release_endpoint must differ from release_endpoint")
    commands = upgrade.get("commands") or {}
    if commands.get("start_previous_api") == commands.get("start_api"):
        violations.append("start_previous_api must not reuse the Candidate start command")
    return with_contract({
        "status": "PASSED" if not violations else "FAILED",
        "evidence_class": "configuration_audit",
        "default_runtime_mode": default.get("runtime_mode"),
        "default_server": default.get("server") or {},
        "candidate_config_path": os.path.relpath(candidate_path, repo_root),
        "candidate_runtime_mode": candidate.get("runtime_mode"),
        "candidate_database": (candidate.get("mysql") or {}).get("database"),
        "candidate_port": (candidate.get("server") or {}).get("port"),
        "candidate_release_endpoint": upgrade.get("release_endpoint", ""),
        "candidate_previous_release_endpoint": upgrade.get("previous_release_endpoint", ""),
        "rollback_command_distinct": commands.get("start_previous_api") != commands.get("start_api"),
        "violations": violations,
    })


if __name__ == "__main__":
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["status"] == "PASSED" else 1)
