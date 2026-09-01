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
    if not upgrade.get("health_endpoint"):
        violations.append("Candidate health_endpoint is missing")
    if not upgrade.get("candidate_browser_url"):
        violations.append("Candidate candidate_browser_url is missing")
    if not upgrade.get("candidate_browser_evidence_path"):
        violations.append("Candidate candidate_browser_evidence_path is missing")
    commands = upgrade.get("commands") or {}
    if commands.get("start_previous_api") == commands.get("start_api"):
        violations.append("start_previous_api must not reuse the Candidate start command")
    for command_name in ("start_serving_api", "stop_serving_api"):
        if not commands.get(command_name):
            violations.append("{} must be configured for the final serving process".format(command_name))
    if commands.get("start_serving_api") == commands.get("start_api"):
        violations.append("start_serving_api must be a distinct lifecycle command")
    if commands.get("stop_serving_api") == commands.get("stop_api"):
        violations.append("stop_serving_api must be a distinct lifecycle command")
    for field in (
            "candidate_root", "candidate_artifact_manifest", "publish_root", "validation_session_manifest",
            "validation_teardown_evidence_path"):
        if not upgrade.get(field):
            violations.append("Candidate {} is missing".format(field))
    candidate_root = str(upgrade.get("candidate_root") or "")
    candidate_artifact_manifest = str(upgrade.get("candidate_artifact_manifest") or "")
    publish_root = str(upgrade.get("publish_root") or "")
    if candidate_root and not os.path.isabs(candidate_root):
        candidate_root = os.path.join(repo_root, candidate_root)
    if publish_root and not os.path.isabs(publish_root):
        publish_root = os.path.join(repo_root, publish_root)
    if candidate_root and publish_root and os.path.realpath(candidate_root) == os.path.realpath(publish_root):
        violations.append("Candidate publish_root must be separate from candidate_root")
    if candidate_artifact_manifest and not os.path.isabs(candidate_artifact_manifest):
        candidate_artifact_manifest = os.path.join(repo_root, candidate_artifact_manifest)
    if candidate_artifact_manifest and candidate_root:
        if os.path.commonpath((os.path.realpath(candidate_artifact_manifest), os.path.realpath(candidate_root))) != os.path.realpath(candidate_root):
            violations.append("candidate_artifact_manifest must be inside candidate_root")
    session_manifest = str(upgrade.get("validation_session_manifest") or "")
    teardown_evidence = str(upgrade.get("validation_teardown_evidence_path") or "")
    if session_manifest and teardown_evidence:
        session_path = session_manifest if os.path.isabs(session_manifest) else os.path.join(repo_root, session_manifest)
        teardown_path = teardown_evidence if os.path.isabs(teardown_evidence) else os.path.join(repo_root, teardown_evidence)
        if os.path.realpath(session_path) == os.path.realpath(teardown_path):
            violations.append("validation teardown evidence must not overwrite the session manifest")
    if publish_root:
        try:
            if os.path.commonpath((os.path.realpath(publish_root), os.path.realpath(repo_root))) == os.path.realpath(repo_root):
                violations.append("Candidate publish_root must be outside the active repository")
        except ValueError:
            pass
    validation_ports = upgrade.get("validation_ports") or []
    if isinstance(validation_ports, (str, int)):
        validation_ports = [validation_ports]
    if not validation_ports:
        violations.append("Candidate validation_ports must identify an owned port")
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
        "candidate_health_endpoint": upgrade.get("health_endpoint", ""),
        "candidate_previous_release_endpoint": upgrade.get("previous_release_endpoint", ""),
        "candidate_root": candidate_root,
        "candidate_artifact_manifest": candidate_artifact_manifest,
        "candidate_publish_root": publish_root,
        "validation_session_manifest": upgrade.get("validation_session_manifest", ""),
        "validation_teardown_evidence_path": upgrade.get("validation_teardown_evidence_path", ""),
        "validation_ports": list(validation_ports),
        "rollback_command_distinct": commands.get("start_previous_api") != commands.get("start_api"),
        "violations": violations,
    })


if __name__ == "__main__":
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["status"] == "PASSED" else 1)
