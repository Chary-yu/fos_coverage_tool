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

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.time_utils import utc_iso


def get_utc_iso():
    return utc_iso()

MANIFEST_FILENAME = "production_evidence_manifest.json"
_SUCCESS_STATUS = "UPGRADE_" + "SUCCESS"

_MATRIX_SECTIONS = ("targeted_tests", "performance_benchmark")
_REQUIRED_EVIDENCE_SECTIONS = (
    "schema_migration", "backup_evidence", "data_hash_verification",
    "browser_fixture_regression", "candidate_browser_evidence",
    "path_mapping_audit", "sidecar_audit",
    "security_audit", "traffic_freeze", "job_drain", "api_stop",
    "api_start", "release_endpoint", "file_cutover",
    "validation_session_manifest", "validation_teardown", "traffic_open",
    "post_open_serving", "rollback_evidence",
)

EVIDENCE_MANIFEST_V2_FILENAME = "evidence-manifest-v2.json"


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


class EvidenceManifestV2:
    """Canonical machine-readable evidence ledger for Gate A--F.

    ``ProductionEvidenceManifest`` remains the release-upgrade compatibility
    ledger.  This smaller record-oriented manifest is the contract used by
    each Gate bundle, so a static or synthetic result cannot accidentally be
    mistaken for release evidence merely because a file exists.
    """

    REQUIRED_RECORD_FIELDS = (
        "gate", "evidence_id", "evidence_class", "candidate_revision",
        "release_identity",
        "host_identity", "command_or_action", "started_at", "finished_at",
        "exit_code", "artifact_path", "artifact_sha256", "source_inputs_sha256",
        "status", "synthetic",
    )

    def __init__(self, repo_root: str, gate: str, candidate_revision: str = "",
                 release_identity=None, database_runtime_identity=None,
                 manifest_path: str = ""):
        self.repo_root = os.path.abspath(repo_root)
        self.gate = str(gate or "").strip()
        if not self.gate:
            raise ValueError("gate is required")
        self.manifest_path = os.path.abspath(
            manifest_path or os.path.join(
                self.repo_root, ".artifacts", self.gate,
                EVIDENCE_MANIFEST_V2_FILENAME,
            )
        )
        self.data = self._load_or_init(
            candidate_revision, release_identity, database_runtime_identity
        )

    def _load_or_init(self, candidate_revision, release_identity,
                      database_runtime_identity):
        if os.path.isfile(self.manifest_path):
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as stream:
                    data = json.load(stream)
                if data.get("evidence_schema_version") == 2 and \
                        data.get("gate") == self.gate:
                    return data
            except (OSError, ValueError, TypeError):
                pass
        now = get_utc_iso()
        return {
            "evidence_schema_version": 2,
            "gate": self.gate,
            "candidate_revision": str(candidate_revision or ""),
            "release_identity": dict(release_identity or {}),
            "database_runtime_identity": dict(database_runtime_identity or {}),
            "created_at": now,
            "updated_at": now,
            "evidence": [],
            "manifest_sha256": "",
        }

    def record(self, evidence_id: str, evidence_class: str, status: str,
               command_or_action, exit_code, artifact_path="",
               source_inputs_sha256=None, candidate_revision="",
               host_identity=None, database_runtime_identity=None,
               release_identity=None, started_at="", finished_at="",
               synthetic=False, **extra):
        candidate = str(candidate_revision or self.data.get("candidate_revision") or "")
        if not candidate:
            raise ValueError("candidate_revision is required")
        now = get_utc_iso()
        artifact_path = os.path.abspath(str(artifact_path)) if artifact_path else ""
        artifact_sha256 = ""
        if artifact_path:
            if not os.path.isfile(artifact_path):
                raise FileNotFoundError(artifact_path)
            artifact_sha256 = _sha256_file(artifact_path)
        record = {
            "gate": self.gate,
            "evidence_id": str(evidence_id),
            "evidence_class": str(evidence_class),
            "candidate_revision": candidate,
            "release_identity": dict(release_identity or
                                      self.data.get("release_identity") or {}),
            "host_identity": host_identity or {
                "hostname": socket.gethostname(),
                "runtime": platform.platform(),
            },
            "database_runtime_identity": dict(
                database_runtime_identity or
                self.data.get("database_runtime_identity") or {}
            ),
            "command_or_action": command_or_action,
            "started_at": started_at or now,
            "finished_at": finished_at or now,
            "exit_code": exit_code,
            "artifact_path": artifact_path,
            "artifact_sha256": artifact_sha256,
            "source_inputs_sha256": sorted(set(str(item) for item in
                                                (source_inputs_sha256 or []))),
            "status": str(status),
            "synthetic": bool(synthetic),
        }
        record.update(extra)
        records = [item for item in self.data.setdefault("evidence", [])
                   if item.get("evidence_id") != str(evidence_id)]
        records.append(record)
        self.data["evidence"] = records
        self.data["candidate_revision"] = candidate
        if release_identity:
            self.data["release_identity"] = dict(release_identity)
        if database_runtime_identity:
            self.data["database_runtime_identity"] = dict(database_runtime_identity)
        self.data["updated_at"] = get_utc_iso()
        self.save()
        return record

    def validate(self, require_current_revision=False):
        errors = []
        if self.data.get("evidence_schema_version") != 2:
            errors.append("evidence_schema_version must be 2")
        if self.data.get("gate") != self.gate:
            errors.append("manifest gate does not match the requested gate")
        unsigned = dict(self.data)
        observed_manifest_sha = str(unsigned.pop("manifest_sha256", "") or "")
        expected_manifest_sha = hashlib.sha256(json.dumps(
            dict(unsigned, manifest_sha256=""), sort_keys=True, indent=2,
            ensure_ascii=False
        ).encode("utf-8")).hexdigest()
        if not observed_manifest_sha or observed_manifest_sha != expected_manifest_sha:
            errors.append("manifest_sha256 is missing or does not match manifest content")
        candidate = str(self.data.get("candidate_revision") or "")
        if not candidate:
            errors.append("candidate_revision is missing")
        release_identity = self.data.get("release_identity") or {}
        if not isinstance(release_identity, dict):
            errors.append("release_identity must be an object")
        elif not release_identity.get("commit_sha"):
            errors.append("manifest release identity commit_sha is missing")
        elif release_identity.get("commit_sha") != candidate:
            errors.append("manifest release identity revision mismatch")
        records = self.data.get("evidence")
        if not isinstance(records, list) or not records:
            errors.append("evidence records are missing")
            records = []
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                errors.append("evidence[{}] must be an object".format(index))
                continue
            for field in self.REQUIRED_RECORD_FIELDS:
                if field not in record:
                    errors.append("evidence[{}] missing {}".format(index, field))
            if record.get("gate") != self.gate:
                errors.append("evidence[{}] gate mismatch".format(index))
            if not record.get("evidence_id"):
                errors.append("evidence[{}] evidence_id is missing".format(index))
            if not record.get("evidence_class"):
                errors.append("evidence[{}] evidence_class is missing".format(index))
            if record.get("candidate_revision") != candidate:
                errors.append("evidence[{}] candidate revision mismatch".format(index))
            if not isinstance(record.get("host_identity"), dict) or not record.get("host_identity"):
                errors.append("evidence[{}] host identity is missing".format(index))
            if not record.get("command_or_action"):
                errors.append("evidence[{}] command/action is missing".format(index))
            if not record.get("started_at") or not record.get("finished_at"):
                errors.append("evidence[{}] timestamps are missing".format(index))
            if type(record.get("exit_code")) not in (int, type(None)):
                errors.append("evidence[{}] exit_code must be an integer or null".format(index))
            if record.get("status") not in (
                    "PASSED", "INCOMPLETE", "BLOCKED", "FAILED", "PARTIAL",
                    "UNAVAILABLE", "SKIPPED"):
                errors.append("evidence[{}] status is not a known gate status".format(index))
            artifact_path = record.get("artifact_path") or ""
            if artifact_path and not record.get("artifact_sha256"):
                errors.append("evidence[{}] artifact SHA256 missing".format(index))
            if artifact_path:
                if not os.path.isabs(artifact_path) or not os.path.isfile(artifact_path):
                    errors.append("evidence[{}] artifact path is missing".format(index))
                else:
                    try:
                        if _sha256_file(artifact_path) != str(record.get("artifact_sha256")):
                            errors.append("evidence[{}] artifact SHA256 does not match".format(index))
                    except OSError:
                        errors.append("evidence[{}] artifact cannot be hashed".format(index))
            if record.get("status") == "PASSED" and record.get("exit_code") != 0:
                errors.append("evidence[{}] PASSED without exit_code 0".format(index))
            if record.get("status") == "PASSED" and record.get("synthetic"):
                errors.append("evidence[{}] synthetic evidence cannot be PASSED".format(index))
            if record.get("status") == "PASSED":
                if not record.get("artifact_path") or not record.get("artifact_sha256"):
                    errors.append("evidence[{}] PASSED evidence artifact path/SHA256 is missing".format(index))
            release_identity = record.get("release_identity") or {}
            if not isinstance(release_identity, dict) or not release_identity.get("commit_sha"):
                errors.append("evidence[{}] release identity commit_sha is missing".format(index))
            elif release_identity.get("commit_sha") != candidate:
                errors.append("evidence[{}] release identity revision mismatch".format(index))
            evidence_class = str(record.get("evidence_class") or "").lower()
            if ("database" in evidence_class or "mariadb" in evidence_class or
                    "mysql" in evidence_class) and not record.get("database_runtime_identity"):
                errors.append("evidence[{}] database runtime identity is missing".format(index))
        if require_current_revision:
            current = ProductionEvidenceManifest(self.repo_root)._current_commit_sha()
            if current and current != candidate:
                errors.append("candidate revision does not match current commit")
        return not errors, errors

    def save(self):
        directory = os.path.dirname(self.manifest_path)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        unsigned = dict(self.data)
        unsigned["manifest_sha256"] = ""
        encoded = json.dumps(unsigned, sort_keys=True, indent=2,
                             ensure_ascii=False).encode("utf-8")
        self.data["manifest_sha256"] = hashlib.sha256(encoded).hexdigest()
        temporary = self.manifest_path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(self.data, stream, sort_keys=True, indent=2,
                      ensure_ascii=False)
        os.replace(temporary, self.manifest_path)

class ProductionEvidenceManifest:
    def __init__(self, repo_root: str):
        self.repo_root = repo_root
        evidence_root = os.environ.get("COVERAGE_EVIDENCE_DIR") or os.path.join(repo_root, ".artifacts", "evidence")
        os.makedirs(evidence_root, exist_ok=True)
        self.manifest_path = os.path.join(evidence_root, MANIFEST_FILENAME)
        self.data: Dict[str, Any] = self._load_or_init()
        # A newly created or legacy manifest is never implicitly release-ready.
        self.data.setdefault("release_decision", "NOT_READY")

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
            "release_decision": "NOT_READY",
            "release_identity": {},
            "schema_migration": {},
            "backup_evidence": {},
            "data_hash_verification": {},
            "targeted_tests": {},
            "browser_fixture_regression": {},
            "candidate_browser_evidence": {},
            "path_mapping_audit": {},
            "sidecar_audit": {},
            "security_audit": {},
            "performance_benchmark": {},
            "validation_session_manifest": {},
            "validation_teardown": {},
            "post_open_serving": {},
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

    def validate_final_gate(self, require_traffic_open: bool = True,
                            require_post_open_serving: Optional[bool] = None) -> Tuple[bool, List[str]]:
        """
        Validate all strict production gates to prevent false UPGRADE_SUCCESS certifications.
        Returns (is_passed, list_of_unmet_requirements).
        """
        unmet = []
        if require_post_open_serving is None:
            require_post_open_serving = require_traffic_open
        
        # 1. Release Identity
        rel = self.data.get("release_identity", {})
        if not rel.get("version") or not rel.get("commit_sha") or not rel.get("build_id"):
            unmet.append("Incomplete release_identity in manifest")
        revision = rel.get("commit_sha")

        # Every recorded gate must be attributable to the exact target
        # revision.  A green boolean without provenance is not release proof.
        matrix_sections = _MATRIX_SECTIONS
        for section in _REQUIRED_EVIDENCE_SECTIONS:
            if section == "traffic_open" and not require_traffic_open:
                continue
            if section == "post_open_serving" and not require_post_open_serving:
                continue
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
            if section == "post_open_serving" and not require_post_open_serving:
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
            
        # 3. Browser evidence has two deliberately separate authorities.
        # The fixture regression protects test coverage; only the externally
        # collected exact-Candidate artifact can certify production traffic.
        fixture = self.data.get("browser_fixture_regression", {})
        if fixture.get("status") != "PASSED" or \
                fixture.get("evidence_class") != "browser_fixture_regression":
            unmet.append("Browser fixture regression did not pass")
        candidate_browser = self.data.get("candidate_browser_evidence", {})
        if candidate_browser.get("status") != "PASSED" or \
                candidate_browser.get("evidence_class") != "real_candidate_browser":
            unmet.append("Real Candidate browser evidence did not pass")
        if candidate_browser.get("release_eligible") is not True:
            unmet.append("Candidate browser evidence is not release eligible")
        if candidate_browser.get("synthetic") is not False:
            unmet.append("Synthetic browser evidence cannot pass the production gate")
        if not candidate_browser.get("candidate_url"):
            unmet.append("Candidate browser URL is missing")
        if candidate_browser.get("expected_commit_sha") != revision:
            unmet.append("Candidate browser expected commit does not match release identity")
        validation_session = self.data.get("validation_session_manifest") or {}
        attempt_id = validation_session.get("session_id")
        if candidate_browser.get("release_validation_session_id") != attempt_id:
            unmet.append("Candidate browser evidence is not bound to this validation attempt")
        file_cutover = self.data.get("file_cutover") or {}
        expected_candidate_artifact = file_cutover.get("candidate_artifact_sha256")
        expected_served_root = file_cutover.get("served_root_sha256")
        if not expected_candidate_artifact or candidate_browser.get(
                "candidate_artifact_sha256") != expected_candidate_artifact:
            unmet.append("Candidate browser evidence is not bound to the published Candidate artifact")
        if not expected_served_root or candidate_browser.get(
                "served_root_sha256") != expected_served_root:
            unmet.append("Candidate browser evidence is not bound to the immutable Served Root")
        served_identity = candidate_browser.get("served_release_identity") or {}
        if not isinstance(served_identity, dict) or \
                served_identity.get("commit_sha") != revision:
            unmet.append("Candidate browser served release identity does not match release identity")
        if candidate_browser.get("real_http") is not True:
            unmet.append("Candidate browser evidence is not real HTTP")
        if candidate_browser.get("chromium") is not True:
            unmet.append("Candidate browser evidence is not Chromium")
        browser_artifact = candidate_browser.get("browser_artifact_path")
        browser_artifact_sha = candidate_browser.get("browser_artifact_sha256")
        if not browser_artifact or not os.path.isabs(str(browser_artifact)) or \
                not os.path.isfile(str(browser_artifact)):
            unmet.append("Candidate browser artifact is missing")
        elif not browser_artifact_sha or _sha256_file(str(browser_artifact)) != str(browser_artifact_sha):
            unmet.append("Candidate browser artifact SHA256 mismatch")
            
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

        # The process that was validated before teardown is not sufficient
        # proof of the process serving traffic after the switch.  Require a
        # fresh release/health probe and an exact immutable CURRENT identity.
        if require_post_open_serving:
            post_open = self.data.get("post_open_serving") or {}
            if post_open.get("status") != "PASSED":
                unmet.append("post_open_serving lifecycle evidence is not PASSED")
            if post_open.get("process_role") != "production_serving":
                unmet.append("post_open_serving must describe the production serving process")
            release_check = post_open.get("release_endpoint") or {}
            release_identity = (
                release_check.get("release")
                if isinstance(release_check, dict) else {}
            )
            if not isinstance(release_identity, dict) or \
                    release_identity.get("commit_sha") != revision:
                unmet.append("post_open_serving release identity does not match the target")
            current_id = post_open.get("current_release_validation_session_id")
            expected_id = post_open.get("expected_release_validation_session_id")
            if not current_id or current_id != expected_id:
                unmet.append("post_open_serving CURRENT identity is not exact")
            if post_open.get("release_validation_session_id") != expected_id:
                unmet.append("post_open_serving release-validation attempt is not exact")
            if post_open.get("candidate_artifact_sha256") != expected_candidate_artifact:
                unmet.append("post_open_serving Candidate artifact hash is not exact")
            if post_open.get("served_root_sha256") != expected_served_root:
                unmet.append("post_open_serving Served Root hash is not exact")
            health_check = post_open.get("health_endpoint") or {}
            health_payload = (
                health_check.get("health")
                if isinstance(health_check, dict) else {}
            )
            if health_check.get("status") != "PASSED" or \
                    not isinstance(health_payload, dict) or \
                    health_payload.get("status") not in ("ok", "healthy", "PASSED"):
                unmet.append("post_open_serving health check is not healthy")
            served_root_http = post_open.get("served_root_http") or {}
            if served_root_http.get("status") != "PASSED":
                unmet.append("post_open_serving HTTP Served Root identity is not verified")
            if served_root_http.get("relative_path") in (None, ""):
                unmet.append("post_open_serving HTTP Served Root probe path is missing")

        # Validation processes are owned resources, not an operator reminder.
        # The manifest and its teardown record must describe the same exact
        # candidate session, and both process and port closure are mandatory
        # before a release can become READY.
        validation_session = self.data.get("validation_session_manifest") or {}
        if validation_session.get("status") != "PASSED":
            unmet.append("validation_session_manifest is not PASSED")
        if not validation_session.get("session_id"):
            unmet.append("validation_session_manifest session_id is missing")
        if validation_session.get("candidate_sha") != revision:
            unmet.append("validation_session_manifest candidate SHA mismatches release identity")
        if not validation_session.get("baseline_sha"):
            unmet.append("validation_session_manifest baseline SHA is missing")
        if not validation_session.get("artifact_path") or not validation_session.get("artifact_sha256"):
            unmet.append("validation_session_manifest artifact hash is missing")

        validation_teardown = self.data.get("validation_teardown") or {}
        if validation_teardown.get("status") != "PASSED":
            unmet.append("validation_teardown is not PASSED")
        if validation_teardown.get("session_id") != validation_session.get("session_id"):
            unmet.append("validation_teardown session_id does not match validation session")
        if validation_teardown.get("pids_closed") is not True:
            unmet.append("validation_teardown requires pids_closed=true")
        if validation_teardown.get("ports_closed") is not True:
            unmet.append("validation_teardown requires ports_closed=true")
        if validation_teardown.get("ports_probe_ok") is not True:
            unmet.append("validation_teardown requires a successful port probe")
        if not validation_teardown.get("artifact_path") or not validation_teardown.get("artifact_sha256"):
            unmet.append("validation_teardown artifact hash is missing")

        rollback = self.data.get("rollback_evidence") or {}
        if rollback.get("status") != "PASSED" or not rollback.get("rehearsal_verified"):
            unmet.append("Forced rollback rehearsal evidence is missing")
        before_id = rollback.get("before_release_id")
        target_id = rollback.get("target_release_id")
        rollback_id = rollback.get("rollback_release_id")
        if not before_id or not target_id or not rollback_id:
            unmet.append("Rollback evidence lacks before/target/rollback release identities")
        elif before_id == target_id or rollback_id != before_id:
            unmet.append("Rollback evidence does not restore the before release identity")
        if rollback.get("release_validation_session_id") != validation_session.get("session_id"):
            unmet.append("Rollback evidence is not bound to this validation attempt")
        if target_id and target_id != validation_session.get("session_id"):
            unmet.append("Rollback target is not the current validation attempt")
        if rollback.get("candidate_artifact_sha256") != expected_candidate_artifact:
            unmet.append("Rollback evidence Candidate artifact hash is not exact")
        if rollback.get("served_root_sha256") != expected_served_root:
            unmet.append("Rollback evidence Served Root hash is not exact")
            
        # 7. Security Audit
        sec = self.data.get("security_audit", {})
        if (not sec.get("is_safe") or sec.get("critical_count", 0) > 0
                or sec.get("high_count", 0) > 0 or sec.get("auth_mode") == "disabled"):
            unmet.append(f"Security audit has unresolved findings: Critical={sec.get('critical_count')}, High={sec.get('high_count')}")
            
        # 8. Performance Benchmark
        pb = self.data.get("performance_benchmark", {})
        if (pb.get("evidence_class") != "release_performance_ab" or not pb.get("workload_id")
                or not isinstance(pb.get("baseline_ms"), (int, float))
                or not isinstance(pb.get("candidate_ms"), (int, float))
                or not pb.get("baseline_commit") or not pb.get("candidate_commit")
                or not pb.get("workload_hash") or not pb.get("environment_identity")):
            unmet.append("Performance evidence is not an immutable release baseline/candidate A/B run")
        if pb.get("candidate_commit") != revision:
            unmet.append("Performance candidate commit does not match release identity")
        if pb.get("release_validation_session_id") != validation_session.get("session_id"):
            unmet.append("Performance evidence is not bound to this validation attempt")
        if pb.get("candidate_artifact_sha256") != expected_candidate_artifact:
            unmet.append("Performance evidence Candidate artifact hash is not exact")
        if pb.get("served_root_sha256") != expected_served_root:
            unmet.append("Performance evidence Served Root hash is not exact")
        if pb.get("baseline_commit") == pb.get("candidate_commit"):
            unmet.append("Performance baseline and candidate commits must differ")
        source_inputs = pb.get("source_inputs_sha256")
        if not isinstance(source_inputs, list) or len(source_inputs) < 2 or \
                any(not str(item or "") for item in source_inputs):
            unmet.append("Performance source input hashes are missing")
        source_artifacts = pb.get("source_artifacts") or {}
        for role in ("baseline", "candidate"):
            source = source_artifacts.get(role) if isinstance(source_artifacts, dict) else None
            source_path = source.get("path") if isinstance(source, dict) else ""
            source_sha = source.get("sha256") if isinstance(source, dict) else ""
            expected_source_revision = pb.get("baseline_commit") if role == "baseline" else pb.get("candidate_commit")
            if not source_path or not os.path.isabs(str(source_path)) or not os.path.isfile(str(source_path)):
                unmet.append("Performance {} source artifact is missing".format(role))
            elif not source_sha or _sha256_file(str(source_path)) != str(source_sha):
                unmet.append("Performance {} source artifact SHA256 mismatch".format(role))
            if not isinstance(source, dict) or source.get("revision") != expected_source_revision:
                unmet.append("Performance {} source artifact revision mismatch".format(role))
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
        if bk.get("backup_root_external") is not True:
            unmet.append("Backup root is not proven to be outside deployment roots")
        if verification.get("restore_target_empty_before_restore") is not True:
            unmet.append("Backup restore target was not proven empty before restore")
        if not verification.get("source_database_runtime_identity") or \
                not verification.get("restore_database_runtime_identity"):
            unmet.append("Backup restore database runtime identities are incomplete")
            
        # A checked-in/old ledger must never certify the current release.
        if rel.get("commit_sha") != self._current_commit_sha():
            unmet.append("Evidence revision does not match current commit")
        passed = (len(unmet) == 0)
        self.data["status"] = _SUCCESS_STATUS if passed else "UNMET_GATES"
        # Keep the release decision explicit for downstream tooling.  The
        # historical ``status`` values remain intact for compatibility.
        self.data["release_decision"] = "READY" if passed else "NOT_READY"
        self.save()
        return passed, unmet

    def _current_commit_sha(self):
        import subprocess
        try:
            return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo_root,
                                           stderr=subprocess.DEVNULL).decode().strip()
        except Exception:
            return ""
