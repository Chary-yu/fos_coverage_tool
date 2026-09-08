import sqlite3
import tempfile
import unittest

from scripts.upgrade.release_gate_mode import (
    BLOCKED_IDENTITY_INCOMPLETE,
    BLOCKED_LINEAGE_CONFLICT,
    LEGACY_REPORT_COMPATIBLE,
    PATH_MAPPING_DEFERRED,
    PATH_MAPPING_REQUIRED,
    VNEXT_REPORT_DEFERRED,
    VNEXT_REPORT_REQUIRED,
    classify_current_release_gate_mode,
    classify_release_gate_mode,
    classification_signature,
    compare_expected_classification,
    validate_path_mapping_evidence, validate_vnext_report_evidence,
)
from scripts.upgrade.evidence_manifest import ProductionEvidenceManifest
from scripts.upgrade.run_upgrade import UpgradeOrchestrator


class ReleaseGateModeTest(unittest.TestCase):
    def _connection(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE coverage_projects(
                id INTEGER PRIMARY KEY, project_name TEXT NOT NULL
            );
            CREATE TABLE coverage_scans(
                id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
                scan_type TEXT NOT NULL, status TEXT NOT NULL,
                legacy_migrated INTEGER NOT NULL DEFAULT 0,
                info_file_name TEXT NOT NULL DEFAULT '',
                info_sha256 TEXT NOT NULL DEFAULT '',
                imported_at TEXT NOT NULL
            );
            CREATE TABLE coverage_scan_repositories(
                id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL,
                repository_name TEXT NOT NULL,
                commit_sha TEXT, identity_verified INTEGER NOT NULL DEFAULT 0,
                identity_provenance TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE coverage_reports(
                id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL,
                report_id TEXT NOT NULL, report_mode TEXT NOT NULL
            );
            INSERT INTO coverage_projects(id, project_name)
            VALUES (1, 'FOS_V6R2');
        """)
        return connection

    def _insert_scan(self, connection, scan_id=1, scan_type="legacy_migrated",
                     legacy=1, info_name="", info_sha="",
                     report_modes=("LEGACY_STATIC",), repo_identity=False,
                     imported_at="2026-09-01 00:00:00"):
        connection.execute(
            """INSERT INTO coverage_scans(
                   id, project_id, scan_type, status, legacy_migrated,
                   info_file_name, info_sha256, imported_at
               ) VALUES (?, 1, ?, 'ready', ?, ?, ?, ?)""",
            (scan_id, scan_type, legacy, info_name, info_sha, imported_at),
        )
        connection.execute(
            """INSERT INTO coverage_scan_repositories(
                   id, scan_id, repository_name, commit_sha,
                   identity_verified, identity_provenance
               ) VALUES (?, ?, 'repo', ?, ?, ?)""",
            (
                scan_id, scan_id,
                "a" * 40 if repo_identity else None,
                1 if repo_identity else 0,
                "git" if repo_identity else "",
            ),
        )
        for index, mode in enumerate(report_modes):
            connection.execute(
                """INSERT INTO coverage_reports(
                       id, scan_id, report_id, report_mode
                   ) VALUES (?, ?, ?, ?)""",
                (scan_id * 10 + index, scan_id,
                 "report-{}-{}".format(scan_id, index), mode),
            )
        connection.commit()

    def test_current_legacy_static_identity_gap_is_deferred(self):
        connection = self._connection()
        self.addCleanup(connection.close)
        self._insert_scan(connection)
        inputs, result = classify_current_release_gate_mode(
            connection, "FOS_V6R2", LEGACY_REPORT_COMPATIBLE, False
        )
        self.assertEqual(inputs["scan_id"], 1)
        self.assertFalse(inputs["repository_identity_complete"])
        self.assertEqual(result["path_mapping_gate"], PATH_MAPPING_DEFERRED)
        self.assertEqual(result["vnext_report_gate"], VNEXT_REPORT_DEFERRED)
        self.assertFalse(result["report_identity_fabricated"])
        self.assertFalse(result["blocked"])

    def test_authoritative_identity_forces_strict_path_mapping(self):
        connection = self._connection()
        self.addCleanup(connection.close)
        self._insert_scan(
            connection, info_name="coverage.info", info_sha="b" * 64,
            repo_identity=True,
        )
        _inputs, result = classify_current_release_gate_mode(
            connection, "FOS_V6R2", LEGACY_REPORT_COMPATIBLE, False
        )
        self.assertTrue(result["authoritative_lcov_repository_identity"])
        self.assertEqual(result["path_mapping_gate"], PATH_MAPPING_REQUIRED)
        self.assertEqual(result["vnext_report_gate"], VNEXT_REPORT_DEFERRED)
        self.assertFalse(result["blocked"])

    def test_vnext_claim_with_incomplete_identity_blocks(self):
        result = classify_release_gate_mode({
            "scan_type": "legacy_migrated",
            "legacy_migrated": True,
            "report_mode": "LEGACY_STATIC",
            "release_mode": LEGACY_REPORT_COMPATIBLE,
            "repository_identity_complete": False,
            "claims_vnext_code_detail": True,
        })
        self.assertEqual(result["path_mapping_gate"], BLOCKED_IDENTITY_INCOMPLETE)
        self.assertEqual(result["vnext_report_gate"], VNEXT_REPORT_REQUIRED)
        self.assertTrue(result["blocked"])

    def test_conflicting_report_modes_fail_closed(self):
        connection = self._connection()
        self.addCleanup(connection.close)
        self._insert_scan(
            connection,
            report_modes=("LEGACY_STATIC", "VNEXT_ARTIFACT_READY"),
        )
        inputs, result = classify_current_release_gate_mode(
            connection, "FOS_V6R2", LEGACY_REPORT_COMPATIBLE, False
        )
        self.assertTrue(inputs["report_mode_conflict"])
        self.assertEqual(result["path_mapping_gate"], BLOCKED_LINEAGE_CONFLICT)
        self.assertEqual(result["vnext_report_gate"], BLOCKED_LINEAGE_CONFLICT)
        self.assertTrue(result["blocked"])

    def test_expected_runtime_mismatch_is_detected(self):
        actual = classify_release_gate_mode({
            "scan_type": "legacy_migrated",
            "legacy_migrated": True,
            "report_mode": "LEGACY_STATIC",
            "release_mode": LEGACY_REPORT_COMPATIBLE,
            "repository_identity_complete": False,
            "claims_vnext_code_detail": False,
        })
        expected = classification_signature(actual)
        expected["path_mapping_gate"] = PATH_MAPPING_REQUIRED
        errors = compare_expected_classification(expected, actual)
        self.assertTrue(errors)
        self.assertTrue(any("path_mapping_gate" in item for item in errors))

    def test_orchestrator_requeries_and_blocks_expected_classification_drift(self):
        connection = self._connection()
        self.addCleanup(connection.close)
        self._insert_scan(connection)
        _inputs, expected_actual = classify_current_release_gate_mode(
            connection, "FOS_V6R2", LEGACY_REPORT_COMPATIBLE, False
        )
        config = {
            "release_mode": LEGACY_REPORT_COMPATIBLE,
            "claims_vnext_code_detail": False,
            "expected_release_gate_classification": classification_signature(
                expected_actual
            ),
        }
        with tempfile.TemporaryDirectory(prefix="release-gate-refresh-") as root:
            orchestrator = UpgradeOrchestrator(repo_root=root)
            identity = {"commit_sha": "c" * 40}
            result = orchestrator._refresh_release_gate_classification(
                connection, identity, config, "pre_mutation"
            )
            self.assertEqual(result["path_mapping_gate"], PATH_MAPPING_DEFERRED)
            self.assertEqual(
                orchestrator.manifest.data["release_gate_classification"]["status"],
                "PASSED",
            )

            self._insert_scan(
                connection, scan_id=2, scan_type="native", legacy=0,
                report_modes=("VNEXT_ARTIFACT_READY",),
                imported_at="2026-09-02 00:00:00",
            )
            with self.assertRaisesRegex(
                    RuntimeError, "RELEASE_GATE_CLASSIFICATION_MISMATCH"):
                orchestrator._refresh_release_gate_classification(
                    connection, identity, config, "step8_reconfirmation"
                )
            evidence = orchestrator.manifest.data["release_gate_classification"]
            self.assertEqual(evidence["status"], "FAILED")
            self.assertFalse(evidence["classification_matches_expected"])

    def test_final_gate_consumes_deferred_classification_without_requiring_fake_pass(self):
        revision = "d" * 40
        classification = classify_release_gate_mode({
            "scan_type": "legacy_migrated",
            "legacy_migrated": True,
            "report_mode": "LEGACY_STATIC",
            "release_mode": LEGACY_REPORT_COMPATIBLE,
            "repository_identity_complete": False,
            "claims_vnext_code_detail": False,
        })
        with tempfile.TemporaryDirectory(prefix="release-gate-final-") as root:
            manifest = ProductionEvidenceManifest(root)
            manifest.data["upgrade_mode"] = "production"
            manifest.record("release_identity", {
                "version": "v", "commit_sha": revision, "build_id": "b",
                "command": "identity", "exit_code": 0,
            })
            manifest.record("release_gate_classification", dict(
                classification, status="PASSED", revision=revision,
                evidence_class="production_database", command="classify",
                exit_code=0, classification_stage="step8_reconfirmation",
                classification_matches_expected=True,
                expected_classification=classification_signature(classification),
            ))
            manifest.data["candidate_release_prepared"] = {
                "release_manifest": {"report_modes": ["LEGACY_STATIC"]},
            }
            manifest.record("path_mapping_audit", {
                "status": "DEFERRED", "revision": revision,
                "evidence_class": "integration", "command": "defer",
                "exit_code": 0, "gate_status": PATH_MAPPING_DEFERRED,
                "is_valid": False, "input_kind": "legacy_identity_gap",
                "report_identity_fabricated": False,
            })
            _passed, unmet = manifest.validate_final_gate(
                require_traffic_open=False, require_post_open_serving=False
            )
            path_errors = [
                item for item in unmet
                if "path mapping" in item.lower() or
                "release gate classification" in item.lower() or
                "deferred" in item.lower()
            ]
            self.assertEqual(path_errors, [])

            manifest.record("path_mapping_audit", {
                "status": "PASSED", "revision": revision,
                "evidence_class": "integration", "command": "fake pass",
                "exit_code": 0, "gate_status": PATH_MAPPING_DEFERRED,
                "is_valid": True, "input_kind": "legacy_identity_gap",
                "report_identity_fabricated": False,
            })
            _passed, unmet = manifest.validate_final_gate(
                require_traffic_open=False, require_post_open_serving=False
            )
            self.assertTrue(
                any("DEFERRED" in item or "deferred" in item for item in unmet),
                unmet,
            )

    def test_required_vnext_report_gate_requires_vnext_release_evidence(self):
        classification = classify_release_gate_mode({
            "scan_type": "native",
            "legacy_migrated": False,
            "report_mode": "VNEXT_ARTIFACT_READY",
            "release_mode": "FULL_VNEXT",
            "info_file_name": "coverage.info",
            "info_sha256": "e" * 64,
            "repository_identity_complete": True,
            "claims_vnext_code_detail": True,
        })
        self.assertEqual(
            validate_vnext_report_evidence(
                classification,
                {"release_manifest": {"report_modes": ["VNEXT_ARTIFACT_READY"]}},
            ),
            [],
        )
        errors = validate_vnext_report_evidence(
            classification,
            {"release_manifest": {"report_modes": ["LEGACY_STATIC"]}},
        )
        self.assertTrue(errors)

    def test_deferred_vnext_report_gate_requires_explicit_legacy_static(self):
        classification = classify_release_gate_mode({
            "scan_type": "legacy_migrated",
            "legacy_migrated": True,
            "report_mode": "LEGACY_STATIC",
            "release_mode": LEGACY_REPORT_COMPATIBLE,
            "repository_identity_complete": False,
            "claims_vnext_code_detail": False,
        })
        self.assertEqual(
            validate_vnext_report_evidence(
                classification,
                {"release_manifest": {"report_modes": ["LEGACY_STATIC"]}},
            ),
            [],
        )
        errors = validate_vnext_report_evidence(
            classification,
            {"release_manifest": {"report_modes": ["VNEXT_ARTIFACT_READY"]}},
        )
        self.assertTrue(errors)

    def test_deferred_evidence_must_not_claim_pass(self):
        classification = classify_release_gate_mode({
            "scan_type": "legacy_migrated",
            "legacy_migrated": True,
            "report_mode": "LEGACY_STATIC",
            "release_mode": LEGACY_REPORT_COMPATIBLE,
            "repository_identity_complete": False,
            "claims_vnext_code_detail": False,
        })
        classification.update({"status": "PASSED"})
        valid = {
            "status": "DEFERRED",
            "gate_status": PATH_MAPPING_DEFERRED,
            "is_valid": False,
            "input_kind": "legacy_identity_gap",
            "report_identity_fabricated": False,
        }
        self.assertEqual(validate_path_mapping_evidence(classification, valid), [])
        fake_pass = dict(valid, status="PASSED", is_valid=True)
        errors = validate_path_mapping_evidence(classification, fake_pass)
        self.assertTrue(errors)
        self.assertTrue(any("DEFERRED" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
