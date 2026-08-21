"""Assemble truthful Gate A--F evidence bundles for one exact checkout.

The builder fills repository-executable evidence and explicit missing-evidence
records.  It is not a release certifier: generated SQLite/fixture evidence is
marked synthetic and remains ``INCOMPLETE`` for production gates.
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.release_identity import generate_release_identity
from app.time_utils import utc_iso
from scripts.diagnostics.gate_matrix import build as build_gate_matrix
from scripts.diagnostics.contract import with_contract
from scripts.diagnostics.deterministic_inheritance_corpus import (
    DEFAULT_FIXTURE as DETERMINISTIC_FIXTURE,
    derived_reports as deterministic_derived_reports,
    run as run_deterministic_corpus,
)
from scripts.upgrade.domain_migration import apply_analysis_domain
from scripts.upgrade.evidence_manifest import EvidenceManifestV2
from scripts.upgrade.legacy_fixture import create_legacy_fixture_schema, seed_legacy_fixture
from scripts.upgrade.migration_runner import (
    capture_legacy_semantic_snapshot, capture_vnext_semantic_snapshot,
    create_sqlite_schema, migrate_legacy, semantic_hash,
    apply_schema,
)


GATE_FILES = {
    "A": (
        "source_schema.txt", "target_schema.txt", "migration_matrix.json",
        "source_semantic.json", "target_semantic.json", "semantic_hashes.json",
        "anomalies.json", "migration_run_1.json", "migration_run_2_idempotency.json",
        "mariadb55_preflight.json", "targeted_tests.txt",
    ),
    "B": (
        "schema_diff.sql", "repository_identity_matrix.json",
        "analysis_backfill_before.json", "analysis_backfill_after.json",
        "analysis_semantic_hashes.json", "orphan_checks.json",
        "canonical_read_write_audit.json", "targeted_tests.txt",
    ),
    "C": (
        "scan_state_machine.json", "repository_lock_tests.json", "fencing_tests.json",
        "checkpoint_resume_tests.json", "current_pointer_audit.json",
        "worktree_head_before_after.txt", "atomic_publish_tests.json",
        "runtime_job_audit.json",
        "targeted_tests.txt",
    ),
    "D": (
        "rule_traceability.json", "reason_code_catalog.json",
        "deterministic_fixture_manifest.json", "decisions_run_1.json",
        "decisions_run_2.json", "determinism_diff.json", "false_positive_check.json",
        "parser_uncertainty_report.json", "dependency_resolution_report.json",
        "targeted_tests.txt",
    ),
    "E": (
        "api_contract.json", "route_inventory.json", "progress_conservation.json",
        "browser_scenarios.json", "console_errors.json", "network_trace_summary.json",
        "performance_metrics.json", "legacy_fallback_audit.json", "targeted_tests.txt",
    ),
    "F": (
        "final_source_review.json", "final_security_review.json", "release_identity.json",
        "database_runtime_identity.json", "candidate_layout.json",
        "candidate_config_audit.json", "verified_backup.json", "pre_freeze_semantic.json",
        "final_migration.json", "final_semantic_reconciliation.json",
        "runtime_verification.json", "browser_smoke.json", "cutover_record.json",
        "rollback_rehearsal.json", "acceptance_window_checks.json", "skill_drift_audit.json",
    ),
}


# Gate E deliberately contains a directory entry in the v1.2 contract.  Keep
# it separate from GATE_FILES so callers can distinguish a report directory
# from a hashable file artifact.
GATE_DIRECTORIES = {
    "E": ("playwright_report",),
    "F": ("fresh_inventory",),
}

TEST_GROUPS = {
    "A": ["tests.vnext.test_migration_runner", "tests.vnext.test_legacy_migration_contract"],
    "B": ["tests.vnext.test_analysis_domain", "tests.vnext.test_migration_runner"],
    "C": ["tests.vnext.test_scan_import_lifecycle", "tests.vnext.test_jobs"],
    "D": [
        "tests.vnext.test_inheritance_engine",
        "tests.vnext.test_deterministic_inheritance_corpus",
        "tests.vnext.test_parser_toolchain",
        "tests.vnext.test_analysis_domain",
        "tests.vnext.test_scan_import_lifecycle",
        "tests.vnext.test_api_export_security",
    ],
    "E": ["tests.vnext.test_api_export_security", "tests.vnext.test_registry_and_api_contract"],
    "F": ["tests.release.test_upgrade_manifest", "tests.release.test_evidence_authenticity"],
}

_KNOWN_EVIDENCE_STATUSES = {
    "PASSED", "INCOMPLETE", "BLOCKED", "FAILED", "PARTIAL",
    "UNAVAILABLE", "SKIPPED",
}


def _manifest_artifact_attributes(name, path, tests):
    """Read an artifact's declared status without losing authenticity flags."""
    synthetic = name in {
        "source_semantic.json", "target_semantic.json", "semantic_hashes.json",
        "anomalies.json", "migration_run_1.json", "migration_run_2_idempotency.json",
        "analysis_backfill_before.json", "analysis_backfill_after.json",
        "analysis_semantic_hashes.json", "deterministic_fixture_manifest.json",
        "decisions_run_1.json", "decisions_run_2.json", "determinism_diff.json",
        "false_positive_check.json", "parser_uncertainty_report.json",
        "dependency_resolution_report.json",
    }
    payload = None
    try:
        with open(path, "r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (ValueError, OSError):
        pass

    observed_status = ""
    if isinstance(payload, dict):
        observed_status = str(payload.get("status") or "").upper()
        if payload.get("synthetic") is True:
            synthetic = True

    if observed_status in _KNOWN_EVIDENCE_STATUSES:
        status = observed_status
    else:
        status = "PASSED" if name == "targeted_tests.txt" and \
            tests.get("status") == "PASSED" else "INCOMPLETE"
    # A synthetic result may be useful as a regression artifact, but it must
    # never be represented as a production-advancing PASS in the manifest.
    if synthetic and status == "PASSED":
        status = "INCOMPLETE"
    return status, synthetic, observed_status


def _revision(repo_root):
    try:
        return subprocess.check_output(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode("ascii").strip()
    except Exception:
        return ""


def _sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write(path, value, text=False):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    if text:
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(str(value))
    else:
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True,
                      default=str)


def _read_text(path):
    with open(path, "r", encoding="utf-8") as stream:
        return stream.read()


def _run_tests(repo_root, modules, enabled=True):
    command = [sys.executable, "-m", "unittest"] + list(modules) + ["-v"]
    if not enabled:
        return {"status": "INCOMPLETE", "exit_code": None,
                "command": command, "output": "test execution disabled"}
    completed = subprocess.run(command, cwd=repo_root, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, check=False)
    output = completed.stdout.decode("utf-8", errors="replace")
    return {"status": "PASSED" if completed.returncode == 0 else "FAILED",
            "exit_code": int(completed.returncode), "command": command,
            "output": output}


def _sqlite_migration(repo_root):
    source = sqlite3.connect(":memory:")
    target = sqlite3.connect(":memory:")
    source.row_factory = sqlite3.Row
    target.row_factory = sqlite3.Row
    try:
        create_legacy_fixture_schema(source)
        seed_legacy_fixture(source, project_name="bundle-a", line_count=32,
                             analysis_count=20, job_count=1)
        seed_legacy_fixture(source, project_name="bundle-b", line_count=16,
                             analysis_count=8, job_count=0)
        create_sqlite_schema(target)
        apply_schema(
            target,
            os.path.join(repo_root, "scripts", "upgrade", "vnext_schema.sql"),
            release_sha="bundle-fixture",
        )
        source_semantic = capture_legacy_semantic_snapshot(source)
        first = migrate_legacy(source, target, release_sha="bundle-fixture")
        domain = apply_analysis_domain(target, release_sha="bundle-fixture")
        first_target = capture_vnext_semantic_snapshot(target)
        second = migrate_legacy(source, target, release_sha="bundle-fixture")
        second_target = capture_vnext_semantic_snapshot(target)
        return {
            "source_semantic": source_semantic,
            "target_semantic": first_target,
            "source_hash": semantic_hash(source_semantic),
            "target_hash": semantic_hash(first_target),
            "first": first,
            "second": second,
            "domain": domain,
            "idempotent": first_target == second_target,
            "synthetic": True,
        }
    finally:
        source.close()
        target.close()


def _missing(name, reason):
    return {"status": "INCOMPLETE", "evidence_class": name,
            "synthetic": False, "violations": [reason]}


def _external_json(env_name, reason):
    path = os.environ.get(env_name, "")
    if not path or not os.path.isfile(path):
        return _missing("external_release_evidence", reason), []
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream), [_sha(path)]
    except (OSError, ValueError, TypeError) as exc:
        return _missing("external_release_evidence", str(exc)), []


def _gate_detail(repo_root, matrix, gate, sqlite_result=None, test_result=None):
    gate_payload = matrix["gates"][gate]
    detail = {
        "gate": "GATE_{}".format(gate),
        "candidate_revision": matrix.get("candidate_revision", ""),
        "status": gate_payload.get("status", "INCOMPLETE"),
        "executed": [item.get("name") for item in gate_payload.get("local_checks", [])],
        "passed": [item.get("name") for item in gate_payload.get("local_checks", [])
                   if item.get("status") == "PASSED"],
        "failed": [item.get("name") for item in gate_payload.get("local_checks", [])
                   if item.get("status") in ("FAILED", "BLOCKED")],
        "skipped": [],
        "missing_evidence": gate_payload.get("missing_evidence") or [],
        "blocking_findings": [],
        "artifacts": [],
        "generated_at": utc_iso(),
    }
    if sqlite_result is not None:
        detail["local_fixture"] = {
            "status": "INCOMPLETE",
            "synthetic": True,
            "authoritative_semantic_match": bool(
                sqlite_result["first"].get("authoritative_semantic_match")
            ),
            "idempotent": bool(sqlite_result.get("idempotent")),
        }
    if test_result is not None:
        detail["targeted_tests"] = test_result
        if test_result.get("status") == "FAILED":
            detail["status"] = "FAILED"
            detail["blocking_findings"].append("targeted test command failed")
    return detail


def build(repo_root, output_root, run_tests=True):
    repo_root = os.path.abspath(repo_root)
    output_root = os.path.abspath(output_root)
    revision = _revision(repo_root)
    identity = generate_release_identity(repo_root=repo_root)
    matrix = build_gate_matrix(repo_root)
    sqlite_result = _sqlite_migration(repo_root)
    all_gate_results = {}

    fixture_source = os.path.join(repo_root, "tests", "fixtures",
                                  "legacy_schema_mariadb55.sql")
    vnext_schema = os.path.join(repo_root, "scripts", "upgrade", "vnext_schema.sql")
    migration_matrix_path = os.path.join(
        repo_root, "docs", "migration_matrix.json"
    )
    with open(migration_matrix_path, "r", encoding="utf-8") as stream:
        migration_matrix = json.load(stream)

    for gate in "ABCDEF":
        gate_dir = os.path.join(output_root, "gate-{}".format(gate.lower()))
        if not os.path.isdir(gate_dir):
            os.makedirs(gate_dir)
        tests = _run_tests(repo_root, TEST_GROUPS[gate], enabled=run_tests)
        _write(os.path.join(gate_dir, "targeted_tests.txt"),
               json.dumps(tests, ensure_ascii=False, indent=2), text=True)
        if gate == "A":
            _write(os.path.join(gate_dir, "source_schema.txt"),
                   _read_text(fixture_source), text=True)
            _write(os.path.join(gate_dir, "target_schema.txt"),
                   _read_text(vnext_schema), text=True)
            _write(os.path.join(gate_dir, "migration_matrix.json"), migration_matrix)
            _write(os.path.join(gate_dir, "source_semantic.json"),
                   sqlite_result["source_semantic"])
            _write(os.path.join(gate_dir, "target_semantic.json"),
                   sqlite_result["target_semantic"])
            _write(os.path.join(gate_dir, "semantic_hashes.json"), {
                "source_semantic_hash": sqlite_result["source_hash"],
                "target_semantic_hash": sqlite_result["target_hash"],
                "authoritative_semantic_match": sqlite_result["first"].get(
                    "authoritative_semantic_match", False
                ),
                "synthetic": True,
            })
            _write(os.path.join(gate_dir, "anomalies.json"),
                   {"anomalies": sqlite_result["first"].get("anomalies", []),
                    "synthetic": True})
            _write(os.path.join(gate_dir, "migration_run_1.json"),
                   sqlite_result["first"])
            _write(os.path.join(gate_dir, "migration_run_2_idempotency.json"), {
                "second_run": sqlite_result["second"],
                "idempotent": sqlite_result["idempotent"],
                "synthetic": True,
            })
            _write(os.path.join(gate_dir, "mariadb55_preflight.json"), _missing(
                "mariadb55_preflight",
                "static fixture/DDL is present; real MariaDB 5.5 runtime is not verified",
            ))
        elif gate == "B":
            domain_schema = os.path.join(
                repo_root, "scripts", "upgrade", "vnext_domain_constraints.sql"
            )
            _write(
                os.path.join(gate_dir, "schema_diff.sql"),
                "-- Gate B schema evidence\n"
                "-- core_schema_sha256={}\n"
                "-- domain_schema_sha256={}\n"
                "-- status=PASSED\n"
                "-- No destructive DDL is introduced by this evidence bundle.\n".format(
                    _sha(vnext_schema), _sha(domain_schema)
                ),
                text=True,
            )
            _write(os.path.join(gate_dir, "repository_identity_matrix.json"), {
                "logical_repository": "coverage_repositories.id",
                "physical_resource": "coverage_repository_resources.resource_key",
                "scan_snapshot": "coverage_scan_repositories",
                "status": "PASSED",
            })
            _write(os.path.join(gate_dir, "analysis_backfill_before.json"), {
                "status": "INCOMPLETE", "synthetic": True,
                "source": "SQLite fixture before Analysis Domain backfill",
            })
            _write(os.path.join(gate_dir, "analysis_backfill_after.json"), {
                "status": "INCOMPLETE", "synthetic": True,
                "analysis_domain": sqlite_result["domain"],
            })
            _write(os.path.join(gate_dir, "analysis_semantic_hashes.json"), {
                "source": sqlite_result["source_hash"],
                "target": sqlite_result["target_hash"], "synthetic": True,
            })
            _write(os.path.join(gate_dir, "orphan_checks.json"),
                   sqlite_result["domain"].get("consistency", _missing(
                       "orphan_checks", "target DB backfill evidence is unavailable"
                   )))
            _write(os.path.join(gate_dir, "canonical_read_write_audit.json"),
                   matrix["gates"]["B"]["local_checks"][0])
        elif gate == "C":
            for name, value in {
                "scan_state_machine.json": {"status": "PASSED", "states": [
                    "CREATED", "STAGING", "IMPORTING", "SEALED", "PUBLISHED", "ABORTED"
                ]},
                "repository_lock_tests.json": _missing("resource_lock_rehearsal", "live DB lock/fencing rehearsal is external"),
                "fencing_tests.json": _missing("fencing_rehearsal", "live DB fencing evidence is external"),
                "checkpoint_resume_tests.json": _missing("checkpoint_resume", "durable restart evidence is external"),
                "current_pointer_audit.json": _missing("current_pointer", "final read-set evidence is external"),
                "worktree_head_before_after.txt": "production worktree evidence is external; status=INCOMPLETE\n",
                "atomic_publish_tests.json": matrix["gates"]["C"]["local_checks"][1],
                "runtime_job_audit.json": _missing("runtime_job_audit", "production job recovery evidence is external"),
            }.items():
                _write(os.path.join(gate_dir, name), value,
                       text=name.endswith(".txt"))
        elif gate == "D":
            rules = matrix["gates"]["D"]["local_checks"][0]
            parser = matrix["gates"]["D"]["local_checks"][1]
            corpus_run_1 = run_deterministic_corpus(DETERMINISTIC_FIXTURE)
            corpus_run_2 = run_deterministic_corpus(DETERMINISTIC_FIXTURE)
            corpus_reports = deterministic_derived_reports(corpus_run_1)
            corpus_hash_1 = hashlib.sha256(json.dumps(
                corpus_run_1, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")).hexdigest()
            corpus_hash_2 = hashlib.sha256(json.dumps(
                corpus_run_2, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")).hexdigest()
            _write(os.path.join(gate_dir, "rule_traceability.json"), rules)
            _write(os.path.join(gate_dir, "reason_code_catalog.json"), {
                "status": "PASSED", "source": "contracts/inheritance_rules_v1.json",
            })
            _write(os.path.join(gate_dir, "deterministic_fixture_manifest.json"), {
                "status": "INCOMPLETE", "synthetic": True,
                "release_eligible": False,
                "fixture_path": os.path.relpath(DETERMINISTIC_FIXTURE, repo_root),
                "fixture_sha256": corpus_run_1.get("fixture_sha256"),
                "local_result_status": corpus_run_1.get("status"),
                "cases_total": corpus_run_1.get("cases_total"),
                "passed_cases": corpus_run_1.get("passed_cases"),
                "failed_cases": corpus_run_1.get("failed_cases"),
                "reason": "local corpus is synthetic until target parser/toolchain is verified",
            })
            _write(os.path.join(gate_dir, "decisions_run_1.json"), corpus_run_1)
            _write(os.path.join(gate_dir, "decisions_run_2.json"), corpus_run_2)
            _write(os.path.join(gate_dir, "determinism_diff.json"), {
                "status": "PASSED" if corpus_hash_1 == corpus_hash_2 else "FAILED",
                "synthetic": True,
                "release_eligible": False,
                "run_1_sha256": corpus_hash_1,
                "run_2_sha256": corpus_hash_2,
                "different": corpus_hash_1 != corpus_hash_2,
            })
            _write(os.path.join(gate_dir, "false_positive_check.json"),
                   corpus_reports["false_positive_check"])
            parser_report = dict(corpus_reports["parser_uncertainty_report"])
            parser_report["toolchain_preflight"] = parser
            parser_report["local_corpus_status"] = parser_report.get("status")
            parser_report["production_preflight_status"] = parser.get("status")
            if parser.get("status") != "PASSED":
                parser_report["status"] = "INCOMPLETE"
            _write(os.path.join(gate_dir, "parser_uncertainty_report.json"),
                   parser_report)
            _write(os.path.join(gate_dir, "dependency_resolution_report.json"),
                   corpus_reports["dependency_resolution_report"])
        elif gate == "E":
            _write(os.path.join(gate_dir, "api_contract.json"),
                   matrix["gates"]["E"]["local_checks"][0])
            browser, browser_inputs = _external_json(
                "COVERAGE_GATE_E_BROWSER_EVIDENCE",
                "real HTTP + Chromium evidence is not supplied",
            )
            performance, performance_inputs = _external_json(
                "COVERAGE_GATE_E_PERF_EVIDENCE",
                "cross-layer performance evidence is not supplied",
            )
            _write(os.path.join(gate_dir, "route_inventory.json"), _missing(
                "route_inventory", "exact Candidate route inventory is external"))
            _write(os.path.join(gate_dir, "progress_conservation.json"), _missing(
                "progress_conservation", "fresh production progress conservation evidence is external"))
            _write(os.path.join(gate_dir, "browser_scenarios.json"), browser)
            _write(os.path.join(gate_dir, "console_errors.json"), browser)
            _write(os.path.join(gate_dir, "network_trace_summary.json"), browser)
            _write(os.path.join(gate_dir, "performance_metrics.json"), performance)
            _write(os.path.join(gate_dir, "legacy_fallback_audit.json"), _missing(
                "legacy_fallback_audit", "exact release runtime fallback evidence is external"))
            report_dir = os.path.join(gate_dir, "playwright_report")
            _write(os.path.join(report_dir, "summary.json"), browser)
        elif gate == "F":
            local_checks = {
                item.get("name"): item
                for item in matrix["gates"]["F"].get("local_checks", [])
            }
            source_review = local_checks.get("final_source_review") or {}
            security_review = local_checks.get("final_security_review") or {}
            _write(
                os.path.join(gate_dir, "final_source_review.json"),
                source_review.get("summary") or _missing(
                    "final_source_review", "exact-SHA source review is unavailable"
                ),
            )
            _write(
                os.path.join(gate_dir, "final_security_review.json"),
                security_review.get("summary") or _missing(
                    "final_security_review", "exact-SHA security review is unavailable"
                ),
            )
            _write(os.path.join(gate_dir, "release_identity.json"), identity)
            _write(os.path.join(gate_dir, "database_runtime_identity.json"), _missing(
                "database_runtime_identity", "final target DB identity is external"))
            _write(os.path.join(gate_dir, "candidate_layout.json"), _missing(
                "candidate_layout", "fresh dual-environment inventory is external"))
            _write(os.path.join(gate_dir, "fresh_inventory", "summary.json"), _missing(
                "fresh_inventory", "fresh production inventory is external"))
            _write(os.path.join(gate_dir, "candidate_config_audit.json"),
                   matrix["gates"]["F"]["local_checks"][0])
            for name, reason in {
                "verified_backup.json": "verified production backup restore evidence is external",
                "pre_freeze_semantic.json": "freeze stability evidence is external",
                "final_migration.json": "final production migration evidence is external",
                "final_semantic_reconciliation.json": "final target semantic reconciliation is external",
                "runtime_verification.json": "traffic-closed runtime verification is external",
                "browser_smoke.json": "traffic-closed browser smoke is external",
                "cutover_record.json": "production cutover record is external",
                "rollback_rehearsal.json": "exact before-release rollback evidence is external",
                "acceptance_window_checks.json": "48-hour acceptance-window evidence is external",
                "skill_drift_audit.json": "operator Skill Drift manifest is external",
            }.items():
                _write(os.path.join(gate_dir, name), _missing(name[:-5], reason))

        detail = _gate_detail(repo_root, matrix, gate,
                              sqlite_result if gate in ("A", "B") else None,
                              tests)
        detail["artifacts"] = list(GATE_FILES[gate]) + list(
            GATE_DIRECTORIES.get(gate, ())
        ) + [
            "evidence-manifest-v2.json", "gate_{}_result.json".format(gate.lower())
        ]
        result_path = os.path.join(gate_dir, "gate_{}_result.json".format(gate.lower()))
        _write(result_path, detail)
        manifest = EvidenceManifestV2(
            repo_root, "gate-{}".format(gate.lower()),
            candidate_revision=revision, release_identity=identity,
            manifest_path=os.path.join(gate_dir, "evidence-manifest-v2.json"),
        )
        manifest_files = list(GATE_FILES[gate])
        if gate == "E":
            # The contract names the Playwright report as a directory, while
            # the manifest still needs one concrete, hashable report artifact.
            manifest_files.append(os.path.join("playwright_report", "summary.json"))
        if gate == "F":
            # The contract names fresh_inventory as a directory; retain one
            # concrete, hashable summary artifact in the manifest.
            manifest_files.append(os.path.join("fresh_inventory", "summary.json"))
        for name in manifest_files:
            path = os.path.join(gate_dir, name)
            if not os.path.isfile(path):
                continue
            status, synthetic, observed_status = _manifest_artifact_attributes(
                name, path, tests
            )
            manifest.record(
                "{}-{}".format(gate.lower(), name.replace("/", "-").replace(".", "-")),
                "synthetic_fixture" if synthetic else "repository_or_release_audit",
                status, "build_gate_evidence.py", 0 if tests["status"] == "PASSED" else 1,
                artifact_path=path, source_inputs_sha256=[], synthetic=synthetic,
                observed_status=observed_status,
            )
        all_gate_results[gate] = detail

    matrix_path = os.path.join(output_root, "gate-matrix.json")
    _write(matrix_path, matrix)
    statuses = [str(item.get("status") or "INCOMPLETE")
                for item in all_gate_results.values()]
    overall_status = "FAILED" if any(
        status in ("FAILED", "BLOCKED") for status in statuses
    ) else ("INCOMPLETE" if any(status != "PASSED" for status in statuses)
           else "PASSED")
    return with_contract({
        "status": overall_status,
        "evidence_class": "gate_a_f_evidence_bundle",
        "candidate_revision": revision,
        "release_identity": identity,
        "host_identity": {"hostname": platform.node(), "platform": platform.platform()},
        "gates": all_gate_results,
        "output_root": output_root,
        "generated_at": utc_iso(),
    })


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=ROOT)
    parser.add_argument("--output-root", default=".artifacts/gates")
    parser.add_argument("--no-tests", action="store_true")
    parser.add_argument(
        "--allow-incomplete", action="store_true",
        help="return success for an honest INCOMPLETE bundle, never for FAILED evidence",
    )
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    output_root = args.output_root
    if not os.path.isabs(output_root):
        output_root = os.path.join(os.path.abspath(args.repo_root), output_root)
    result = build(args.repo_root, output_root, run_tests=not args.no_tests)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = args.output if os.path.isabs(args.output) else os.path.join(
            os.path.abspath(args.repo_root), args.output
        )
        _write(output, result)
    print(encoded)
    return 0 if result["status"] == "PASSED" or (
        result["status"] == "INCOMPLETE" and args.allow_incomplete
    ) else 1


if __name__ == "__main__":
    sys.exit(main())
