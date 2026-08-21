"""Durable Scan Import coordinator and recovery handler registry."""

from __future__ import absolute_import

import hashlib
import json
import os
import uuid

from app.db.repositories import JobRepository, ProjectRepository, ProjectStateRepository
from app.db.repositories.base import adapt_sql, execute, fetchone
from app.db.transaction import transaction
from app.scan_import.artifacts import ImmutableArtifactStager
from app.scan_import.checkpoints import ImportCheckpointRepository, PHASES
from app.scan_import.locks import RepositoryResourceLockService
from app.scan_import.publication import ScanPublicationService
from app.time_utils import utc_sql


class ScanImportCoordinator(object):
    HANDLER_VERSION = "SCAN_IMPORT_HANDLER_V1"

    def __init__(self, project_repository=None, state_repository=None,
                 job_repository=None, lock_service=None, checkpoint_repository=None,
                 publication_service=None, stager=None):
        self.projects = project_repository or ProjectRepository()
        self.states = state_repository or ProjectStateRepository()
        self.jobs = job_repository or JobRepository()
        self.locks = lock_service or RepositoryResourceLockService()
        self.checkpoints = checkpoint_repository or ImportCheckpointRepository()
        self.publication = publication_service or ScanPublicationService(self.states)
        self.stager = stager

    def _stager(self, root):
        if self.stager is None:
            self.stager = ImmutableArtifactStager(root)
        return self.stager

    def create(self, connection, project_name, info_path, info_sha256="",
               repository_resource_ids=None, repositories=None, review_scope="full",
               report=None, requested_by="", staging_root=None, algorithm_version="v1"):
        """Stage and register a Candidate without changing CURRENT.

        The physical locks are acquired before the business Scan row is
        created, which gives the busy path its zero-residue guarantee.
        """
        job_id = uuid.uuid4().hex
        owner_token = uuid.uuid4().hex
        if not staging_root:
            raise ValueError("staging_root is required for durable import")
        resource_ids = sorted(set(int(item) for item in (repository_resource_ids or [])))
        artifact = None
        with transaction(connection) as conn:
            acquired = self.locks.acquire(conn, resource_ids, job_id, owner_token)
            try:
                artifact = self._stager(staging_root).stage(conn, job_id, info_path)
                requested_sha = str(info_sha256 or "").strip().lower()
                if requested_sha and requested_sha != str(artifact["sha256"]).lower():
                    raise ValueError("INFO_SHA256_MISMATCH")
                project = self.projects.ensure_project(conn, project_name)
                state = self.states.ensure(conn, project["id"])
                predecessor = state.get("current_scan_id")
                repositories = list(repositories or [])
                scan_key = self._scan_key(
                    project_name, info_sha256 or artifact["sha256"], repositories,
                    review_scope, (report or {}).get("source_signature", "")
                )
                scan = self.projects.create_scan(
                    conn, project["id"], scan_key, "import", review_scope,
                    info_file_name=os.path.basename(info_path),
                    info_sha256=info_sha256 or artifact["sha256"],
                    status="IMPORTING", predecessor_scan_id=predecessor,
                    algorithm_version=algorithm_version,
                )
                job_payload = {
                    "project_id": project["id"], "project_name": project_name,
                    "scan_id": scan["id"], "predecessor_scan_id": predecessor,
                    "artifact_id": artifact["artifact_id"],
                    "artifact_sha256": artifact["sha256"],
                    "info_path": os.path.realpath(str(info_path)),
                    "repository_resource_ids": resource_ids,
                    "repositories": repositories, "review_scope": review_scope,
                    "report": report or {}, "requested_by": requested_by,
                    "algorithm_version": algorithm_version,
                    "handler_version": self.HANDLER_VERSION,
                }
                job = self.jobs.upsert(conn, {
                    "job_id": job_id, "project_id": project["id"],
                    "scan_id": scan["id"], "kind": "scan_import", "state": "queued",
                    "progress": 0, "input_payload": json.dumps(
                        job_payload, ensure_ascii=False, sort_keys=True
                    ), "data_version": state.get("data_version") or 0,
                    "lease_owner": owner_token,
                })
                cursor = execute(conn, """
                    UPDATE coverage_background_jobs SET handler_version=?, updated_at=?
                    WHERE job_id=?
                """, (self.HANDLER_VERSION, utc_sql(), job_id))
                cursor.close()
                token = acquired[0]["fencing_token"] if acquired else 0
                checkpoint = self.checkpoints.create(
                    conn, job_id, scan["id"], expected_current_scan_id=predecessor,
                    input_sha256=artifact["sha256"], fencing_token=token,
                    payload=job_payload,
                )
                return {"job": job, "scan": scan, "artifact": artifact,
                        "checkpoint": checkpoint, "owner_token": owner_token,
                        "locks": acquired}
            except Exception:
                self.locks.release(conn, job_id, owner_token)
                if artifact and artifact.get("staged_path"):
                    try:
                        os.remove(artifact["staged_path"])
                    except OSError:
                        pass
                raise

    def advance(self, connection, job_id, expected_seq, expected_fencing_token,
                phase, payload=None):
        with transaction(connection) as conn:
            return self.checkpoints.advance(
                conn, job_id, expected_seq, expected_fencing_token, phase,
                payload=payload,
            )

    def seal(self, connection, job_id, owner_token, fencing_token):
        with transaction(connection) as conn:
            checkpoint = self.checkpoints.get(conn, job_id)
            if not checkpoint:
                raise KeyError("import checkpoint not found")
            self._assert_all_fences(conn, job_id, owner_token, fencing_token,
                                    checkpoint=checkpoint)
            scan_id = checkpoint.get("scan_id")
            cursor = execute(conn, """
                UPDATE coverage_scans SET status='SEALED'
                WHERE id=? AND status IN ('IMPORTING', 'VALIDATING', 'building', 'importing')
            """, (int(scan_id),))
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                cursor.close()
                raise ValueError("SCAN_NOT_IMPORTING")
            cursor.close()
            return self.checkpoints.advance(
                conn, job_id, checkpoint["checkpoint_seq"], fencing_token,
                "SEALED", payload={"scan_id": scan_id},
            )

    def publish(self, connection, job_id, expected_current_scan_id=None,
                read_set=None):
        with transaction(connection) as conn:
            job = self.jobs.get(conn, job_id)
            if not job:
                raise KeyError("import job not found")
            payload = json.loads(job.get("input_payload") or "{}")
            project_id = int(payload["project_id"])
            scan_id = int(payload["scan_id"])
            checkpoint = self.checkpoints.get(conn, job_id)
            if not checkpoint:
                raise KeyError("import checkpoint not found")
            if checkpoint.get("phase") in ("PUBLISHED", "DONE"):
                return self.states.get(conn, project_id)
            if checkpoint.get("phase") != "SEALED":
                raise ValueError("IMPORT_NOT_SEALED")
            locks = self._locks_for_job(conn, job_id)

            def assert_fences():
                for lock in locks:
                    self.locks.assert_fence(
                        conn, lock["physical_resource_id"], job_id,
                        lock["owner_token"], lock["fencing_token"],
                    )

            result = self.publication.publish_in_transaction(
                conn, project_id, scan_id,
                expected_current_scan_id=(payload.get("predecessor_scan_id")
                                           if expected_current_scan_id is None
                                           else expected_current_scan_id),
                read_set=read_set, fence=assert_fences,
            )
            self.checkpoints.advance(
                conn, job_id, checkpoint["checkpoint_seq"],
                checkpoint.get("fencing_token") or 0, "PUBLISHED",
                payload={"scan_id": scan_id},
            )
            cursor = execute(conn, """
                UPDATE coverage_background_jobs
                SET state='completed', progress=1, finished_at=?, heartbeat_at=?,
                    updated_at=?
                WHERE job_id=? AND state IN ('queued', 'running', 'completed')
            """, (utc_sql(), utc_sql(), utc_sql(), str(job_id)))
            cursor.close()
            for lock in locks:
                self.locks.release(conn, job_id, lock["owner_token"],
                                   lock["fencing_token"])
            return result

    @staticmethod
    def _locks_for_job(connection, job_id):
        cursor = connection.cursor()
        cursor.execute(adapt_sql(connection, """
            SELECT * FROM coverage_repository_resource_locks
            WHERE job_id=? ORDER BY physical_resource_id
        """), (str(job_id),))
        try:
            columns = [item[0] for item in (cursor.description or [])]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def _assert_all_fences(self, connection, job_id, owner_token,
                           fencing_token, checkpoint=None):
        checkpoint = checkpoint or self.checkpoints.get(connection, job_id)
        expected = int((checkpoint or {}).get("fencing_token") or 0)
        if expected and int(fencing_token) != expected:
            raise ValueError("LOCK_FENCING_FAILED")
        for lock in self._locks_for_job(connection, job_id):
            self.locks.assert_fence(
                connection, lock["physical_resource_id"], job_id,
                owner_token, lock["fencing_token"],
            )

    @staticmethod
    def _scan_key(project_name, info_sha256, repositories, review_scope, report_signature):
        identity = []
        for item in repositories or []:
            item = item or {}
            identity.append({
                "repository_name": str(item.get("repository_name") or ""),
                "branch_name": str(item.get("branch_name") or ""),
                "commit_sha": str(item.get("commit_sha") or ""),
            })
        payload = {
            "project_name": project_name, "info_sha256": info_sha256 or "",
            "review_scope": review_scope or "full",
            "repositories": sorted(identity, key=lambda value: (
                value["repository_name"], value["branch_name"], value["commit_sha"]
            )), "report_source_signature": report_signature or "",
            "identity_contract_version": 2,
        }
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
