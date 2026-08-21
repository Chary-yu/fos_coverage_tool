"""Dedicated recovery ownership for durable Scan Import jobs.

Generic job reaping may mark ordinary work interrupted, but it must not
silently complete or fail an import.  This service validates the persisted
artifact/checkpoint identity, reclaims the physical locks with a new fencing
token, and records technical failures in the import failure ledger.
"""

from __future__ import absolute_import

import hashlib
import json
import uuid

from app.db.repositories import JobRepository, ProjectRepository
from app.db.identity_keys import stable_identity_hash
from app.db.repositories.base import adapt_sql, execute, fetchall, fetchone, is_sqlite
from app.db.transaction import transaction
from app.scan_import.artifacts import ImmutableArtifactStager
from app.scan_import.checkpoints import ImportCheckpointRepository
from app.scan_import.coordinator import ScanImportCoordinator
from app.scan_import.locks import RepositoryResourceLockService
from app.time_utils import utc_sql


class ScanImportRecoveryService(object):
    HANDLER_VERSION = ScanImportCoordinator.HANDLER_VERSION

    def __init__(self, coordinator=None, jobs=None, projects=None,
                 checkpoints=None, locks=None, stager=None):
        self.coordinator = coordinator or ScanImportCoordinator()
        self.jobs = jobs or JobRepository()
        self.projects = projects or ProjectRepository()
        self.checkpoints = checkpoints or ImportCheckpointRepository()
        self.locks = locks or RepositoryResourceLockService()
        self.stager = stager

    def _artifact_stager(self, path):
        if self.stager is None:
            self.stager = ImmutableArtifactStager(path)
        return self.stager

    def list_recoverable(self, connection):
        return fetchall(connection, """
            SELECT * FROM coverage_background_jobs
            WHERE kind='scan_import' AND state IN ('queued', 'running', 'interrupted')
            ORDER BY created_at, job_id
        """)

    def validate(self, connection, job_id):
        job = self.jobs.get(connection, job_id)
        if not job or str(job.get("kind") or "") != "scan_import":
            raise KeyError("scan_import job not found")
        if str(job.get("handler_version") or "") != self.HANDLER_VERSION:
            raise ValueError("UNSUPPORTED_SCAN_IMPORT_HANDLER")
        payload = json.loads(job.get("input_payload") or "{}")
        checkpoint = self.checkpoints.get(connection, job_id)
        if not checkpoint:
            raise ValueError("IMPORT_CHECKPOINT_MISSING")
        if int(payload.get("scan_id") or 0) != int(checkpoint.get("scan_id") or 0):
            raise ValueError("IMPORT_SCAN_ID_MISMATCH")
        scan = self.projects.get_scan(connection, int(payload["scan_id"]))
        if not scan or int(scan.get("project_id") or 0) != int(payload.get("project_id") or 0):
            raise ValueError("IMPORT_PROJECT_ID_MISMATCH")
        artifact = self._artifact_stager(
            payload.get("staging_root") or "."
        ).verify_staged(
            connection, payload.get("artifact_id"),
            expected_sha256=payload.get("artifact_sha256"),
        )
        if str(checkpoint.get("input_sha256") or "") != str(artifact.get("sha256") or ""):
            raise ValueError("IMPORT_CHECKPOINT_SHA_MISMATCH")
        return {
            "job": job, "payload": payload, "checkpoint": checkpoint,
            "scan": scan, "artifact": artifact,
        }

    def reclaim(self, connection, job_id, staging_root, resource_ids=None,
                owner_token=None, lease_seconds=300):
        owner_token = owner_token or uuid.uuid4().hex
        with transaction(connection) as conn:
            state = self.validate(connection, job_id)
            payload = state["payload"]
            resources = resource_ids
            if resources is None:
                resources = payload.get("repository_resource_ids") or []
            locks = self.locks.acquire(
                conn, resources, job_id, owner_token,
                lease_seconds=lease_seconds,
            )
            token = locks[0]["fencing_token"] if locks else 0
            checkpoint = self.checkpoints.claim_fencing(
                conn, job_id, token
            ) if locks else state["checkpoint"]
            cursor = execute(conn, """
                UPDATE coverage_background_jobs
                SET state='running', lease_owner=?, heartbeat_at=?, updated_at=?
                WHERE job_id=? AND state IN ('queued', 'running', 'interrupted')
            """, (owner_token, utc_sql(), utc_sql(), str(job_id)))
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                cursor.close()
                raise ValueError("INVALID_JOB_TRANSITION")
            cursor.close()
            return {
                "job": self.jobs.get(conn, job_id),
                "checkpoint": checkpoint,
                "locks": locks,
                "owner_token": owner_token,
            }

    def record_failure(self, connection, job_id, phase, error_class,
                       message="", fencing_token=None, scan_status="FAILED"):
        scan_status = str(scan_status or "FAILED").upper()
        if scan_status not in ("FAILED", "ABORTED"):
            raise ValueError("invalid scan failure status")
        fingerprint = hashlib.sha256(
            "{}\n{}\n{}".format(phase, error_class, message).encode("utf-8")
        ).hexdigest()
        failure_key_hash = stable_identity_hash(
            str(job_id), str(phase), fingerprint
        )
        redacted = str(message or "")[:512]
        with transaction(connection) as conn:
            job = self.jobs.get(conn, job_id)
            if not job:
                raise KeyError("scan_import job not found")
            scan_id = job.get("scan_id")
            insert_prefix = "INSERT OR IGNORE" if is_sqlite(conn) else "INSERT IGNORE"
            insert_sql = """
                    {prefix} INTO coverage_import_failures(
                        job_id, scan_id, phase, error_class, error_fingerprint,
                        failure_key_hash, message_redacted, fencing_token,
                        occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """.format(prefix=insert_prefix)
            cursor = conn.cursor()
            cursor.execute(adapt_sql(conn, insert_sql), (
                str(job_id), scan_id, str(phase), str(error_class),
                fingerprint, failure_key_hash, redacted, fencing_token,
                utc_sql(),
            ))
            cursor.close()
            cursor = execute(conn, """
                UPDATE coverage_background_jobs
                SET state='failed', error_message=?, finished_at=?, updated_at=?
                WHERE job_id=? AND state IN ('queued', 'running', 'interrupted')
            """, (redacted, utc_sql(), utc_sql(), str(job_id)))
            cursor.close()
            if scan_id:
                cursor = execute(conn, """
                    UPDATE coverage_scans SET status=?
                    WHERE id=? AND UPPER(status) IN
                        ('IMPORTING', 'VALIDATING', 'BUILDING', 'CONSTRUCTING',
                         'SEALED', 'READY')
                      AND NOT EXISTS (
                          SELECT 1 FROM coverage_project_state ps
                          WHERE ps.project_id=coverage_scans.project_id
                            AND ps.current_scan_id=coverage_scans.id
                      )
                """, (scan_status, int(scan_id)))
                cursor.close()
            for lock in self.coordinator._locks_for_job(conn, job_id):
                self.locks.release(
                    conn, job_id, lock["owner_token"], lock["fencing_token"]
                )
            return self.jobs.get(conn, job_id)
