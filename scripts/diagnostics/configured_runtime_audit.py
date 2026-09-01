"""Audit configuration selection without claiming a live process is active."""

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.config.runtime_config import load_application_config
from app.release_identity import is_valid_commit_sha

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
    trusted_workflow_identity = str(
        upgrade.get("trusted_build_workflow_identity") or ""
    ).strip()
    trusted_workflow_sha = str(
        upgrade.get("trusted_build_workflow_sha") or ""
    ).strip()
    if not trusted_workflow_identity:
        violations.append("Candidate trusted_build_workflow_identity is missing")
    if not trusted_workflow_sha:
        violations.append("Candidate trusted_build_workflow_sha is missing")
    elif "REPLACE_WITH" in trusted_workflow_sha.upper():
        violations.append("Candidate trusted_build_workflow_sha is still a placeholder")
    elif not is_valid_commit_sha(trusted_workflow_sha):
        violations.append(
            "Candidate trusted_build_workflow_sha must be an exact commit SHA"
        )
    if not upgrade.get("previous_release_endpoint"):
        violations.append("Candidate previous_release_endpoint is missing")
    elif upgrade.get("previous_release_endpoint") == upgrade.get("release_endpoint"):
        violations.append("Candidate previous_release_endpoint must differ from release_endpoint")
    if not upgrade.get("health_endpoint"):
        violations.append("Candidate health_endpoint is missing")
    if not upgrade.get("served_root_probe_url"):
        violations.append("Candidate served_root_probe_url is missing")
    if not upgrade.get("served_root_url_prefix"):
        violations.append("Candidate served_root_url_prefix is missing")
    elif not str(upgrade.get("served_root_url_prefix")).startswith("/"):
        violations.append("Candidate served_root_url_prefix must start with '/'")
    if not upgrade.get("served_root_probe_relative_path"):
        violations.append("Candidate served_root_probe_relative_path is missing")
    if not upgrade.get("candidate_browser_url"):
        violations.append("Candidate candidate_browser_url is missing")
    if not upgrade.get("candidate_browser_evidence_path"):
        violations.append("Candidate candidate_browser_evidence_path is missing")
    for evidence_field in ("rollback_evidence_path", "performance_evidence_path"):
        if not upgrade.get(evidence_field):
            violations.append("Candidate {} is missing".format(evidence_field))
    commands = upgrade.get("commands") or {}
    for command_name in (
            "stop_current_api", "stop_validation_api", "start_validation_api",
            "start_serving_api", "stop_serving_api", "start_previous_api"):
        if not commands.get(command_name):
            violations.append("{} must be configured as an independent lifecycle command".format(command_name))
    if commands.get("stop_current_api") == commands.get("stop_validation_api"):
        violations.append("stop_current_api must be distinct from stop_validation_api")
    if commands.get("start_validation_api") == commands.get("start_serving_api"):
        violations.append("start_validation_api must be distinct from start_serving_api")
    if commands.get("stop_validation_api") == commands.get("stop_serving_api"):
        violations.append("stop_validation_api must be distinct from stop_serving_api")
    if commands.get("start_previous_api") == commands.get("start_validation_api"):
        violations.append("start_previous_api must not reuse the validation start command")
    if commands.get("start_serving_api") == commands.get("start_validation_api"):
        violations.append("start_serving_api must be a distinct lifecycle command")
    if commands.get("stop_serving_api") == commands.get("stop_validation_api"):
        violations.append("stop_serving_api must be a distinct lifecycle command")
    for field in (
            "validation_candidate_root", "validation_candidate_artifact_manifest",
            "production_candidate_root", "production_candidate_artifact_manifest",
            "production_candidate_build_receipt",
            "production_candidate_attestation_bundle",
            "production_candidate_attestation_repository",
            "production_candidate_attestation_workflow",
            "publish_root",
            "served_root_path",
            "validation_session_manifest", "validation_teardown_evidence_path",
            "serving_session_id", "serving_session_manifest",
            "serving_teardown_evidence_path", "current_serving_state_path"):
        if not upgrade.get(field):
            violations.append("Candidate {} is missing".format(field))
    legacy_fields = (
        "candidate_root", "candidate_artifact_manifest", "candidate_build_receipt",
        "candidate_build_attestation_bundle", "candidate_build_attestation_repository",
        "candidate_build_attestation_workflow",
    )
    for field in legacy_fields:
        if field in upgrade:
            violations.append(
                "legacy upgrade.{} is retired; use explicit validation/production candidate fields".format(
                    field
                )
            )
    validation_candidate_root = str(upgrade.get("validation_candidate_root") or "")
    validation_candidate_manifest = str(
        upgrade.get("validation_candidate_artifact_manifest") or ""
    )
    production_candidate_root = str(upgrade.get("production_candidate_root") or "")
    production_candidate_manifest = str(
        upgrade.get("production_candidate_artifact_manifest") or ""
    )
    production_candidate_build_receipt = str(
        upgrade.get("production_candidate_build_receipt") or ""
    )
    production_candidate_attestation_bundle = str(
        upgrade.get("production_candidate_attestation_bundle") or ""
    )
    production_candidate_attestation_repository = str(
        upgrade.get("production_candidate_attestation_repository") or ""
    )
    production_candidate_attestation_workflow = str(
        upgrade.get("production_candidate_attestation_workflow") or ""
    )
    publish_root = str(upgrade.get("publish_root") or "")
    served_root_path = str(upgrade.get("served_root_path") or "")
    if validation_candidate_root and not os.path.isabs(validation_candidate_root):
        validation_candidate_root = os.path.join(repo_root, validation_candidate_root)
    if production_candidate_root and not os.path.isabs(production_candidate_root):
        production_candidate_root = os.path.join(repo_root, production_candidate_root)
    if publish_root and not os.path.isabs(publish_root):
        publish_root = os.path.join(repo_root, publish_root)
    if validation_candidate_manifest and not os.path.isabs(validation_candidate_manifest):
        validation_candidate_manifest = os.path.join(repo_root, validation_candidate_manifest)
    if production_candidate_manifest and not os.path.isabs(production_candidate_manifest):
        production_candidate_manifest = os.path.join(repo_root, production_candidate_manifest)
    if production_candidate_build_receipt and not os.path.isabs(production_candidate_build_receipt):
        production_candidate_build_receipt = os.path.join(repo_root, production_candidate_build_receipt)
    if validation_candidate_root and production_candidate_root and \
            os.path.realpath(validation_candidate_root) == os.path.realpath(production_candidate_root):
        violations.append("validation and production Candidate roots must be separate")
    if production_candidate_root and publish_root and (
            os.path.realpath(production_candidate_root) == os.path.realpath(publish_root) or
            os.path.commonpath((os.path.realpath(production_candidate_root), os.path.realpath(publish_root))) in (
                os.path.realpath(production_candidate_root), os.path.realpath(publish_root)
            )
    ):
        violations.append("production_candidate_root and publish_root must be separate")
    for label, manifest_path, owner in (
            ("validation_candidate_artifact_manifest", validation_candidate_manifest,
             validation_candidate_root),
            ("production_candidate_artifact_manifest", production_candidate_manifest,
             production_candidate_root),
            ("production_candidate_build_receipt", production_candidate_build_receipt,
             production_candidate_root)):
        if manifest_path and owner:
            try:
                if os.path.commonpath((os.path.realpath(manifest_path), os.path.realpath(owner))) != os.path.realpath(owner):
                    violations.append("{} must be inside its Candidate root".format(label))
            except ValueError:
                violations.append("{} must be inside its Candidate root".format(label))
    if served_root_path and not os.path.isabs(served_root_path):
        served_root_path = os.path.join(repo_root, served_root_path)
    expected_served_root_path = os.path.normpath(os.path.abspath(
        os.path.join(publish_root, "CURRENT", "reports")
    )) if publish_root else ""
    if not served_root_path:
        violations.append("Candidate served_root_path is missing")
    elif os.path.normpath(os.path.abspath(served_root_path)) != expected_served_root_path:
        violations.append("Candidate served_root_path must be publish_root/CURRENT/reports")
    session_manifest = str(upgrade.get("validation_session_manifest") or "")
    teardown_evidence = str(upgrade.get("validation_teardown_evidence_path") or "")
    if session_manifest and "{attempt_id}" not in session_manifest:
        violations.append("validation_session_manifest must be attempt-scoped with {attempt_id}")
    if teardown_evidence and "{attempt_id}" not in teardown_evidence:
        violations.append("validation_teardown_evidence_path must be attempt-scoped with {attempt_id}")
    for field in (
            "candidate_browser_evidence_path", "rollback_evidence_path",
            "performance_evidence_path"):
        if upgrade.get(field) and "{attempt_id}" not in str(upgrade.get(field)):
            violations.append("{} must be attempt-scoped with {{attempt_id}}".format(field))
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
        "served_root_probe_url": upgrade.get("served_root_probe_url", ""),
        "served_root_url_prefix": upgrade.get("served_root_url_prefix", ""),
        "served_root_probe_relative_path": upgrade.get(
            "served_root_probe_relative_path", ""
        ),
        "candidate_previous_release_endpoint": upgrade.get("previous_release_endpoint", ""),
        "trusted_build_workflow_identity": trusted_workflow_identity,
        "trusted_build_workflow_sha": trusted_workflow_sha,
        "validation_candidate_root": validation_candidate_root,
        "validation_candidate_artifact_manifest": validation_candidate_manifest,
        "production_candidate_root": production_candidate_root,
        "production_candidate_artifact_manifest": production_candidate_manifest,
        "production_candidate_build_receipt": production_candidate_build_receipt,
        "production_candidate_attestation_bundle": production_candidate_attestation_bundle,
        "production_candidate_attestation_repository": production_candidate_attestation_repository,
        "production_candidate_attestation_workflow": production_candidate_attestation_workflow,
        "candidate_publish_root": publish_root,
        "served_root_path": served_root_path,
        "validation_session_manifest": upgrade.get("validation_session_manifest", ""),
        "validation_teardown_evidence_path": upgrade.get("validation_teardown_evidence_path", ""),
        "attempt_scoped_validation_paths": bool(
            "{attempt_id}" in session_manifest and
            "{attempt_id}" in teardown_evidence
        ),
        "validation_ports": list(validation_ports),
        "current_stop_command_distinct": commands.get("stop_current_api") != commands.get("stop_validation_api"),
        "validation_serving_commands_distinct": (
            commands.get("start_validation_api") != commands.get("start_serving_api") and
            commands.get("stop_validation_api") != commands.get("stop_serving_api")
        ),
        "rollback_command_distinct": commands.get("start_previous_api") != commands.get("start_validation_api"),
        "violations": violations,
    })


if __name__ == "__main__":
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["status"] == "PASSED" else 1)
