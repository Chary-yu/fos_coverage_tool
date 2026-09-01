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
import hashlib
import math
import subprocess
import logging
import argparse
import re
import urllib.request
import shutil
import tempfile
import uuid
from typing import Dict, Any, List, Tuple, Optional

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.release_identity import verify_release_identity
from app.release_publication import ImmutableReleasePublisher
from scripts.diagnostics.data_hash_gate import capture_database_snapshot, verify_data_integrity
from scripts.upgrade.evidence_manifest import ProductionEvidenceManifest
from scripts.upgrade.schema_preflight import (
    ensure_column_information_schema,
    validate_ddl_file,
)
from scripts.diagnostics.path_mapping_audit import audit_path_mappings, audit_lcov_paths
from scripts.diagnostics.security_scanner import scan_directory
from scripts.diagnostics.sidecar_registry_audit import audit_sidecar_and_registry
from scripts.maintenance.mysql_backup import perform_database_backup
from scripts.upgrade.migrate_file_state import backfill_all_projects
from app.upgrade.lifecycle import UpgradeLifecycle
from scripts.upgrade.validation_session import (
    SESSION_SCHEMA_VERSION, ValidationSession,
)

logger = logging.getLogger(__name__)


def _path_is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([
            os.path.realpath(os.path.abspath(path)),
            os.path.realpath(os.path.abspath(root)),
        ]) == os.path.realpath(os.path.abspath(root))
    except (AttributeError, OSError, ValueError):
        return False


def _resolve_upgrade_path(repo_root: str, configured: Optional[str], field: str) -> str:
    """Resolve a required upgrade control path relative to the checkout."""
    if not configured:
        raise RuntimeError("upgrade.{} is required".format(field))
    value = str(configured)
    if not os.path.isabs(value):
        value = os.path.join(repo_root, value)
    return os.path.realpath(os.path.abspath(value))


def _new_release_validation_session_id(commit_sha: str,
                                       configured: Optional[str] = None) -> str:
    """Return a unique validation attempt identity for one Candidate SHA."""
    explicit = str(configured or "").strip()
    if explicit:
        return explicit
    return "candidate-{}-{}".format(
        str(commit_sha or "").strip(), uuid.uuid4().hex[:16]
    )


def _resolve_attempt_path(repo_root: str, configured: Optional[str], field: str,
                          attempt_id: str) -> str:
    """Resolve an evidence path without reusing a previous attempt file."""
    if not configured:
        raise RuntimeError("upgrade.{} is required".format(field))
    raw = str(configured)
    if "{attempt_id}" in raw:
        raw = raw.replace("{attempt_id}", str(attempt_id))
    resolved = _resolve_upgrade_path(repo_root, raw, field)
    if not os.path.lexists(resolved):
        return resolved
    stem, extension = os.path.splitext(resolved)
    return os.path.realpath("{}.{}{}".format(stem, attempt_id, extension))


def resolve_backup_root(repo_root: str, configured_root: Optional[str] = None) -> str:
    """Resolve a recoverable backup root outside the active deployment tree."""
    raw = configured_root or os.environ.get("COVERAGE_BACKUP_ROOT")
    if raw:
        candidate = str(raw)
        if not os.path.isabs(candidate):
            # Relative backup roots are deliberately relative to the deploy
            # parent, not the deploy tree itself.
            candidate = os.path.join(os.path.dirname(os.path.abspath(repo_root)), candidate)
    else:
        candidate = os.path.join(
            os.path.dirname(os.path.abspath(repo_root)),
            ".coverage-backups", os.path.basename(os.path.abspath(repo_root)),
        )
    candidate = os.path.realpath(os.path.abspath(candidate))
    if _path_is_within(candidate, repo_root):
        raise ValueError("backup root must be outside the active deployment root")
    return candidate


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_release_performance_artifact(path: str, payload: Dict[str, Any],
                                           target_revision: str) -> List[str]:
    """Validate immutable release A/B evidence before the cutover can finish.

    The normal synthetic benchmark is intentionally not accepted here.  A
    release artifact must be produced by two isolated exact-revision runs and
    must retain the hashes of both source artifacts.
    """
    errors = []
    if not isinstance(payload, dict):
        return ["performance evidence is not a JSON object"]
    if payload.get("status") != "PASSED":
        errors.append("release performance A/B status is not PASSED")
    if payload.get("evidence_class") != "release_performance_ab":
        errors.append("performance evidence is not release_performance_ab")
    if payload.get("comparison_type") != "release_revision_ab":
        errors.append("performance evidence is not a release revision comparison")
    if payload.get("candidate_commit") != target_revision:
        errors.append("performance candidate_commit does not match target release")
    if not payload.get("baseline_commit") or payload.get("baseline_commit") == target_revision:
        errors.append("performance baseline_commit is missing or equals candidate")
    if not payload.get("workload_id") or not payload.get("workload_hash"):
        errors.append("performance workload identity is incomplete")
    if not isinstance(payload.get("environment_identity"), dict) or not payload.get("environment_identity"):
        errors.append("performance environment_identity is missing")
    if payload.get("exit_code") != 0:
        errors.append("PASSED performance evidence must have exit_code=0")
    if payload.get("synthetic"):
        errors.append("synthetic performance evidence cannot pass the release gate")

    for field in ("baseline_ms", "candidate_ms"):
        if not isinstance(payload.get(field), (int, float)) or not math.isfinite(float(payload.get(field))):
            errors.append("performance {} is missing or non-finite".format(field))
    for tier_name in ("Tier_A_1k", "Tier_B_10k", "Tier_C_50k", "Tier_D_100k"):
        tier = payload.get(tier_name)
        if not isinstance(tier, dict) or tier.get("status") != "PASSED":
            errors.append("performance tier {} is not PASSED".format(tier_name))
            continue
        for field in ("baseline_ms", "candidate_ms"):
            if not isinstance(tier.get(field), (int, float)) or not math.isfinite(float(tier.get(field))):
                errors.append("performance {}.{} is missing or non-finite".format(tier_name, field))

    virtual = payload.get("coverage_virtual_scroll_100k")
    if not isinstance(virtual, dict) or virtual.get("status") != "PASSED":
        errors.append("100k virtual-scroll release workload is not PASSED")
    elif not isinstance(virtual.get("candidate_elapsed_ms"), (int, float)):
        errors.append("100k virtual-scroll candidate elapsed time is missing")

    source_artifacts = payload.get("source_artifacts")
    source_payloads = {}
    if not isinstance(source_artifacts, dict):
        errors.append("source_artifacts are missing")
    else:
        for role in ("baseline", "candidate"):
            source = source_artifacts.get(role)
            source_path = source.get("path") if isinstance(source, dict) else ""
            source_sha = source.get("sha256") if isinstance(source, dict) else ""
            source_revision = source.get("revision") if isinstance(source, dict) else ""
            if not source_path or not os.path.isabs(str(source_path)) or not os.path.isfile(str(source_path)):
                errors.append("{} source performance artifact is missing".format(role))
                continue
            if not source_sha or _sha256_file(str(source_path)) != str(source_sha):
                errors.append("{} source performance artifact SHA256 mismatch".format(role))
            expected_revision = payload.get("baseline_commit") if role == "baseline" else payload.get("candidate_commit")
            if source_revision != expected_revision:
                errors.append("{} source performance artifact revision mismatch".format(role))
            try:
                with open(str(source_path), "r", encoding="utf-8") as source_stream:
                    source_payload = json.load(source_stream)
            except (OSError, ValueError, TypeError) as exc:
                errors.append("{} source performance artifact is unreadable: {}".format(role, exc))
                continue
            source_payloads[role] = source_payload
            if not isinstance(source_payload, dict) or source_payload.get("status") != "PASSED":
                errors.append("{} source performance artifact is not PASSED".format(role))
                continue
            if source_payload.get("evidence_class") != "release_performance_revision" or \
                    source_payload.get("comparison_type") != "single_revision":
                errors.append("{} source performance artifact is not an independent revision run".format(role))
            if source_payload.get("revision") != expected_revision:
                errors.append("{} source performance payload revision mismatch".format(role))
            if source_payload.get("workload_id") != payload.get("workload_id") or \
                    source_payload.get("workload_hash") != payload.get("workload_hash"):
                errors.append("{} source performance workload identity mismatch".format(role))
            if source_payload.get("environment_identity") != payload.get("environment_identity"):
                errors.append("{} source performance environment identity mismatch".format(role))

    # Re-check the combined timings against the hashed source JSON. A modified
    # summary must not be able to retain valid source hashes while changing the
    # displayed measurements or PASS status.
    for role, source_payload in source_payloads.items():
        source_tiers = source_payload.get("tiers") or source_payload
        for tier_name in ("Tier_A_1k", "Tier_B_10k", "Tier_C_50k", "Tier_D_100k"):
            source_tier = source_tiers.get(tier_name) if isinstance(source_tiers, dict) else None
            combined_tier = payload.get(tier_name) or {}
            if not isinstance(source_tier, dict) or not isinstance(combined_tier, dict):
                continue
            measured = source_tier.get("measured_ms")
            expected_field = "baseline_ms" if role == "baseline" else "candidate_ms"
            observed = combined_tier.get(expected_field)
            if not isinstance(measured, (int, float)) or not isinstance(observed, (int, float)) or \
                    round(float(measured), 3) != round(float(observed), 3):
                errors.append("{} source timing mismatch for {}".format(role, tier_name))
        source_virtual = source_payload.get("coverage_virtual_scroll_100k") or {}
        combined_virtual = payload.get("coverage_virtual_scroll_100k") or {}
        expected_virtual_field = "baseline_elapsed_ms" if role == "baseline" else "candidate_elapsed_ms"
        if isinstance(source_virtual, dict) and isinstance(combined_virtual, dict):
            measured = source_virtual.get("elapsed_ms")
            observed = combined_virtual.get(expected_virtual_field)
            if not isinstance(measured, (int, float)) or not isinstance(observed, (int, float)) or \
                    round(float(measured), 3) != round(float(observed), 3):
                errors.append("{} source timing mismatch for 100k virtual-scroll".format(role))

    if not path or not os.path.isfile(path):
        errors.append("release performance artifact path is missing")
    return errors


def _normalize_evidence_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _validate_candidate_browser_evidence(path: str, payload: Dict[str, Any],
                                         identity: Dict[str, Any],
                                         expected_url: str) -> Tuple[List[str], Dict[str, Any]]:
    """Validate the external real-Candidate browser evidence envelope."""
    errors = []
    if not isinstance(payload, dict):
        return ["Candidate browser evidence is not a JSON object"], {}
    if payload.get("status") != "PASSED":
        errors.append("Candidate browser evidence status is not PASSED")
    if payload.get("evidence_class") != "real_http_chromium_browser":
        errors.append("Candidate browser evidence is not real_http_chromium_browser")
    if payload.get("release_eligible") is not True:
        errors.append("Candidate browser evidence is not release eligible")
    if payload.get("synthetic") is not False:
        errors.append("synthetic Candidate browser evidence is forbidden")
    target_revision = identity.get("commit_sha")
    if payload.get("candidate_revision") != target_revision:
        errors.append("Candidate browser revision does not match target release")
    candidate_url = payload.get("candidate_url") or payload.get("page_url")
    if not candidate_url:
        errors.append("Candidate browser evidence URL is missing")
    elif not _normalize_evidence_url(candidate_url).startswith(("http:", "https:")):
        errors.append("Candidate browser evidence URL is not HTTP(S)")
    if not expected_url:
        errors.append("upgrade.candidate_browser_url is required")
    elif _normalize_evidence_url(candidate_url) != _normalize_evidence_url(expected_url):
        errors.append("Candidate browser URL does not match configured candidate_browser_url")

    served_identity = payload.get("release_identity") or {}
    if not isinstance(served_identity, dict):
        errors.append("Candidate browser served release identity is missing")
        served_identity = {}
    for key in (
            "version", "commit_sha", "build_id", "asset_hash", "schema_version",
            "asset_manifest_version", "asset_count", "asset_manifest_hash",
            "asset_manifest"):
        if identity.get(key) not in (None, "") and served_identity.get(key) != identity.get(key):
            errors.append("Candidate browser served release identity mismatch: {}".format(key))

    functional = payload.get("browser_functional") or {}
    workload = payload.get("coverage_virtual_scroll_100k") or {}
    environment = workload.get("environment_identity") or {}
    if functional.get("status") != "PASSED":
        errors.append("Candidate browser functional HTTP check is not PASSED")
    if workload.get("status") != "PASSED":
        errors.append("Candidate browser workload is not PASSED")
    if environment.get("browser_name") != "chromium":
        errors.append("Candidate browser evidence does not identify Chromium")

    artifact_path = payload.get("artifact_path") or payload.get("report_artifact_path")
    artifact_sha = payload.get("artifact_sha256") or payload.get("report_artifact_sha256")
    if not artifact_path or not os.path.isabs(str(artifact_path)) or \
            not os.path.isfile(str(artifact_path)):
        errors.append("Candidate browser workload artifact is missing")
        artifact_path = ""
        artifact_sha = ""
    else:
        actual_sha = _sha256_file(str(artifact_path))
        if not artifact_sha or str(artifact_sha) != actual_sha:
            errors.append("Candidate browser workload artifact SHA256 mismatch")
            artifact_sha = actual_sha

    normalized = {
        "status": "PASSED" if not errors else "FAILED",
        "evidence_class": "real_candidate_browser",
        "source_evidence_class": payload.get("evidence_class", ""),
        "candidate_url": candidate_url or "",
        "expected_commit_sha": target_revision,
        "served_release_identity": served_identity,
        "browser_artifact_path": artifact_path,
        "browser_artifact_sha256": artifact_sha,
        "real_http": payload.get("evidence_class") == "real_http_chromium_browser" and \
            _normalize_evidence_url(candidate_url).startswith(("http:", "https:")),
        "chromium": environment.get("browser_name") == "chromium",
        "synthetic": payload.get("synthetic"),
        "release_eligible": payload.get("release_eligible"),
        "workload_id": workload.get("workload_id", ""),
        "workload_status": workload.get("status", ""),
        "violations": errors,
    }
    return errors, normalized


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
    def __init__(self, repo_root: Optional[str] = None,
                 backup_root: Optional[str] = None):
        self.repo_root = os.path.realpath(repo_root or _REPO_ROOT)
        self.manifest = ProductionEvidenceManifest(self.repo_root)
        self.backup_dir = resolve_backup_root(self.repo_root, backup_root)
        self.publisher = None
        self.candidate_root = ""
        self.publish_root = ""
        self.validation_session = None
        self.validation_session_manifest_path = ""
        self.validation_teardown_evidence_path = ""
        self.serving_session_manifest_path = ""
        self.serving_teardown_evidence_path = ""
        self.serving_session_id = ""
        self.release_validation_session_id = ""
        self.previous_published_session_id = ""
        self._target_identity = {}
        self._upgrade_mode = ""
        self._runtime_config = {}
        self._validation_teardown_result = None
        self.logs: List[str] = []
        self._publication_switched = False

    def _configure_backup_root(self, configured_root: Optional[str]):
        resolved = resolve_backup_root(self.repo_root, configured_root)
        if resolved == self.backup_dir:
            return
        if self._publication_switched:
            raise RuntimeError("backup root cannot change after file cutover")
        self.backup_dir = resolved

    def _configure_release_controls(self, upgrade_config: Dict[str, Any],
                                    identity: Dict[str, Any],
                                    previous_release: Dict[str, Any],
                                    runtime_config: Dict[str, Any]):
        """Bind the upgrade to immutable publication and an owned session.

        The active checkout is not a publication target.  The only mutable
        release operation permitted here is an atomic ``CURRENT`` pointer
        switch performed by ``ImmutableReleasePublisher``.
        """
        candidate_root = _resolve_upgrade_path(
            self.repo_root, upgrade_config.get("candidate_root"), "candidate_root"
        )
        publish_root = _resolve_upgrade_path(
            self.repo_root, upgrade_config.get("publish_root"), "publish_root"
        )
        if _path_is_within(publish_root, self.repo_root):
            raise RuntimeError("upgrade.publish_root must be outside the active deployment root")
        if _path_is_within(publish_root, candidate_root) or \
                _path_is_within(candidate_root, publish_root):
            raise RuntimeError("upgrade.publish_root and candidate_root must be separate")

        self.candidate_root = candidate_root
        self.publish_root = publish_root
        self.publisher = ImmutableReleasePublisher(publish_root)
        current = self.publisher.validate_current()
        if current.get("status") != "PASSED":
            raise RuntimeError(
                "CURRENT does not point to a validated immutable release: {}".format(
                    "; ".join(current.get("violations") or [])
                )
            )
        if current.get("commit_sha") != previous_release.get("commit_sha"):
            raise RuntimeError(
                "CURRENT release does not match previous_release commit_sha"
            )
        self.previous_published_session_id = self.publisher.current_session_id()
        if not self.previous_published_session_id:
            raise RuntimeError("CURRENT release-validation session identity is missing")

        session_id = _new_release_validation_session_id(
            identity.get("commit_sha"),
            upgrade_config.get("release_validation_session_id") or
            upgrade_config.get("validation_session_id"),
        ).strip()
        try:
            self.publisher.release_path(session_id)
        except ValueError as exc:
            raise RuntimeError("validation session id is invalid: {}".format(exc))
        self.release_validation_session_id = session_id

        self.validation_session_manifest_path = _resolve_attempt_path(
            self.repo_root,
            upgrade_config.get("validation_session_manifest"),
            "validation_session_manifest",
            session_id,
        )
        configured_teardown = upgrade_config.get("validation_teardown_evidence_path")
        teardown_config = configured_teardown or (
            self.validation_session_manifest_path + ".teardown.json"
        )
        self.validation_teardown_evidence_path = _resolve_attempt_path(
            self.repo_root,
            teardown_config,
            "validation_teardown_evidence_path",
            session_id,
        )
        if self.validation_teardown_evidence_path == self.validation_session_manifest_path:
            raise RuntimeError(
                "validation teardown evidence must not overwrite the session manifest"
            )

        if os.path.isfile(self.validation_session_manifest_path):
            session = ValidationSession.load(self.validation_session_manifest_path)
            if session.data.get("session_id") != session_id:
                raise RuntimeError("validation session id does not match manifest")
        else:
            server = runtime_config.get("server") or {}
            configured_ports = upgrade_config.get("validation_ports")
            if configured_ports is None:
                configured_ports = [server.get("port")]
            if isinstance(configured_ports, (str, int)):
                configured_ports = [configured_ports]
            try:
                ports = sorted(set(int(port) for port in configured_ports if port))
            except (TypeError, ValueError):
                raise RuntimeError("upgrade.validation_ports must contain integers")
            if not ports:
                raise RuntimeError(
                    "upgrade.validation_ports must identify at least one owned port"
                )
            session = ValidationSession.create(
                self.validation_session_manifest_path,
                session_id,
                candidate_sha=identity.get("commit_sha"),
                baseline_sha=previous_release.get("commit_sha"),
                ports=ports,
                evidence_paths=[self.validation_teardown_evidence_path],
            )

        if int(session.data.get("schema_version") or 0) != SESSION_SCHEMA_VERSION:
            raise RuntimeError("validation session schema version is unsupported")
        if not session.data.get("session_id"):
            raise RuntimeError("validation session id is missing")
        if session.data.get("candidate_sha") != identity.get("commit_sha"):
            raise RuntimeError("validation session candidate SHA mismatches target release")
        if session.data.get("baseline_sha") != previous_release.get("commit_sha"):
            raise RuntimeError("validation session baseline SHA mismatches previous release")
        if session.data.get("teardown_status") == "PASSED":
            raise RuntimeError("validation session has already been torn down")
        if not session.data.get("ports"):
            raise RuntimeError("validation session has no owned validation ports")
        self.validation_session = session

        # Local staging controls consume these values without allowing a
        # static command line to accidentally bind the wrong release session.
        os.environ["COVERAGE_VALIDATION_SESSION_MANIFEST"] = \
            self.validation_session_manifest_path
        os.environ["COVERAGE_VALIDATION_SESSION_ID"] = str(
            session.data.get("session_id")
        )
        os.environ["COVERAGE_VALIDATION_CANDIDATE_SHA"] = str(
            identity.get("commit_sha")
        )
        os.environ["COVERAGE_VALIDATION_BASELINE_SHA"] = str(
            previous_release.get("commit_sha")
        )
        os.environ["COVERAGE_VALIDATION_TEARDOWN_EVIDENCE"] = \
            self.validation_teardown_evidence_path

    def _teardown_validation_session(self, identity: Dict[str, Any], mode: str):
        if self.validation_session is None:
            return None
        if self._validation_teardown_result is not None:
            return self._validation_teardown_result

        try:
            result = self.validation_session.teardown(
                evidence_path=self.validation_teardown_evidence_path,
                timeout=float(
                    ((self._runtime_config or {}).get("upgrade") or {}).get(
                        "validation_teardown_timeout_sec", 10
                    )
                ),
            )
        except Exception as exc:
            result = {
                "schema_version": SESSION_SCHEMA_VERSION,
                "session_id": self.validation_session.data.get("session_id", ""),
                "status": "FAILED",
                "pids_closed": False,
                "ports_closed": False,
                "ports_probe_ok": False,
                "attempted": [],
                "verification": {},
                "violations": ["ValidationSession teardown failed: {}".format(exc)],
                "p1": True,
            }
            self.log("❌ Validation session teardown failed: {}".format(exc))

        verification = result.get("verification") or {}
        result["pids_closed"] = verification.get("pids_closed") is True
        result["ports_closed"] = verification.get("ports_closed") is True
        result["ports_probe_ok"] = verification.get("ports_probe_ok") is True
        result.update({
            "revision": identity.get("commit_sha"),
            "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
            "command": "ValidationSession.teardown",
            "exit_code": 0 if result.get("status") == "PASSED" else 1,
            "artifact_path": self.validation_teardown_evidence_path,
        })
        self.manifest.record("validation_teardown", result)

        session_record = dict(self.validation_session.data)
        session_record.update({
            "status": "PASSED" if (
                session_record.get("session_id") and
                session_record.get("candidate_sha") == identity.get("commit_sha") and
                session_record.get("baseline_sha")
            ) else "FAILED",
            "revision": identity.get("commit_sha"),
            "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
            "command": "ValidationSession.load",
            "exit_code": 0,
            "artifact_path": self.validation_session_manifest_path,
        })
        self.manifest.record("validation_session_manifest", session_record)
        self._validation_teardown_result = result
        return result

    def _activate_final_serving_session(self):
        """Bind the final serving process to a session separate from validation."""
        if self.validation_session is None:
            raise RuntimeError("validation session is required before final serving")
        validation_id = str(self.validation_session.data.get("session_id") or "")
        if not validation_id:
            raise RuntimeError("validation session identity is missing")
        serving_id = "serving-{}".format(validation_id)
        if len(serving_id) > 128:
            serving_id = "serving-{}".format(
                hashlib.sha256(validation_id.encode("utf-8")).hexdigest()
            )
        serving_manifest = self.validation_session_manifest_path + ".serving.json"
        serving_teardown = self.validation_teardown_evidence_path + ".serving.json"
        if os.path.lexists(serving_manifest):
            raise RuntimeError(
                "final serving session manifest already exists: {}".format(
                    serving_manifest
                )
            )
        self.serving_session_id = serving_id
        self.serving_session_manifest_path = serving_manifest
        self.serving_teardown_evidence_path = serving_teardown
        # local_staging_control consumes the serving namespace in preference
        # to the validation namespace.  Production supervisors may ignore
        # these values, but the lifecycle command remains explicitly distinct.
        os.environ["COVERAGE_SERVING_SESSION_MANIFEST"] = serving_manifest
        os.environ["COVERAGE_SERVING_SESSION_ID"] = serving_id
        os.environ["COVERAGE_SERVING_CANDIDATE_SHA"] = str(
            self.validation_session.data.get("candidate_sha") or ""
        )
        os.environ["COVERAGE_SERVING_BASELINE_SHA"] = str(
            self.validation_session.data.get("baseline_sha") or ""
        )
        os.environ["COVERAGE_SERVING_TEARDOWN_EVIDENCE"] = serving_teardown

    def log(self, msg: str):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}")
        self.logs.append(msg)
        self.manifest.add_log(msg)

    def _mark_not_ready(self):
        """Make every failed/unfinished execution explicitly NOT_READY."""
        self.manifest.data["release_decision"] = "NOT_READY"
        if self.manifest.data.get("status") == "UPGRADE_SUCCESS":
            self.manifest.data["status"] = "UNMET_GATES"
        self.manifest.save()

    def _fail(self, lifecycle: Optional[UpgradeLifecycle], message: str) -> Tuple[bool, str]:
        self._mark_not_ready()
        if self._publication_switched:
            try:
                if self.publisher is None or not self.previous_published_session_id:
                    raise RuntimeError("immutable rollback identity is unavailable")
                rollback = self.publisher.rollback(self.previous_published_session_id)
                if rollback.get("status") != "PASSED":
                    raise RuntimeError("immutable rollback did not pass")
                self._publication_switched = False
                self.log("✔ Immutable CURRENT pointer rolled back before traffic was opened.")
            except Exception as exc:
                self.log("❌ DATA_SAFETY_HOLD: immutable publication rollback failed: {}".format(exc))
        if lifecycle is not None and lifecycle.active:
            try:
                rollback_result = lifecycle.abort()
                self.log("✔ Upgrade lifecycle aborted and previous API restored: {}".format(
                    rollback_result.get("previous_release_verified", False)))
            except Exception as exc:
                self.log("❌ DATA_SAFETY_HOLD: lifecycle abort failed: {}".format(exc))
        if self.validation_session is not None:
            try:
                teardown = self._teardown_validation_session(
                    self._target_identity, self._upgrade_mode
                )
                if teardown and teardown.get("status") != "PASSED":
                    self.log("❌ Release NOT_READY: validation session teardown did not pass.")
            except Exception as exc:
                self.log("❌ DATA_SAFETY_HOLD: validation session teardown evidence failed: {}".format(exc))
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
        for key in (
                "version", "commit_sha", "build_id", "asset_hash", "schema_version",
                "asset_manifest_version", "asset_count", "asset_manifest_hash",
                "asset_manifest"):
            if actual.get(key) != identity.get(key):
                raise RuntimeError("release endpoint mismatch: {}".format(key))
        return {
            "status": "PASSED", "evidence_class": "staging_cutover",
            "endpoint": endpoint, "release": actual,
            "command": "GET {}".format(endpoint), "exit_code": 0,
        }

    def _verify_health_endpoint(self, endpoint: str) -> Dict[str, Any]:
        if not endpoint:
            raise RuntimeError("upgrade.health_endpoint is required")
        with urllib.request.urlopen(endpoint, timeout=10) as response:
            status = int(getattr(response, "status", 200))
            payload = json.loads(response.read().decode("utf-8"))
        if status != 200:
            raise RuntimeError("health endpoint returned HTTP {}".format(status))
        if not isinstance(payload, dict) or payload.get("status") not in (
                "ok", "healthy", "PASSED"):
            raise RuntimeError("health endpoint did not report a healthy runtime")
        return {
            "status": "PASSED", "evidence_class": "staging_cutover",
            "endpoint": endpoint, "health": payload,
            "command": "GET {}".format(endpoint), "exit_code": 0,
        }

    def _verify_post_open_serving(self, identity: Dict[str, Any]) -> Dict[str, Any]:
        """Verify the process serving traffic after the traffic switch."""
        upgrade_config = ((self._runtime_config or {}).get("upgrade") or {})
        release_endpoint = upgrade_config.get("release_endpoint")
        health_endpoint = upgrade_config.get("health_endpoint")
        release = self._verify_release_endpoint(release_endpoint, identity)
        health = self._verify_health_endpoint(health_endpoint)
        if self.publisher is None or self.validation_session is None:
            raise RuntimeError("immutable publisher and validation session are required")
        current = self.publisher.validate_current()
        if current.get("status") != "PASSED":
            raise RuntimeError(
                "CURRENT failed post-open validation: {}".format(
                    "; ".join(current.get("violations") or [])
                )
            )
        expected_session = self.validation_session.data.get("session_id")
        if current.get("commit_sha") != identity.get("commit_sha"):
            raise RuntimeError("post-open CURRENT commit does not match target release")
        if current.get("release_validation_session_id") != expected_session:
            raise RuntimeError("post-open CURRENT session does not match target release")
        return {
            "status": "PASSED",
            "revision": identity.get("commit_sha"),
            "evidence_class": "staging_cutover" if self._upgrade_mode == "staging" else "production_cutover",
            "process_role": "production_serving",
            "release_endpoint": release,
            "health_endpoint": health,
            "served_root": current.get("served_root"),
            "current_release_validation_session_id": current.get("release_validation_session_id"),
            "expected_release_validation_session_id": expected_session,
            "publisher_current_validation": current,
            "command": "GET release + GET health + ImmutableReleasePublisher.validate_current",
            "exit_code": 0,
        }

    def execute_upgrade(self, dry_run: bool = False, connection=None, db_config=None,
                        mode: str = "staging", deployment_manifest: Optional[str] = None,
                        target_release: Optional[Dict[str, Any]] = None,
                        runtime_config: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """Run complete upgrade procedure."""
        self._runtime_config = dict(runtime_config or {})
        self._upgrade_mode = mode
        self._mark_not_ready()
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
        self._target_identity = dict(identity)
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
        self._configure_backup_root(upgrade_config.get("backup_root"))
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
        try:
            self._configure_release_controls(
                upgrade_config, identity, previous_release, self._runtime_config
            )
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            self.log("❌ Immutable publication/session preflight failed: {}".format(exc))
            return False, "Immutable publication/session preflight failed"
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
        backup_db_config = dict(db_config or {"database": "coverage_tool"})
        configured_deploy_roots = list(
            backup_db_config.get("deployment_roots") or []
        )
        configured_deploy_roots.append(self.repo_root)
        for root_key in ("current_root", "candidate_root", "deployment_root"):
            if upgrade_config.get(root_key):
                configured_deploy_roots.append(upgrade_config.get(root_key))
        backup_db_config["deployment_roots"] = configured_deploy_roots
        ok_bk, bk_manifest, bk_err = perform_database_backup(
            db_config=backup_db_config,
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
            stop_evidence = lifecycle.stop_current_api()
            stop_evidence.update({
                "revision": identity.get("commit_sha"),
                "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
                "process_role": "pre_upgrade_baseline",
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

        # The deployment manifest remains a review record, while publication
        # itself is exclusively an immutable artifact preparation followed by
        # one atomic CURRENT pointer switch.  The active checkout is never
        # rewritten by the production upgrade path.
        self.log("[Cutover] Preparing immutable release and switching CURRENT atomically...")
        try:
            session_id = self.validation_session.data.get("session_id")
            prepared = self.publisher.prepare(
                self.candidate_root,
                identity,
                session_id,
                api_contract_version=upgrade_config.get("api_contract_version", ""),
                candidate_sha=identity.get("commit_sha"),
                candidate_artifact_manifest=upgrade_config.get(
                    "candidate_artifact_manifest", ""
                ),
            )
            switched = self.publisher.switch_current(session_id)
            if switched.get("status") != "PASSED":
                raise RuntimeError("immutable CURRENT switch did not pass")
            self._publication_switched = True
            self.manifest.record("file_cutover", {
                "status": "PASSED",
                "revision": identity.get("commit_sha"),
                "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
                "action_count": len(actions),
                "publication_mode": "immutable_release",
                "candidate_root": self.candidate_root,
                "publish_root": self.publish_root,
                "release_root": self.publisher.release_path(session_id),
                "previous_session_id": self.previous_published_session_id,
                "current_session_id": session_id,
                "release_manifest": prepared,
                "switch": switched,
                "command": "ImmutableReleasePublisher.prepare + switch_current",
                "exit_code": 0,
            })
        except Exception as exc:
            self.log("❌ Immutable release publication failed: {}".format(exc))
            return self._fail(lifecycle, "Immutable release publication failed")

        try:
            start_evidence = lifecycle.start_api()
            start_evidence.update({
                "revision": identity.get("commit_sha"),
                "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
                "process_role": "validation_candidate",
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
        # This Playwright suite is a fixture regression only.  It must never
        # be recorded as evidence of the externally served Candidate.
        browser_cmd = ["npm", "run", "test:browser", "--", "--reporter=line"]
        browser_fixture = subprocess.run(
            browser_cmd, cwd=self.repo_root, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        fixture_status = "PASSED" if browser_fixture.returncode == 0 else "FAILED"
        self.manifest.record("browser_fixture_regression", {
            "status": fixture_status,
            "revision": identity.get("commit_sha"),
            "evidence_class": "browser_fixture_regression",
            "synthetic": True,
            "suite": "tests/browser/coverage_real_browser.spec.js",
            "command": " ".join(browser_cmd),
            "exit_code": browser_fixture.returncode,
        })
        if browser_fixture.returncode != 0:
            err_text = browser_fixture.stderr.decode("utf-8", errors="ignore")
            self.log("❌ Browser fixture regression failed: {}".format(err_text))
            return self._fail(lifecycle, "Browser fixture regression failed")
        self.log("✔ Browser fixture regression passed.")

        # Production authority is an external artifact generated by
        # real_browser_evidence.js against the actual Candidate URL.  The
        # fixture command above is intentionally not consulted for this gate.
        self.log("[Step 6b/10] Validating external real Candidate browser evidence...")
        configured_browser_evidence = upgrade_config.get("candidate_browser_evidence_path")
        browser_evidence_path = configured_browser_evidence or os.environ.get(
            "COVERAGE_CANDIDATE_BROWSER_EVIDENCE", ""
        )
        if browser_evidence_path and not os.path.isabs(str(browser_evidence_path)):
            browser_evidence_path = os.path.join(self.repo_root, str(browser_evidence_path))
        browser_evidence_path = os.path.realpath(str(browser_evidence_path or ""))
        expected_browser_url = str(upgrade_config.get("candidate_browser_url") or "")
        browser_payload = {}
        browser_errors = []
        if not browser_evidence_path or not os.path.isfile(browser_evidence_path):
            browser_errors.append(
                "upgrade.candidate_browser_evidence_path must point to real Candidate evidence"
            )
        else:
            try:
                with open(browser_evidence_path, "r", encoding="utf-8") as browser_stream:
                    browser_payload = json.load(browser_stream)
            except (OSError, ValueError, TypeError) as exc:
                browser_errors.append("Candidate browser evidence is unreadable: {}".format(exc))
        if not browser_errors:
            browser_errors, normalized_browser = _validate_candidate_browser_evidence(
                browser_evidence_path, browser_payload, identity, expected_browser_url
            )
        else:
            normalized_browser = {
                "status": "FAILED",
                "evidence_class": "real_candidate_browser",
                "candidate_url": expected_browser_url,
                "expected_commit_sha": identity.get("commit_sha"),
                "served_release_identity": {},
                "browser_artifact_path": "",
                "browser_artifact_sha256": "",
                "real_http": False,
                "chromium": False,
                "synthetic": False,
                "release_eligible": False,
                "violations": browser_errors,
            }
        normalized_browser.update({
            "revision": identity.get("commit_sha"),
            "artifact_path": browser_evidence_path,
            "command": "validate real_browser_evidence.js --browser-evidence-output",
            "exit_code": 0 if not browser_errors else 1,
        })
        if browser_evidence_path and os.path.isfile(browser_evidence_path):
            normalized_browser["evidence_artifact_sha256"] = _sha256_file(browser_evidence_path)
        self.manifest.record("candidate_browser_evidence", normalized_browser)
        if browser_errors:
            self.log("❌ Real Candidate browser evidence rejected: {}".format(
                "; ".join(browser_errors)
            ))
            return self._fail(lifecycle, "Real Candidate browser evidence rejected")
        self.log("✔ Real Candidate browser evidence passed.")

        # Step 7: Consume immutable release A/B evidence.  The synthetic DOM
        # benchmark remains a useful diagnostic, but it is not allowed to
        # create a production performance claim inside the upgrade runner.
        self.log("[Step 7/10] Validating exact-revision Performance A/B Evidence...")
        configured_perf = upgrade_config.get("performance_evidence_path")
        configured_perf = configured_perf or os.environ.get("COVERAGE_RELEASE_PERFORMANCE_AB")
        perf_artifact = ""
        if configured_perf:
            perf_artifact = str(configured_perf)
            if not os.path.isabs(perf_artifact):
                perf_artifact = os.path.join(self.repo_root, perf_artifact)
            perf_artifact = os.path.realpath(perf_artifact)
        perf_cmd = "validate release_performance_ab artifact {}".format(perf_artifact or "<missing>")
        perf_res = {}
        perf_errors = []
        if not perf_artifact or not os.path.isfile(perf_artifact):
            perf_errors.append("upgrade.performance_evidence_path must point to a release A/B artifact")
        else:
            try:
                with open(perf_artifact, "r", encoding="utf-8") as perf_stream:
                    perf_res = json.load(perf_stream)
            except (OSError, ValueError) as exc:
                perf_errors.append("release performance artifact is unreadable: {}".format(exc))
        if not perf_errors:
            perf_errors.extend(_validate_release_performance_artifact(
                perf_artifact, perf_res, identity.get("commit_sha")
            ))
        if perf_errors:
            invalid_perf = dict(perf_res) if isinstance(perf_res, dict) else {}
            invalid_perf.update({
                "status": "FAILED",
                "evidence_class": "release_performance_ab",
                "revision": identity.get("commit_sha"),
                "command": perf_cmd,
                "exit_code": 1,
                "artifact_path": perf_artifact,
                "violations": perf_errors,
            })
            self.manifest.record("performance_benchmark", invalid_perf)
            self.log("❌ Release performance A/B evidence rejected: {}".format("; ".join(perf_errors)))
            return self._fail(lifecycle, "Release performance A/B evidence rejected")

        perf_res["revision"] = identity.get("commit_sha")
        perf_res["command"] = perf_cmd
        perf_res["exit_code"] = 0
        perf_res["artifact_path"] = perf_artifact
        for tier_name in ("Tier_A_1k", "Tier_B_10k", "Tier_C_50k", "Tier_D_100k"):
            tier = perf_res.get(tier_name)
            if isinstance(tier, dict):
                tier["revision"] = identity.get("commit_sha")
                tier["evidence_class"] = "release_performance_ab"
                tier["command"] = perf_cmd
                tier["exit_code"] = 0
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
            # LCOV/external path identities containing parent traversal are
            # rejected by the canonical resolver rather than folded into a
            # different path.
            queries.append(("../" + first, "invalid_path"))
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

        # Teardown is a hard release prerequisite.  This runs before the
        # final evidence read and records both the session manifest and the
        # owned-process/port closure result.
        teardown = self._teardown_validation_session(identity, mode)
        if not teardown or teardown.get("status") != "PASSED":
            self.log("❌ Release NOT_READY: validation session teardown is incomplete.")
            return self._fail(lifecycle, "Validation session teardown failed")

        # The validation candidate is now gone by construction.  Start the
        # final serving process under a different session/command so a later
        # validation teardown can never kill the process that will receive
        # production traffic.
        lifecycle.api_started = False
        try:
            self._activate_final_serving_session()
            serving_start = lifecycle.start_serving_api()
            serving_start.update({
                "revision": identity.get("commit_sha"),
                "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
                "process_role": "production_serving",
                "serving_session_id": self.serving_session_id,
                "serving_session_manifest": self.serving_session_manifest_path,
            })
            # api_start and release_endpoint represent the process that will
            # be served, rather than the already-torn-down validation process.
            self.manifest.record("api_start", serving_start)
            endpoint = ((runtime_config or {}).get("upgrade") or {}).get("release_endpoint")
            endpoint_evidence = self._verify_release_endpoint(endpoint, identity)
            endpoint_evidence.update({
                "revision": identity.get("commit_sha"),
                "process_role": "production_serving",
            })
            self.manifest.record("release_endpoint", endpoint_evidence)
        except Exception as exc:
            self.log("❌ Final serving API verification failed: {}".format(exc))
            return self._fail(lifecycle, "Final serving API verification failed")

        # Step 10: Validate Final Production Release Governance Gate
        self.log("[Step 10/10] Validating Production Release Governance Gate...")
        gate_passed, unmet = self.manifest.validate_final_gate(
            require_traffic_open=False, require_post_open_serving=False
        )
        if not gate_passed:
            return self._fail(lifecycle, f"Final gate unmet: {unmet}")

        try:
            open_evidence = lifecycle.open_traffic()
            open_evidence.update({
                "revision": identity.get("commit_sha"),
                "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
            })
            self.manifest.record("traffic_open", open_evidence)
            post_open = self._verify_post_open_serving(identity)
            self.manifest.record("post_open_serving", post_open)
            final_open_gate, open_unmet = self.manifest.validate_final_gate(
                require_traffic_open=True, require_post_open_serving=True
            )
            if not final_open_gate:
                self.log("❌ Post-open evidence gate failed: {}".format(open_unmet))
                return self._fail(lifecycle, "Post-open evidence gate failed")
            finalized = lifecycle.finalize_traffic_open()
            open_evidence["finalize"] = finalized
            self.manifest.record("traffic_open", open_evidence)
        except Exception as exc:
            self.manifest.record("post_open_serving", {
                "status": "FAILED",
                "revision": identity.get("commit_sha"),
                "evidence_class": "staging_cutover" if mode == "staging" else "production_cutover",
                "process_role": "production_serving",
                "command": "GET release + GET health + ImmutableReleasePublisher.validate_current",
                "exit_code": 1,
                "violations": [str(exc)],
            })
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
    orchestrator = UpgradeOrchestrator(
        backup_root=(config.get("upgrade") or {}).get("backup_root")
    )
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
