"""Build the machine-readable Gate A--F status without synthetic PASS claims.

This command is intentionally an evidence assembler, not a release certifier.
It runs repository-local audits and records the exact external evidence still
required for MariaDB/production/cutover gates.  A missing artifact therefore
produces ``INCOMPLETE`` or ``BLOCKED`` and a non-zero exit code.
"""

from __future__ import absolute_import

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.inheritance.toolchain import parser_toolchain_preflight
from app.release_identity import generate_release_identity
from app.time_utils import utc_iso
from scripts.diagnostics.canonical_ownership_audit import audit_canonical_ownership as audit_canonical
from scripts.diagnostics.contract_artifact_audit import audit as audit_contract_artifacts
from scripts.diagnostics.frontend_vnext_api_contract_audit import audit as audit_frontend
from scripts.diagnostics.inheritance_rules_audit import audit as audit_rules
from scripts.diagnostics.legacy_retirement_audit import audit as audit_legacy_retirement
from scripts.diagnostics.runtime_legacy_dependency_audit import audit as audit_legacy
from scripts.diagnostics.runtime_participation_audit import audit as audit_participation
from scripts.diagnostics.scan_immutability_audit import audit as audit_scan
from scripts.diagnostics.active_runtime_audit import audit as audit_active
from scripts.diagnostics.configured_runtime_audit import audit as audit_configured
from scripts.diagnostics.performance_evidence_audit import audit as audit_performance
from scripts.upgrade.evidence_manifest import EvidenceManifestV2
from scripts.upgrade.schema_preflight import validate_ddl_file


GATES = ("A", "B", "C", "D", "E", "F")


def _revision(repo_root):
    try:
        return subprocess.check_output(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode("ascii").strip()
    except Exception:
        return ""


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _status(*results):
    values = [str(item or "INCOMPLETE").upper() for item in results]
    if "FAILED" in values:
        return "FAILED"
    if "BLOCKED" in values:
        return "BLOCKED"
    if any(item in values for item in ("INCOMPLETE", "PARTIAL", "UNAVAILABLE", "SKIPPED")):
        return "INCOMPLETE"
    return "PASSED"


def _local_check(name, result):
    result = result or {}
    return {
        "name": name,
        "status": str(result.get("status") or "INCOMPLETE"),
        "evidence_class": result.get("evidence_class", "repository_audit"),
        "violations": result.get("violations") or result.get("errors") or [],
        "summary": result,
    }


_KNOWN_STATUSES = {
    "PASSED", "INCOMPLETE", "BLOCKED", "FAILED", "PARTIAL",
    "UNAVAILABLE", "SKIPPED",
}


def _external(name, requirement, env_name=None, candidate_revision="",
              repo_root=ROOT, expected_gate=""):
    value = os.environ.get(env_name or "") if env_name else ""
    artifact_path = os.path.abspath(value) if value else ""
    artifact_exists = bool(artifact_path and os.path.isfile(artifact_path))
    violations = []
    observed_status = "INCOMPLETE"
    evidence_payload = {}
    if not value:
        violations.append("{} is not configured".format(env_name or "evidence path"))
    elif not artifact_exists:
        violations.append("external evidence artifact does not exist: {}".format(
            artifact_path
        ))
    else:
        try:
            with open(artifact_path, "r", encoding="utf-8") as stream:
                evidence_payload = json.load(stream)
        except (OSError, ValueError, TypeError):
            violations.append("external evidence must be JSON: {}".format(
                artifact_path
            ))
        if not isinstance(evidence_payload, dict):
            violations.append("external evidence JSON must be an object")
            evidence_payload = {}
        if evidence_payload.get("evidence_schema_version") == 2:
            evidence_gate = str(evidence_payload.get("gate") or "")
            if not evidence_gate:
                violations.append("Evidence Manifest v2 gate is missing")
            elif expected_gate and evidence_gate != str(expected_gate):
                violations.append("external evidence gate does not match the required gate")
            else:
                manifest = EvidenceManifestV2(
                    repo_root, evidence_gate, manifest_path=artifact_path
                )
                valid, errors = manifest.validate()
                if not valid:
                    violations.extend(errors)
                records = evidence_payload.get("evidence") or []
                statuses = [str(item.get("status") or "INCOMPLETE").upper()
                            for item in records if isinstance(item, dict)]
                if statuses and "FAILED" in statuses:
                    observed_status = "FAILED"
                elif statuses and all(item == "PASSED" for item in statuses):
                    observed_status = "PASSED"
                else:
                    observed_status = "INCOMPLETE"
        else:
            observed_status = str(evidence_payload.get("status") or
                                  "INCOMPLETE").upper()
            if observed_status not in _KNOWN_STATUSES:
                violations.append("external evidence status is unknown")
                observed_status = "INCOMPLETE"
            # A flat external artifact is accepted only when it carries the
            # same provenance that Evidence Manifest v2 requires.  In
            # particular, a hand-written ``status=PASSED`` file must not be
            # enough to advance a release gate.
            if not isinstance(evidence_payload.get("host_identity"), dict) or \
                    not evidence_payload.get("host_identity"):
                violations.append("external evidence host_identity is missing")
            if not evidence_payload.get("command_or_action"):
                violations.append("external evidence command_or_action is missing")
            if not evidence_payload.get("evidence_class"):
                violations.append("external evidence evidence_class is missing")
            if expected_gate and evidence_payload.get("gate") != str(expected_gate):
                violations.append("external evidence gate is missing or does not match the required gate")
            if not evidence_payload.get("started_at") or \
                    not evidence_payload.get("finished_at"):
                violations.append("external evidence timestamps are missing")
            exit_code = evidence_payload.get("exit_code")
            if type(exit_code) is not int:
                violations.append("external evidence exit_code is missing or not an integer")
            elif observed_status == "PASSED" and exit_code != 0:
                violations.append("PASSED external evidence must have exit_code=0")
            if evidence_payload.get("synthetic") is not False:
                violations.append("external evidence must explicitly set synthetic=false")
            if observed_status == "PASSED":
                release_identity = evidence_payload.get("release_identity")
                if not isinstance(release_identity, dict) or not release_identity.get("commit_sha"):
                    violations.append("PASSED external evidence release_identity is missing")
                artifact_ref = evidence_payload.get("artifact_path")
                artifact_sha = str(evidence_payload.get("artifact_sha256") or "")
                if not artifact_ref or not artifact_sha:
                    violations.append("PASSED external evidence artifact path/SHA256 is missing")
                else:
                    referenced = str(artifact_ref)
                    if not os.path.isabs(referenced):
                        referenced = os.path.join(os.path.dirname(artifact_path), referenced)
                    referenced = os.path.abspath(referenced)
                    if not os.path.isfile(referenced):
                        violations.append("PASSED external evidence referenced artifact is missing")
                    elif _sha256(referenced) != artifact_sha:
                        violations.append("PASSED external evidence artifact SHA256 does not match")
        observed_candidate = str(evidence_payload.get("candidate_revision") or "")
        if not observed_candidate or observed_candidate != str(candidate_revision or ""):
            violations.append("external evidence candidate revision mismatch")
        release_identity = evidence_payload.get("release_identity") or {}
        if isinstance(release_identity, dict) and release_identity.get("commit_sha") and \
                release_identity.get("commit_sha") != str(candidate_revision or ""):
            violations.append("external evidence release identity mismatch")
    status = observed_status if artifact_exists and not violations else "INCOMPLETE"
    return {
        "name": name,
        "status": status,
        "evidence_class": "external_release_evidence",
        "requirement": requirement,
        "artifact_path": artifact_path,
        "artifact_sha256": _sha256(artifact_path) if artifact_exists else "",
        "violations": violations,
        "observed_status": observed_status,
        "source": env_name or "operator supplied evidence",
    }


def build(repo_root):
    repo_root = os.path.abspath(repo_root)
    revision = _revision(repo_root)
    identity = generate_release_identity(repo_root=repo_root)
    ddl_path = os.path.join(repo_root, "scripts", "upgrade", "vnext_schema.sql")
    ddl_safe, ddl_errors, ddl_warnings = validate_ddl_file(ddl_path)
    domain_ddl_path = os.path.join(
        repo_root, "scripts", "upgrade", "vnext_domain_constraints.sql"
    )
    domain_ddl_safe, domain_ddl_errors, domain_ddl_warnings = validate_ddl_file(
        domain_ddl_path
    )
    canonical = audit_canonical(repo_root)
    contract_artifacts = audit_contract_artifacts(repo_root)
    legacy = audit_legacy(repo_root)
    legacy_retirement = audit_legacy_retirement(repo_root)
    participation = audit_participation()
    scan = audit_scan()
    configured = audit_configured(repo_root)
    active = audit_active(repo_root)
    rules = audit_rules(repo_root)
    frontend = audit_frontend(repo_root)
    parser = parser_toolchain_preflight()
    performance_path = os.environ.get("COVERAGE_PERFORMANCE_EVIDENCE", "")
    performance = audit_performance(performance_path) if performance_path else {
        "status": "INCOMPLETE", "evidence_class": "cross_layer_performance",
        "violations": ["COVERAGE_PERFORMANCE_EVIDENCE is not configured"],
    }

    gates = {
        "A": {
            "local_checks": [
                _local_check("schema_preflight", {
                    "status": "PASSED" if ddl_safe else "FAILED",
                    "errors": ddl_errors, "warnings": ddl_warnings,
                }),
                _local_check("contract_artifacts", contract_artifacts),
            ],
            "external_evidence": [
                _external("verified_backup_restore", "verified production backup restored into an empty target", "COVERAGE_GATE_A_BACKUP_EVIDENCE", revision, repo_root, "gate-a"),
                _external("mariadb_55_rehearsal", "MariaDB 5.5 compatibility rehearsal", "COVERAGE_GATE_A_MARIADB_EVIDENCE", revision, repo_root, "gate-a"),
            ],
        },
        "B": {
            "local_checks": [
                _local_check("canonical_ownership", canonical),
                _local_check("legacy_dependency", legacy),
                _local_check("domain_constraint_preflight", {
                    "status": "PASSED" if domain_ddl_safe else "FAILED",
                    "errors": domain_ddl_errors, "warnings": domain_ddl_warnings,
                }),
            ],
            "external_evidence": [_external(
                "target_backfill_semantic_hash", "target DB backfill/orphan/semantic hash evidence",
                "COVERAGE_GATE_B_DB_EVIDENCE", revision, repo_root, "gate-b",
            )],
        },
        "C": {
            "local_checks": [
                _local_check("runtime_participation", participation),
                _local_check("scan_immutability", scan),
            ],
            "external_evidence": [_external(
                "durable_restart_rehearsal", "durable import restart/fencing/read-set rehearsal",
                "COVERAGE_GATE_C_RESTART_EVIDENCE", revision, repo_root, "gate-c",
            )],
        },
        "D": {
            "local_checks": [
                _local_check("rules_r01_r83", rules),
                _local_check("parser_toolchain", parser),
            ],
            "external_evidence": [_external(
                "deterministic_corpus", "deterministic parser/callee/header corpus with zero false positives",
                "COVERAGE_GATE_D_CORPUS_EVIDENCE", revision, repo_root, "gate-d",
            )],
        },
        "E": {
            "local_checks": [
                _local_check("frontend_api_contract", frontend),
                _local_check("performance_evidence", performance),
            ],
            "external_evidence": [
                _external("real_browser_evidence", "real HTTP + Chromium parity evidence", "COVERAGE_GATE_E_BROWSER_EVIDENCE", revision, repo_root, "gate-e"),
                _external("cross_layer_performance", "DB/sidecar/query/RSS/p95 performance evidence", "COVERAGE_GATE_E_PERF_EVIDENCE", revision, repo_root, "gate-e"),
            ],
        },
        "F": {
            "local_checks": [
                _local_check("configured_runtime", configured),
                _local_check("active_runtime", active),
                _local_check("legacy_retirement", legacy_retirement),
                _local_check("parser_toolchain", parser),
            ],
            "external_evidence": [
                _external("fresh_production_inventory", "fresh inventory/free-disk/dual-environment evidence", "COVERAGE_GATE_F_INVENTORY_EVIDENCE", revision, repo_root, "gate-f"),
                _external("backup_and_cutover", "verified backup/freeze/drain/cutover/rollback evidence", "COVERAGE_GATE_F_CUTOVER_EVIDENCE", revision, repo_root, "gate-f"),
                _external("acceptance_window", "48-hour acceptance and skill-drift audit evidence", "COVERAGE_GATE_F_ACCEPTANCE_EVIDENCE", revision, repo_root, "gate-f"),
            ],
        },
    }
    for gate, payload in gates.items():
        statuses = [item["status"] for item in payload["local_checks"]]
        statuses.extend(item["status"] for item in payload["external_evidence"])
        payload["status"] = _status(*statuses)
        payload["missing_evidence"] = [
            item.get("requirement") for item in payload["external_evidence"]
            if item.get("status") != "PASSED"
        ]

    overall = _status(*(payload["status"] for payload in gates.values()))
    return {
        "schema_version": 1,
        "evidence_class": "gate_a_f_matrix",
        "candidate_revision": revision,
        "release_identity": identity,
        "host_identity": {
            "hostname": platform.node(), "platform": platform.platform(),
        },
        "generated_at": utc_iso(),
        "status": overall,
        "gates": gates,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=ROOT)
    parser.add_argument("--output", default=".artifacts/gates/gate-matrix.json")
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args(argv)
    repo_root = os.path.abspath(args.repo_root)
    output = os.path.abspath(args.output)
    if not os.path.isabs(args.output):
        output = os.path.join(repo_root, args.output)
    if not os.path.isdir(os.path.dirname(output)):
        os.makedirs(os.path.dirname(output))
    matrix = build(repo_root)
    with open(output, "w", encoding="utf-8") as stream:
        json.dump(matrix, stream, ensure_ascii=False, indent=2, sort_keys=True)
    for gate in GATES:
        gate_dir = os.path.join(os.path.dirname(output), "gate-{}".format(gate.lower()))
        if not os.path.isdir(gate_dir):
            os.makedirs(gate_dir)
        gate_output = os.path.join(gate_dir, "gate-{}_result.json".format(gate.lower()))
        with open(gate_output, "w", encoding="utf-8") as stream:
            json.dump(matrix["gates"][gate], stream, ensure_ascii=False,
                      indent=2, sort_keys=True)
        manifest = EvidenceManifestV2(
            repo_root, "gate-{}".format(gate.lower()),
            candidate_revision=matrix["candidate_revision"],
            release_identity=matrix["release_identity"],
            manifest_path=os.path.join(gate_dir, "evidence-manifest-v2.json"),
        )
        manifest.record(
            "gate-{}-result".format(gate.lower()), "gate_matrix",
            matrix["gates"][gate]["status"],
            command_or_action="python scripts/diagnostics/gate_matrix.py",
            exit_code=0 if matrix["gates"][gate]["status"] == "PASSED" else 1,
            artifact_path=gate_output, source_inputs_sha256=[], synthetic=False,
        )
    print(json.dumps(matrix, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if matrix["status"] == "PASSED" or args.allow_incomplete else 1


if __name__ == "__main__":
    sys.exit(main())
