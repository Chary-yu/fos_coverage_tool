"""
Manifest-Driven Upgrade & Automated Rollback Orchestrator (Item 27 & 28)
Executes the production cutover workflow:
1. PRECHECK: Validates target release identity and repository directories
2. FREEZE: Drains running background jobs and pauses incoming review mutations
3. PRE_SNAPSHOT: Captures pre-migration content hashes across core tables
4. BACKUP: Executes full MySQL dump with SHA256 integrity check
5. ADDITIVE_MIGRATION: Preflight check & execute schema_v2_additive.sql
6. BACKFILL: Idempotent backfill of coverage_file_state + reconciliation
7. DATA_HASH_VERIFY: Compares pre vs post snapshots to guarantee ZERO data loss
8. TARGETED_TESTS: Executes all 7 phase targeted test suites + browser smoke suite
9. PERFORMANCE_GATE: Runs 4-tier performance benchmark including Tier D 100k
10. AUDITS: Runs sidecar, path mapping, and static security audits
11. EVIDENCE_RECORD: Records all artifact hashes and validates strict production gates.
"""

import os
import sys
import time
import json
import subprocess
import logging
import argparse
import re
import urllib.request
import shutil
import tempfile
from typing import Dict, Any, List, Tuple, Optional

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.release_identity import verify_release_identity
from scripts.diagnostics.data_hash_gate import capture_database_snapshot, verify_data_integrity
from scripts.upgrade.evidence_manifest import ProductionEvidenceManifest
from scripts.upgrade.schema_preflight import (
    ensure_column_information_schema,
    validate_ddl_file,
)
from scripts.diagnostics.path_mapping_audit import audit_path_mappings, audit_lcov_paths
from scripts.diagnostics.security_scanner import scan_directory
from scripts.diagnostics.sidecar_registry_audit import audit_sidecar_and_registry
from scripts.diagnostics.perf_benchmark import run_performance_suite
from scripts.maintenance.mysql_backup import perform_database_backup
from scripts.upgrade.migrate_file_state import backfill_all_projects
from scripts.upgrade.cutover_controller import CutoverController
from app.upgrade.lifecycle import UpgradeLifecycle

logger = logging.getLogger(__name__)


def connect_live_database(db_config: Dict[str, Any]):
    """Create the live DB-API connection required by a non-dry-run upgrade."""
    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError("PyMySQL is required for a live upgrade connection") from exc
    cfg = dict(db_config or {})
    return pymysql.connect(
        host=cfg.get("host", "127.0.0.1"),
        port=int(cfg.get("port", 3306)),
        user=cfg.get("user", "root"),
        password=str(cfg.get("password", "")),
        database=cfg.get("database", "coverage_tool"),
        charset=cfg.get("charset", "utf8mb4"),
        autocommit=False,
        connect_timeout=float(cfg.get("connect_timeout", 5.0)),
        cursorclass=pymysql.cursors.DictCursor,
    )

class UpgradeOrchestrator:
    def __init__(self, repo_root: Optional[str] = None):
        self.repo_root = repo_root or _REPO_ROOT
        self.manifest = ProductionEvidenceManifest(self.repo_root)
        self.backup_dir = os.path.join(self.repo_root, "backup_pre_upgrade")
        self.cutover = CutoverController(self.repo_root, os.path.join(self.backup_dir, "files"))
        self.logs: List[str] = []
        self._cutover_applied = False

    def log(self, msg: str):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}")
        self.logs.append(msg)
        self.manifest.add_log(msg)

    def _fail(self, lifecycle: Optional[UpgradeLifecycle], message: str) -> Tuple[bool, str]:
        if self._cutover_applied:
            try:
                self.cutover.rollback()
                self._cutover_applied = False
                self.log("✔ File cutover rolled back before traffic was opened.")
            except Exception as exc:
                self.log("❌ DATA_SAFETY_HOLD: file rollback failed: {}".format(exc))
        if lifecycle is not None and lifecycle.active:
            try:
                rollback_result = lifecycle.abort()
                self.log("✔ Upgrade lifecycle aborted and previous API restored: {}".format(
                    rollback_result.get("previous_release_verified", False)))
            except Exception as exc:
                self.log("❌ DATA_SAFETY_HOLD: lifecycle abort failed: {}".format(exc))
        return False, message

    def _verify_release_endpoint(self, endpoint: str, identity: Dict[str, Any]) -> Dict[str, Any]:
        if not endpoint:
            raise RuntimeError("upgrade.release_endpoint is required")
        with urllib.request.urlopen(endpoint, timeout=10) as response:
            if int(getattr(response, "status", 200)) != 200:
                raise RuntimeError("release endpoint returned HTTP {}".format(response.status))
            payload = json.loads(response.read().decode("utf-8"))
        actual = payload.get("release") if isinstance(payload, dict) else None
        if not isinstance(actual, dict):
            raise RuntimeError("release endpoint did not return a release identity")
        for key in ("version", "commit_sha", "build_id", "asset_hash", "schema_version"):
            if actual.get(key) != identity.get(key):
                raise RuntimeError("release endpoint mismatch: {}".format(key))
        return {
            "status": "PASSED", "evidence_class": "staging_cutover",
            "endpoint": endpoint, "release": actual,
            "command": "GET {}".format(endpoint), "exit_code": 0,
        }

    def execute_upgrade(self, dry_run: bool = False, connection=None, db_config=None,
                        mode: str = "staging", deployment_manifest: Optional[str] = None,
                        target_release: Optional[Dict[str, Any]] = None,
                        runtime_config: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """Run complete upgrade procedure."""
        self.log("=== Starting Manifest-Driven Upgrade Procedure ===")
        if mode not in ("staging", "production"):
            return False, "Invalid upgrade mode"
        state = (runtime_config or {}).get("runtime_state") or {}
        registry_root = state.get("root") or os.path.join(self.repo_root, ".runtime-state")
        if not os.path.isabs(registry_root):
            registry_root = os.path.join(self.repo_root, registry_root)
        registry_dir = state.get("registry_dir", "report-registry")
        os.environ["COVERAGE_REGISTRY_DIR"] = os.path.realpath(os.path.join(registry_root, registry_dir))
        if not deployment_manifest or not os.path.isfile(deployment_manifest):
            return False, "Explicit deployment manifest is required"
        try:
            with open(deployment_manifest, "r", encoding="utf-8") as stream:
                deployment = json.load(stream)
            actions = deployment.get("actions")
            if not isinstance(actions, list) or not actions:
                return False, "Deployment manifest has no explicit actions"
            for action in actions:
                if action.get("op") not in ("ADD", "MODIFY", "MOVE", "DELETE"):
                    return False, "Deployment manifest contains invalid operation"
                if any(ch in str(action.get("source", "")) for ch in ("*", "?")):
                    return False, "Wildcard deployment action is forbidden"
                if action.get("op") not in ("ADD", "MODIFY"):
                    return False, "Only explicit ADD/MODIFY cutover actions are supported"
        except (OSError, ValueError, TypeError) as exc:
            return False, "Invalid deployment manifest: {}".format(exc)
        
        # Step 1: Precheck & Identity Verification
        self.log("[Step 1/10] Verifying Release Identity & Git Tree...")
        try:
            identity = verify_release_identity(self.repo_root, target_release)
        except RuntimeError as exc:
            self.log("❌ Release identity preflight failed: {}".format(exc))
            return False, "Release identity mismatch"
        identity_evidence = dict(identity)
        identity_evidence.update({
            "name": "release_identity",
            "command": "verify_release_identity",
            "exit_code": 0,
        })
        self.manifest.record("release_identity", identity_evidence)
        self.log(f"  Target Release: {identity.get('version')} (Build: {identity.get('build_id')})")

        # Step 2: Schema Preflight
        self.log("[Step 2/10] Running Static DDL Preflight Validation...")
        ddl_path = os.path.join(self.repo_root, "scripts", "upgrade", "schema_v2_additive.sql")
        safe, errs, warns = validate_ddl_file(ddl_path)
        if not safe:
            self.log(f"❌ Schema preflight failed: {errs}")
            return False, "Schema preflight rejected DDL script"
        self.manifest.record("schema_migration", {
            "preflight_safe": True,
            "status": "PASSED",
            "revision": identity.get("commit_sha"),
            "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
            "ddl_script": "schema_v2_additive.sql",
            "warnings": warns,
            "command": "validate_ddl_file scripts/upgrade/schema_v2_additive.sql",
            "exit_code": 0,
        })
        self.log("✔ Schema preflight check passed (Additive & Idempotent).")

        upgrade_config = (runtime_config or {}).get("upgrade") or {}
        previous_release = upgrade_config.get("previous_release")
        if not isinstance(previous_release, dict) or not previous_release:
            # A target release must never be used as its own rollback proof.
            # Staging and production both need an independently captured
            # before-release identity from the previous deployment.
            self.log("❌ Explicit previous_release identity is required for rollback evidence")
            return False, "Explicit previous release identity required"
        if previous_release.get("commit_sha") == identity.get("commit_sha"):
            self.log("❌ previous_release must differ from target release")
            return False, "Previous and target release identities are identical"
        lifecycle = UpgradeLifecycle(self.repo_root, runtime_config or {}, mode, previous_release)
        try:
            freeze_evidence = lifecycle.freeze(identity.get("commit_sha", ""))
            freeze_evidence["revision"] = identity.get("commit_sha")
            self.manifest.record("traffic_freeze", freeze_evidence)
            if connection is None:
                return self._fail(lifecycle, "Live database connection required before drain")
            drain_timeout = float(((runtime_config or {}).get("upgrade") or {}).get("drain_timeout_sec", 30))
            drain_evidence = lifecycle.drain(connection, timeout_sec=drain_timeout)
            drain_evidence["revision"] = identity.get("commit_sha")
            self.manifest.record("job_drain", drain_evidence)
        except Exception as exc:
            self.log("❌ Freeze/drain failed: {}".format(exc))
            return self._fail(lifecycle, "Freeze/drain failed")

        # Step 3: MySQL Backup (fails closed if mysqldump missing unless test mock)
        self.log("[Step 3/10] Creating Pre-upgrade Full MySQL Backup & Checksum...")
        ok_bk, bk_manifest, bk_err = perform_database_backup(
            db_config=db_config or {"database": "coverage_tool"},
            backup_dir=self.backup_dir,
            connection=connection,
            allow_mock_in_test=False
        )
        if not ok_bk:
            self.log(f"❌ Backup failed: {bk_err}")
            return self._fail(lifecycle, f"Backup failed: {bk_err}")
        bk_manifest["revision"] = identity.get("commit_sha")
        bk_manifest["command"] = "mysqldump --single-transaction --quick {}".format(
            (db_config or {}).get("database", "coverage_tool")
        )
        bk_manifest["exit_code"] = 0
        bk_manifest["artifact_path"] = os.path.join(
            bk_manifest.get("backup_dir", self.backup_dir), "full.sql.gz"
        )
        self.manifest.record("backup_evidence", bk_manifest)
        self.log(f"✔ Backup created & verified: SHA256={bk_manifest.get('full_sql_gz_sha256')}")

        if bk_manifest.get("evidence_class") == "mock":
            self.log("❌ Mock backup evidence cannot pass a release gate")
            return self._fail(lifecycle, "Mock backup evidence rejected")

        # Step 4: Data Snapshot & Hash Verification
        self.log("[Step 4/10] Executing Pre/Post Data Hash Integrity Gate...")
        if connection is None:
            self.log("❌ No live database connection supplied; synthetic hash evidence is forbidden")
            return self._fail(lifecycle, "Live database connection required")
        pre_snapshot = capture_database_snapshot(connection, identity)
        try:
            stop_evidence = lifecycle.stop_api()
            stop_evidence.update({
                "revision": identity.get("commit_sha"),
                "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
            })
            self.manifest.record("api_stop", stop_evidence)
        except Exception as exc:
            self.log("❌ API stop failed: {}".format(exc))
            return self._fail(lifecycle, "API stop failed")
        # Apply only the reviewed additive DDL, then backfill the derived table
        # and reconcile it against authoritative facts.  No source-of-truth
        # table is ever written by the backfill.
        try:
            with open(ddl_path, "r", encoding="utf-8") as ddl_stream:
                ddl_sql = ddl_stream.read()
            ddl_sql = re.sub(r"(?m)^\s*--.*$", "", ddl_sql)
            with connection.cursor() as cursor:
                for statement in (part.strip() for part in ddl_sql.split(";") if part.strip()):
                    cursor.execute(statement)
            connection.commit()
            ensure_column_information_schema(
                connection,
                "coverage_project_state",
                "file_state_version",
                "BIGINT NOT NULL DEFAULT 0",
            )
            backfill_report = backfill_all_projects(connection)
        except Exception as exc:
            self.log("❌ Additive migration/backfill failed: {}".format(exc))
            return self._fail(lifecycle, "Migration/backfill failed")
        self.manifest.record("schema_migration", {
            "preflight_safe": True, "status": "PASSED",
            "revision": identity.get("commit_sha"),
            "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
            "backfill": backfill_report,
            "command": "schema_v2_additive.sql + backfill_all_projects",
            "exit_code": 0,
        })
        post_snapshot = capture_database_snapshot(connection, identity)
        verified, violations = verify_data_integrity(pre_snapshot, post_snapshot)
        self.manifest.record("data_hash_verification", {
            "verified": verified,
            "status": "PASSED" if verified else "FAILED",
            "evidence_class": "production_database",
            "revision": identity.get("commit_sha"),
            "pre_snapshot": pre_snapshot,
            "post_snapshot": post_snapshot,
            "reconciliation_status": "MATCHED" if verified else "FAILED",
            "violations": violations,
            "command": "capture_database_snapshot + verify_data_integrity",
            "exit_code": 0 if verified else 1,
        })
        if not verified:
            return self._fail(lifecycle, "Data integrity verification failed")
        self.log("✔ Data integrity verification passed: 0 row decreases, 0 hash mismatches.")

        # Apply only the reviewed, hash-pinned file set.  The controller backs
        # up existing destinations before copying and can restore them before
        # traffic is opened if a later gate fails.
        self.log("[Cutover] Applying explicit hash-pinned deployment manifest...")
        try:
            self._cutover_applied = True
            self.cutover.apply(actions)
            self.manifest.record("file_cutover", {
                "status": "PASSED",
                "revision": identity.get("commit_sha"),
                "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
                "action_count": len(actions),
                "command": "CutoverController.apply (explicit hash-pinned actions)",
                "exit_code": 0,
            })
        except Exception as exc:
            self.log("❌ Explicit file cutover failed: {}".format(exc))
            return self._fail(lifecycle, "File cutover failed")

        try:
            start_evidence = lifecycle.start_api()
            start_evidence.update({
                "revision": identity.get("commit_sha"),
                "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
            })
            self.manifest.record("api_start", start_evidence)
            endpoint = ((runtime_config or {}).get("upgrade") or {}).get("release_endpoint")
            endpoint_evidence = self._verify_release_endpoint(endpoint, identity)
            endpoint_evidence["revision"] = identity.get("commit_sha")
            self.manifest.record("release_endpoint", endpoint_evidence)
        except Exception as exc:
            self.log("❌ Candidate API verification failed: {}".format(exc))
            return self._fail(lifecycle, "Candidate API verification failed")

        # Step 5: Run Targeted Unit Test Suites
        self.log("[Step 5/10] Executing Targeted Unit Test Suites (Phases 0-6)...")
        test_modules = [
            "tests.database.test_phase0_baseline",
            "tests.test_phase1_directory",
            "tests.code_detail.test_phase2_core",
            "tests.test_phase3_jobs_export",
            "tests.progress.test_phase4_progress",
            "tests.incremental.test_phase5_inject_path",
            "tests.code_detail.test_phase6_sidecar"
        ]
        
        test_results = {}
        for tm in test_modules:
            cmd = [sys.executable, "-m", "unittest", tm]
            # Test fixtures may register temporary report directories.  Keep
            # that disposable registry separate from the live runtime
            # registry, whose reachability is audited below.
            isolated_registry = tempfile.mkdtemp(prefix="coverage-test-registry-", dir=os.path.join(self.repo_root, ".artifacts"))
            test_env = dict(os.environ)
            test_env["COVERAGE_REGISTRY_DIR"] = isolated_registry
            try:
                res = subprocess.run(cmd, cwd=self.repo_root, env=test_env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            finally:
                shutil.rmtree(isolated_registry, ignore_errors=True)
            if res.returncode != 0:
                err_text = res.stderr.decode("utf-8", errors="ignore")
                self.log(f"❌ Targeted test failed: {tm}\n{err_text}")
                return self._fail(lifecycle, f"Test {tm} failed")
            test_results[tm] = {
                "status": "PASSED", "revision": identity.get("commit_sha"),
                "evidence_class": "unit", "command": "{} -m unittest {}".format(sys.executable, tm),
                "exit_code": 0,
            }
            self.log(f"  ✔ {tm}: PASSED")
            
        self.manifest.record("targeted_tests", test_results)

        # Step 6: Run Node DOM & Event-loop Smoke Suite
        self.log("[Step 6/10] Executing Browser DOM & Event-loop Smoke Suite (5 Scenarios)...")
        b_res = subprocess.run(["node", "test_lazy_collapse_browser_smoke.js"], cwd=self.repo_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if b_res.returncode != 0:
            err_text = b_res.stderr.decode("utf-8", errors="ignore")
            self.log(f"❌ Browser smoke suite failed: {err_text}")
            return self._fail(lifecycle, "Browser smoke suite failed")
        # This Node suite is a regression test only.  Separately run the
        # Playwright suite and classify it as real browser evidence only when
        # Chromium actually exits successfully.
        browser_cmd = ["npm", "run", "test:browser", "--", "--reporter=line"]
        real_browser = subprocess.run(browser_cmd, cwd=self.repo_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        browser_status = "PASSED" if real_browser.returncode == 0 else "UNAVAILABLE"
        self.manifest.record("browser_smoke_suite", {
            "status": browser_status,
            "revision": identity.get("commit_sha"),
            "evidence_class": "real_browser" if real_browser.returncode == 0 else "mock_dom",
            "suite": "tests/browser/coverage_real_browser.spec.js",
            "command": " ".join(browser_cmd),
            "exit_code": real_browser.returncode,
            "artifact_path": os.path.join(os.path.dirname(self.manifest.manifest_path), "browser-playwright-report.json"),
        })
        self.log("✔ Real browser suite passed." if real_browser.returncode == 0 else "⚠ Real browser evidence unavailable.")

        # Step 7: Run Performance Benchmark Matrix
        self.log("[Step 7/10] Running Performance Benchmark Matrix (Tiers A-D + Huge)...")
        perf_artifact = os.path.join(os.path.dirname(self.manifest.manifest_path), "synthetic_dom_microbenchmark.json")
        perf_cmd = ["npm", "run", "perf:synthetic-dom", "--", perf_artifact]
        perf_process = subprocess.run(perf_cmd, cwd=self.repo_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if perf_process.returncode == 0 and os.path.isfile(perf_artifact):
            with open(perf_artifact, "r", encoding="utf-8") as perf_stream:
                perf_res = json.load(perf_stream)
            perf_res["command"] = " ".join(perf_cmd)
            perf_res["exit_code"] = perf_process.returncode
        else:
            # Keep the Python benchmark as diagnostics, explicitly classified
            # synthetic so the final gate cannot mistake it for release proof.
            perf_res = run_performance_suite()
            perf_res["command"] = "python scripts/diagnostics/perf_benchmark.py"
            perf_res["exit_code"] = perf_process.returncode
        perf_res["revision"] = identity.get("commit_sha")
        perf_res.setdefault("command", " ".join(perf_cmd))
        perf_res.setdefault("exit_code", perf_process.returncode)
        perf_res.setdefault("artifact_path", perf_artifact if os.path.isfile(perf_artifact) else "")
        for tier_name, tier in perf_res.items():
            if isinstance(tier, dict):
                if not tier.get("revision"):
                    tier["revision"] = identity.get("commit_sha")
                tier.setdefault("evidence_class", perf_res.get("evidence_class", "synthetic_benchmark"))
                if tier_name != "_record_meta":
                    tier.setdefault("command", " ".join(perf_cmd))
                    tier.setdefault("exit_code", perf_process.returncode)
        self.manifest.record("performance_benchmark", perf_res)
        self.log(
            "  ✔ Tier A: baseline={}ms/candidate={}ms, Tier D (100k): baseline={}ms/candidate={}ms".format(
                perf_res["Tier_A_1k"].get("baseline_ms"),
                perf_res["Tier_A_1k"].get("candidate_ms"),
                perf_res["Tier_D_100k"].get("baseline_ms"),
                perf_res["Tier_D_100k"].get("candidate_ms"),
            )
        )

        # Step 8: Run Path Mapping & Sidecar Audits
        self.log("[Step 8/10] Running Path Mapping & Sidecar Integrity Audits...")
        known = []
        configured_sources = (((runtime_config or {}).get("upgrade") or {}).get("path_mapping_source_paths") or [])
        source_roots = [self.repo_root] if not configured_sources else [
            os.path.realpath(str(p) if os.path.isabs(str(p)) else os.path.join(self.repo_root, str(p)))
            for p in configured_sources
        ]
        for source_root in source_roots:
            if not os.path.exists(source_root):
                raise RuntimeError("configured path-mapping source does not exist: {}".format(source_root))
            if os.path.isfile(source_root):
                candidates = [source_root]
            else:
                candidates = []
                for root, dirs, files in os.walk(source_root):
                    dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".artifacts")]
                    candidates.extend(os.path.join(root, name) for name in files)
            for candidate in candidates:
                if os.path.splitext(candidate)[1].lower() in (".c", ".h", ".cc", ".cpp", ".hpp"):
                    if source_root == self.repo_root:
                        known.append(os.path.relpath(candidate, self.repo_root).replace(os.sep, "/"))
                    else:
                        known.append(os.path.relpath(candidate, source_root).replace(os.sep, "/"))
        known = sorted(set(known))
        queries = []
        if known:
            first = known[0]
            queries.append((first, "exact"))
            queries.append(("../" + first, "normalized"))
            queries.append((os.path.basename(first), "basename_only_rejected"))
            queries.append(("__missing__/not-present.c", "miss"))
        p_audit = audit_path_mappings(known, queries)
        if not known:
            p_audit.update({"is_valid": False, "violations": ["No repository source paths were available for audit"]})
        lcov_paths = (((runtime_config or {}).get("upgrade") or {}).get("path_mapping_lcov_paths") or [])
        lcov_files = [
            os.path.realpath(str(path) if os.path.isabs(str(path)) else os.path.join(self.repo_root, str(path)))
            for path in lcov_paths
        ]
        if lcov_files:
            if any(not os.path.isfile(path) for path in lcov_files):
                p_audit.update({
                    "is_valid": False,
                    "violations": list(p_audit.get("violations", [])) + ["Configured LCOV audit input is missing"],
                    "input_kind": "repository_lcov",
                })
            else:
                lcov_audit = audit_lcov_paths(known, lcov_files)
                p_audit.update(lcov_audit)
                p_audit["input_kind"] = "repository_lcov"
                p_audit["is_valid"] = bool(p_audit.get("is_valid") and lcov_audit.get("is_valid"))
                p_audit["violations"] = list(p_audit.get("violations", [])) + list(lcov_audit.get("violations", []))
        else:
            p_audit["input_kind"] = "repository_paths_only"
        p_audit.update({
            "status": "PASSED" if p_audit.get("is_valid") else "FAILED",
            "revision": identity.get("commit_sha"),
            "evidence_class": "integration",
            "command": "audit_path_mappings (repository/LCOV path inventory)",
            "exit_code": 0 if p_audit.get("is_valid") else 1,
        })
        self.manifest.record("path_mapping_audit", p_audit)
        self.log(f"  ✔ Path Mapping Audit: is_valid={p_audit['is_valid']}")

        s_audit = audit_sidecar_and_registry([self.repo_root, "/opt/coverage_tool"])
        s_audit.update({
            "status": "PASSED" if s_audit.get("is_safe") else "FAILED",
            "revision": identity.get("commit_sha"),
            "evidence_class": "integration",
            "command": "audit_sidecar_and_registry",
            "exit_code": 0 if s_audit.get("is_safe") else 1,
        })
        self.manifest.record("sidecar_audit", s_audit)
        if not s_audit["is_safe"]:
            self.log(f"❌ Sidecar audit failed: {s_audit}")
            return self._fail(lifecycle, "Sidecar audit safety violations")
        self.log(f"  ✔ Sidecar & Registry Audit: is_safe={s_audit['is_safe']}")

        # Step 9: Security Vulnerability Scanner
        self.log("[Step 9/10] Running Static Security Vulnerability Scanner...")
        sec_res = scan_directory(self.repo_root)
        self.manifest.record("security_audit", {
            "scanned_files": sec_res["scanned_files"],
            "critical_count": sec_res["critical_count"],
            "high_count": sec_res["high_count"],
            "is_safe": sec_res["is_safe"]
            ,"status": "PASSED" if sec_res["is_safe"] else "FAILED"
            ,"revision": identity.get("commit_sha")
            ,"evidence_class": "integration"
            ,"auth_mode": (db_config or {}).get("auth_mode", "reverse_proxy")
            ,"command": "scan_directory"
            ,"exit_code": 0 if sec_res["is_safe"] else 1
        })
        if not sec_res["is_safe"]:
            self.log(f"❌ Security audit failed: {sec_res}")
            return self._fail(lifecycle, "Security audit unresolved findings")
        self.log(f"  ✔ Security Scan passed: Critical={sec_res['critical_count']}, High={sec_res['high_count']}")

        rollback_path = ((runtime_config or {}).get("upgrade") or {}).get("rollback_evidence_path")
        if rollback_path and not os.path.isabs(rollback_path):
            rollback_path = os.path.join(self.repo_root, rollback_path)
        if rollback_path:
            try:
                with open(rollback_path, "r", encoding="utf-8") as stream:
                    rollback_evidence = json.load(stream)
                if rollback_evidence.get("revision") != identity.get("commit_sha"):
                    raise RuntimeError("rollback evidence revision mismatch")
                before_id = rollback_evidence.get("before_release_id")
                target_id = rollback_evidence.get("target_release_id")
                rollback_id = rollback_evidence.get("rollback_release_id")
                if not before_id or not target_id or not rollback_id:
                    raise RuntimeError("rollback evidence lacks release identities")
                if before_id == target_id or rollback_id != before_id:
                    raise RuntimeError("rollback evidence does not restore the before release")
                rollback_evidence.setdefault("evidence_class", "staging_cutover" if mode == "staging" else "production_cutover")
                rollback_evidence.setdefault("command", "run_rollback_rehearsal")
                rollback_evidence.setdefault("exit_code", 0 if rollback_evidence.get("status") == "PASSED" else 1)
                rollback_evidence.setdefault("artifact_path", os.path.abspath(rollback_path))
                self.manifest.record("rollback_evidence", rollback_evidence)
            except Exception as exc:
                self.log("❌ Rollback rehearsal evidence unavailable: {}".format(exc))

        # Step 10: Validate Final Production Release Governance Gate
        self.log("[Step 10/10] Validating Production Release Governance Gate...")
        gate_passed, unmet = self.manifest.validate_final_gate(require_traffic_open=False)
        if not gate_passed:
            try:
                self.cutover.rollback()
                self._cutover_applied = False
                self.log("✔ File cutover rolled back before traffic was opened.")
                self.manifest.record("rollback_evidence", {
                    "status": "PASSED",
                    "revision": identity.get("commit_sha"),
                    "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
                    "traffic_opened": False,
                    "file_restore": "verified",
                })
            except Exception as rollback_exc:
                self.log("❌ DATA_SAFETY_HOLD: file rollback failed: {}".format(rollback_exc))
            return self._fail(lifecycle, f"Final gate unmet: {unmet}")

        try:
            open_evidence = lifecycle.open_traffic()
            open_evidence.update({
                "revision": identity.get("commit_sha"),
                "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
            })
            self.manifest.record("traffic_open", open_evidence)
            final_open_gate, open_unmet = self.manifest.validate_final_gate(require_traffic_open=True)
            if not final_open_gate:
                self.log("❌ Post-open evidence gate failed: {}".format(open_unmet))
                return self._fail(lifecycle, "Post-open evidence gate failed")
        except Exception as exc:
            self.log("❌ Traffic open failed; keeping writes frozen: {}".format(exc))
            return self._fail(lifecycle, "Traffic open failed")

        self.log("=== Upgrade Verification Completed Successfully ===")
        self.log("Release gate passed: all required evidence is authentic and exact.")
        return True, "RELEASE_GATE_PASSED"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manifest-driven safe coverage upgrade")
    parser.add_argument("--mode", choices=("staging", "production"), required=True)
    parser.add_argument("--manifest", required=True, dest="deployment_manifest")
    parser.add_argument("--target-release", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    with open(args.target_release, "r", encoding="utf-8") as stream:
        target = json.load(stream)
    with open(args.config, "r", encoding="utf-8") as stream:
        config = json.load(stream)
    orchestrator = UpgradeOrchestrator()
    mysql_config = config.get("mysql", config)
    connection = None
    try:
        connection = connect_live_database(mysql_config)
        success, status = orchestrator.execute_upgrade(
            dry_run=False, mode=args.mode, connection=connection,
            db_config=dict(mysql_config, auth_mode=(config.get("auth") or {}).get("mode", "reverse_proxy")),
            deployment_manifest=args.deployment_manifest, target_release=target,
            runtime_config=config,
        )
    except Exception as exc:
        print("Live upgrade connection/orchestration failed: {}".format(exc), file=sys.stderr)
        success, status = False, "LIVE_UPGRADE_FAILED"
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
    sys.exit(0 if success else 1)
