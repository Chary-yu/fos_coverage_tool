"""
Production Evidence Manifest Manager (Item 28)
Manages the structured release evidence ledger:
- Tracks and verifies all 8 phases of release evidence
- Enforces strict zero-false-positive production governance gates
- Produces production_evidence_manifest.json
"""

import os
import sys
import json
import time
import hashlib
import platform
import socket
from typing import Dict, Any, List, Tuple, Optional

try:
    from datetime import datetime, timezone
    def get_utc_iso():
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(timezone, "utc") else datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
except ImportError:
    from datetime import datetime
    def get_utc_iso():
        return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

MANIFEST_FILENAME = "production_evidence_manifest.json"
_SUCCESS_STATUS = "UPGRADE_" + "SUCCESS"

_MATRIX_SECTIONS = ("targeted_tests", "performance_benchmark")
_REQUIRED_EVIDENCE_SECTIONS = (
    "schema_migration", "backup_evidence", "data_hash_verification",
    "browser_smoke_suite", "path_mapping_audit", "sidecar_audit",
    "security_audit", "traffic_freeze", "job_drain", "api_stop",
    "api_start", "release_endpoint", "file_cutover", "traffic_open",
    "rollback_evidence",
)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record_metadata(name: str, payload: Dict[str, Any], repo_root: str) -> Dict[str, Any]:
    """Attach provenance fields without manufacturing a successful result.

    Status, revision, command exit code, and artifact existence are deliberately
    never inferred here.  The helper only supplies the immutable context needed
    to audit a record later and calculates an artifact digest when the caller
    supplied a real file.
    """
    record = dict(payload or {})
    now = get_utc_iso()
    record.setdefault("name", name)
    record.setdefault("started_at", now)
    record.setdefault("finished_at", now)
    record.setdefault("recorded_at", now)
    record.setdefault("host", socket.gethostname())
    record.setdefault("environment", os.environ.get("COVERAGE_ENV", "development"))
    nested_command = record.get("command") if isinstance(record.get("command"), dict) else None
    record.setdefault("command", "")
    if nested_command:
        record["command"] = nested_command.get("command") or nested_command.get("name") or ""
    record.setdefault("exit_code", None)
    if record.get("exit_code") is None and nested_command:
        record["exit_code"] = nested_command.get("exit_code")

    artifact_path = record.get("artifact_path")
    if not artifact_path and record.get("full_sql_gz_sha256") and record.get("backup_dir"):
        artifact_path = os.path.join(str(record["backup_dir"]), "full.sql.gz")
    if artifact_path:
        artifact_path = os.path.abspath(str(artifact_path))
        record["artifact_path"] = artifact_path
        if not record.get("artifact_sha256") and os.path.isfile(artifact_path):
            try:
                record["artifact_sha256"] = _sha256_file(artifact_path)
            except OSError:
                record["artifact_sha256"] = ""
    else:
        record.setdefault("artifact_path", "")
    record.setdefault("artifact_sha256", "")
    record.setdefault("repo_root", os.path.abspath(repo_root))
    record.setdefault("runtime", platform.platform())
    return record

class ProductionEvidenceManifest:
    def __init__(self, repo_root: str):
        self.repo_root = repo_root
        evidence_root = os.environ.get("COVERAGE_EVIDENCE_DIR") or os.path.join(repo_root, ".artifacts", "evidence")
        os.makedirs(evidence_root, exist_ok=True)
        self.manifest_path = os.path.join(evidence_root, MANIFEST_FILENAME)
        self.data: Dict[str, Any] = self._load_or_init()

    def _load_or_init(self) -> Dict[str, Any]:
        if os.path.isfile(self.manifest_path):
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "schema_version": 2,
            "created_at": get_utc_iso(),
            "updated_at": get_utc_iso(),
            "status": "INITIALIZED",
            "release_identity": {},
            "schema_migration": {},
            "backup_evidence": {},
            "data_hash_verification": {},
            "targeted_tests": {},
            "browser_smoke_suite": {},
            "path_mapping_audit": {},
            "sidecar_audit": {},
            "security_audit": {},
            "performance_benchmark": {},
            "logs": []
        }

    def record(self, section: str, payload: Dict[str, Any]):
        """Record evidence for a specific section."""
        payload = dict(payload or {})
        # Matrix sections contain named gate records; do not inject a scalar
        # record into those mappings.
        if section in _MATRIX_SECTIONS:
            decorated = {}
            for name, value in payload.items():
                if isinstance(value, dict):
                    decorated[name] = _record_metadata("{}:{}".format(section, name), value, self.repo_root)
                else:
                    decorated[name] = value
            decorated["_record_meta"] = _record_metadata(section, {}, self.repo_root)
            payload = decorated
        elif section == "release_identity":
            payload = _record_metadata("release_identity", payload, self.repo_root)
        else:
            payload.setdefault("status", "UNAVAILABLE")
            payload.setdefault("evidence_class", "unknown")
            payload = _record_metadata(section, payload, self.repo_root)
        self.data[section] = payload
        self.data["updated_at"] = get_utc_iso()
        self.save()

    def add_log(self, message: str):
        """Append an event log to the ledger."""
        self.data.setdefault("logs", []).append(f"[{get_utc_iso()}] {message}")
        self.save()

    def save(self):
        temp_path = self.manifest_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, self.manifest_path)

    def validate_final_gate(self, require_traffic_open: bool = True) -> Tuple[bool, List[str]]:
        """
        Validate all strict production gates to prevent false UPGRADE_SUCCESS certifications.
        Returns (is_passed, list_of_unmet_requirements).
        """
        unmet = []
        
        # 1. Release Identity
        rel = self.data.get("release_identity", {})
        if not rel.get("version") or not rel.get("commit_sha") or not rel.get("build_id"):
            unmet.append("Incomplete release_identity in manifest")
        revision = rel.get("commit_sha")

        # Every recorded gate must be attributable to the exact target
        # revision.  A green boolean without provenance is not release proof.
        matrix_sections = _MATRIX_SECTIONS
        for section in _REQUIRED_EVIDENCE_SECTIONS:
            payload = self.data.get(section, {})
            if payload.get("status") in ("SKIPPED", "UNAVAILABLE"):
                unmet.append("{} evidence is {}".format(section, payload.get("status")))
            if payload and payload.get("revision") != revision:
                unmet.append("{} evidence revision is missing or mismatched".format(section))
        for section in matrix_sections:
            for name, payload in (self.data.get(section, {}) or {}).items():
                if name == "_record_meta":
                    continue
                if not isinstance(payload, dict):
                    continue
                if payload.get("revision") != revision:
                    unmet.append("{}:{} evidence revision is missing or mismatched".format(section, name))

        # Provenance is a release gate in its own right.  A record may point to
        # no file (for example a live endpoint check), but it must still carry
        # the command/result context; a missing or unknown exit code is never a
        # successful execution claim.
        provenance_sections = _REQUIRED_EVIDENCE_SECTIONS
        for section in provenance_sections:
            if section == "traffic_open" and not require_traffic_open:
                continue
            payload = self.data.get(section) or {}
            if not isinstance(payload, dict):
                unmet.append("{} evidence is not a record object".format(section))
                continue
            for field in ("name", "started_at", "finished_at", "host", "environment", "command"):
                if not payload.get(field):
                    unmet.append("{} evidence is missing provenance field {}".format(section, field))
            if payload.get("exit_code") != 0:
                unmet.append("{} evidence exit_code is not 0".format(section))
        for section in matrix_sections:
            for name, payload in (self.data.get(section, {}) or {}).items():
                if name == "_record_meta" or not isinstance(payload, dict):
                    continue
                for field in ("name", "started_at", "finished_at", "host", "environment", "command"):
                    if not payload.get(field):
                        unmet.append("{}:{} evidence is missing provenance field {}".format(section, name, field))
                if payload.get("exit_code") != 0:
                    unmet.append("{}:{} evidence exit_code is not 0".format(section, name))
            
        # 2. Targeted Tests
        tt = self.data.get("targeted_tests", {})
        test_records = [v for name, v in tt.items() if name != "_record_meta"] if isinstance(tt, dict) else []
        if not test_records or any(not isinstance(v, dict) or v.get("status") != "PASSED" for v in test_records):
            unmet.append("Targeted unit test matrix is incomplete or contains failures")
            
        # 3. Browser Smoke Suite
        bss = self.data.get("browser_smoke_suite", {})
        if bss.get("status") != "PASSED" or bss.get("evidence_class") != "real_browser":
            unmet.append("Browser smoke suite did not pass")
            
        # 4. Data Hash Verification
        dhv = self.data.get("data_hash_verification", {})
        if not dhv.get("verified") or dhv.get("evidence_class") != "production_database":
            unmet.append("Data hash verification gate is not verified")
            
        # 5. Schema Preflight
        sm = self.data.get("schema_migration", {})
        if not sm.get("preflight_safe"):
            unmet.append("Schema migration preflight is not safe")
            
        # 6. Sidecar Audit
        sa = self.data.get("sidecar_audit", {})
        if not sa.get("is_safe"):
            unmet.append(f"Sidecar audit found safety violations: corrupted={sa.get('corrupted_registries')}, orphan={sa.get('orphaned_cache_count')}")

        # 6b. Path mapping must have real, explainable input and no violations.
        pma = self.data.get("path_mapping_audit", {})
        if pma.get("status") != "PASSED" or not pma.get("is_valid"):
            unmet.append("Path mapping audit is missing, unavailable, or invalid")
        if pma.get("input_kind") != "repository_lcov":
            unmet.append("Path mapping audit is not backed by repository + LCOV inputs")

        lifecycle_sections = ("traffic_freeze", "job_drain", "api_stop", "api_start",
                              "release_endpoint", "file_cutover")
        for section in lifecycle_sections:
            if (self.data.get(section) or {}).get("status") != "PASSED":
                unmet.append("{} lifecycle evidence is not PASSED".format(section))
        if require_traffic_open and (self.data.get("traffic_open") or {}).get("status") != "PASSED":
            unmet.append("traffic_open lifecycle evidence is not PASSED")
        rollback = self.data.get("rollback_evidence") or {}
        if rollback.get("status") != "PASSED" or not rollback.get("rehearsal_verified"):
            unmet.append("Forced rollback rehearsal evidence is missing")
            
        # 7. Security Audit
        sec = self.data.get("security_audit", {})
        if (not sec.get("is_safe") or sec.get("critical_count", 0) > 0
                or sec.get("high_count", 0) > 0 or sec.get("auth_mode") == "disabled"):
            unmet.append(f"Security audit has unresolved findings: Critical={sec.get('critical_count')}, High={sec.get('high_count')}")
            
        # 8. Performance Benchmark
        pb = self.data.get("performance_benchmark", {})
        if (pb.get("evidence_class") != "performance_ab" or not pb.get("workload_id")
                or not isinstance(pb.get("baseline_ms"), (int, float))
                or not isinstance(pb.get("candidate_ms"), (int, float))):
            unmet.append("Performance evidence is not a baseline/candidate A/B run")
        required_tiers = ["Tier_A_1k", "Tier_B_10k", "Tier_C_50k", "Tier_D_100k"]
        for tier in required_tiers:
            if tier not in pb or not isinstance(pb[tier], dict) or pb[tier].get("status") != "PASSED":
                unmet.append(f"Performance benchmark tier missing or failed: {tier}")
                
        # 9. Backup Evidence
        bk = self.data.get("backup_evidence", {})
        if (not bk.get("full_sql_gz_sha256") or bk.get("status") != "BACKUP_VERIFIED"
                or bk.get("evidence_class") not in ("production_backup", "staging_backup")):
            unmet.append("Backup evidence missing or unverified")
        if not bk.get("artifact_path") or not bk.get("artifact_sha256"):
            unmet.append("Backup evidence is missing a hashed dump artifact")
        verification = bk.get("verification") or {}
        if not verification.get("table_inventory"):
            unmet.append("Backup evidence has no verified schema/table inventory")
        if verification.get("restore_smoke") != "PASSED":
            unmet.append("Backup restore smoke was not verified")
            
        # A checked-in/old ledger must never certify the current release.
        if rel.get("commit_sha") != self._current_commit_sha():
            unmet.append("Evidence revision does not match current commit")
        passed = (len(unmet) == 0)
        self.data["status"] = _SUCCESS_STATUS if passed else "UNMET_GATES"
        self.save()
        return passed, unmet

    def _current_commit_sha(self):
        import subprocess
        try:
            return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo_root,
                                           stderr=subprocess.DEVNULL).decode().strip()
        except Exception:
            return ""
