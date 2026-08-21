"""Durable Scan Import coordinator and recovery handler registry."""

from __future__ import absolute_import

import hashlib
import json
import os
import uuid

from app.db.repositories import JobRepository, ProjectRepository, ProjectStateRepository
from app.db.repositories.base import adapt_sql, execute, fetchall, fetchone
from app.db.transaction import transaction
from app.db.repositories.repository_repository import RepositoryRepository
from app.db.repositories.file_state_repository import FileStateRepository
from app.scan_import.artifacts import ImmutableArtifactStager
from app.scan_import.checkpoints import ImportCheckpointRepository, PHASES
from app.scan_import.locks import RepositoryResourceLockService
from app.scan_import.publication import ScanPublicationService
from app.time_utils import utc_sql
from app.inheritance.git_snapshot import GitSnapshotProvider, GitTechnicalFailure


class ScanImportCoordinator(object):
    HANDLER_VERSION = "SCAN_IMPORT_HANDLER_V1"

    def __init__(self, project_repository=None, state_repository=None,
                 job_repository=None, lock_service=None, checkpoint_repository=None,
                 publication_service=None, stager=None, repository_repository=None,
                 import_service=None, inheritance_engine=None,
                 file_state_repository=None, analysis_domain_service=None,
                 project_service=None):
        self.projects = project_repository or ProjectRepository()
        self.states = state_repository or ProjectStateRepository()
        self.jobs = job_repository or JobRepository()
        self.locks = lock_service or RepositoryResourceLockService()
        self.checkpoints = checkpoint_repository or ImportCheckpointRepository()
        self.publication = publication_service or ScanPublicationService(self.states)
        self.stager = stager
        self.repositories = repository_repository or RepositoryRepository()
        self.import_service = import_service
        self.inheritance = inheritance_engine
        self.file_states = file_state_repository or FileStateRepository()
        self.analysis_domain_service = analysis_domain_service
        self.project_service = project_service

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
        if not resource_ids:
            raise ValueError("physical repository resource is required")
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
                for snapshot in repositories:
                    snapshot = dict(snapshot or {})
                    repository_name = str(snapshot.get("repository_name") or "")
                    repository_id = None
                    if repository_name:
                        repository = self.repositories.ensure(
                            conn, project["id"], repository_name,
                            canonical_remote=snapshot.get("canonical_remote", ""),
                            physical_resource_id=snapshot.get("physical_resource_id"),
                            physical_path=snapshot.get("repository_path", ""),
                        )
                        repository_id = repository["id"]
                    self.projects.upsert_repository_snapshot(
                        conn, scan["id"], repository_name,
                        repository_path=snapshot.get("repository_path", ""),
                        branch_name=snapshot.get("branch_name", ""),
                        old_commit_sha=snapshot.get("old_commit_sha"),
                        new_commit_sha=snapshot.get("new_commit_sha"),
                        verified=int(bool(snapshot.get("verified"))),
                        provenance=snapshot.get("provenance", ""),
                        repository_id=repository_id,
                        commit_sha=snapshot.get("commit_sha"),
                        identity_verified=int(bool(snapshot.get("identity_verified", False))),
                        identity_provenance=snapshot.get("identity_provenance", ""),
                    )
                if report:
                    self.projects.bind_report(
                        conn, scan["id"], report["report_id"],
                        report_root=report.get("report_root", ""),
                        source_signature=report.get("source_signature", ""),
                        sidecar_schema=report.get("sidecar_schema", 0),
                        asset_identity=report.get("asset_identity", ""),
                    )
                job_payload = {
                    "project_id": project["id"], "project_name": project_name,
                    "scan_id": scan["id"], "predecessor_scan_id": predecessor,
                    "artifact_id": artifact["artifact_id"],
                    "artifact_sha256": artifact["sha256"],
                    "info_path": os.path.realpath(str(info_path)),
                    "staging_root": os.path.realpath(str(staging_root)),
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

    def execute(self, connection, job_id, owner_token=None, fencing_token=None,
                repository_paths=None):
        """Run a durable import from its immutable staged artifact.

        The source file, Git objects and JSON parsing are verified before the
        mutation transaction.  Every database phase and its checkpoint then
        commit together, so a restart can safely resume at the last durable
        phase without publishing a partially-built Candidate.
        """
        with transaction(connection) as conn:
            job = self.jobs.get(conn, job_id)
            if not job or str(job.get("kind") or "") != "scan_import":
                raise KeyError("scan_import job not found")
            payload = json.loads(job.get("input_payload") or "{}")
            checkpoint = self.checkpoints.get(conn, job_id)
            if not checkpoint:
                raise KeyError("import checkpoint not found")
            locks = self._locks_for_job(conn, job_id)
            owner_token = owner_token or (locks[0].get("owner_token") if locks else "")
            fencing_token = (int(fencing_token) if fencing_token is not None else
                             int(checkpoint.get("fencing_token") or 0))
            self._assert_all_fences(conn, job_id, owner_token, fencing_token,
                                    checkpoint=checkpoint)
            artifact = self._stager(
                payload.get("staging_root") or os.path.dirname(
                    str(payload.get("info_path") or ".")
                )
            ).verify_staged(
                conn, payload.get("artifact_id"),
                expected_sha256=payload.get("artifact_sha256"),
            )
            scan_id = int(payload.get("scan_id") or checkpoint.get("scan_id"))
            snapshots = self.projects.list_repository_snapshots(conn, scan_id)
            terminal_phase = str(checkpoint.get("phase") or "LOCKED")

        if terminal_phase in ("PUBLISHED", "DONE"):
            return self.states.get(connection, int(payload.get("project_id") or 0))
        if terminal_phase == "SEALED":
            return self.publish(connection, job_id)

        if self.import_service is None:
            raise RuntimeError("IMPORT_SERVICE_NOT_CONFIGURED")
        digest, parsed, files = self.import_service.parse_info_file(
            artifact["staged_path"], payload.get("repositories") or []
        )
        if str(digest).lower() != str(artifact.get("sha256") or "").lower():
            raise ValueError("STAGED_ARTIFACT_CHANGED")
        self._verify_git_snapshots(snapshots, payload.get("repositories") or {},
                                   repository_paths=repository_paths)

        project_service = self.project_service
        if project_service is None:
            from app.services.project_service import ProjectService
            project_service = ProjectService(
                self.projects, self.states, repository_repo=self.repositories
            )
        inheritance = self.inheritance
        if inheritance is None:
            from app.inheritance.engine import InheritanceEngine
            inheritance = InheritanceEngine(
                domain_repository=getattr(
                    self.analysis_domain_service, "repository", None
                )
            )
        with transaction(connection) as conn:
            job = self.jobs.get(conn, job_id)
            payload = json.loads(job.get("input_payload") or "{}")
            checkpoint = self.checkpoints.get(conn, job_id)
            locks = self._locks_for_job(conn, job_id)
            owner_token = owner_token or (locks[0].get("owner_token") if locks else "")
            self._assert_all_fences(conn, job_id, owner_token, fencing_token,
                                    checkpoint=checkpoint)
            scan_id = int(payload.get("scan_id") or checkpoint.get("scan_id"))
            project_id = int(payload.get("project_id") or 0)
            data_version = int(job.get("data_version") or 0)
            checkpoint_payload = self._checkpoint_payload(checkpoint)
            read_set = list(checkpoint_payload.get("read_set") or [])
            phase_payload = {
                "scan_id": scan_id,
                "artifact_id": artifact.get("artifact_id"),
                "artifact_sha256": artifact.get("sha256"),
                "file_count": len(files),
                "line_count": sum(len(item.get("lines") or []) for item in files),
                "read_set": read_set,
            }
            checkpoint = self._advance_to(
                conn, checkpoint, job_id, fencing_token, "SCAN_CREATED",
                phase_payload,
            )
            checkpoint = self._advance_to(
                conn, checkpoint, job_id, fencing_token, "INFO_STAGED",
                phase_payload,
            )
            if self._phase_before(checkpoint, "COVERAGE_IMPORTED"):
                project_service._ingest_files(conn, scan_id, files)
            checkpoint = self._advance_to(
                conn, checkpoint, job_id, fencing_token, "COVERAGE_IMPORTED",
                phase_payload,
            )
            checkpoint = self._advance_to(
                conn, checkpoint, job_id, fencing_token, "GIT_VERIFIED",
                phase_payload,
            )
            checkpoint = self._advance_to(
                conn, checkpoint, job_id, fencing_token, "SOURCE_PREPARED",
                phase_payload,
            )
            checkpoint = self._advance_to(
                conn, checkpoint, job_id, fencing_token, "LINE_MAP_BUILT",
                phase_payload,
            )
            if self._phase_before(checkpoint, "INHERITANCE_COMPUTED"):
                path_map = self._repository_path_map(
                    payload.get("repositories") or [], repository_paths
                )
                inheritance_result = inheritance.run(
                    conn, scan_id, repository_paths=path_map
                )
                read_set = list(inheritance_result.get("read_set", read_set) or [])
                phase_payload["read_set"] = read_set
            checkpoint = self._advance_to(
                conn, checkpoint, job_id, fencing_token, "INHERITANCE_COMPUTED",
                phase_payload,
            )
            if self._phase_before(checkpoint, "STATS_REBUILT"):
                self.file_states.rebuild_scan(
                    conn, scan_id, data_version=data_version, file_rows=files
                )
            checkpoint = self._advance_to(
                conn, checkpoint, job_id, fencing_token, "STATS_REBUILT",
                phase_payload,
            )
            if self._phase_before(checkpoint, "CONSISTENCY_VERIFIED"):
                audit = (self.analysis_domain_service.audit_consistency(
                    conn, scan_id=scan_id
                ) if self.analysis_domain_service else {"status": "PASSED"})
                if audit.get("status") != "PASSED":
                    raise ValueError("ANALYSIS_DOMAIN_INCONSISTENT")
            self._advance_to(
                conn, checkpoint, job_id, fencing_token, "CONSISTENCY_VERIFIED",
                dict(phase_payload, consistency="PASSED"),
            )

        self.seal(connection, job_id, owner_token, fencing_token)
        return self.publish(connection, job_id)

    @staticmethod
    def _phase_before(checkpoint, target):
        return PHASES.index(str(checkpoint.get("phase") or "LOCKED")) < PHASES.index(target)

    @staticmethod
    def _checkpoint_payload(checkpoint):
        value = (checkpoint or {}).get("payload")
        if isinstance(value, dict):
            return dict(value)
        try:
            loaded = json.loads(value or "{}")
        except (TypeError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _advance_to(self, connection, checkpoint, job_id, fencing_token, target,
                    payload=None):
        current = checkpoint
        while PHASES.index(str(current.get("phase") or "LOCKED")) < PHASES.index(target):
            current_index = PHASES.index(str(current.get("phase") or "LOCKED"))
            next_phase = PHASES[current_index + 1]
            current = self.checkpoints.advance(
                connection, job_id, current["checkpoint_seq"], fencing_token,
                next_phase, payload=payload,
            )
        return current

    @staticmethod
    def _repository_path_map(repositories, explicit=None):
        result = dict(explicit or {})
        for item in repositories or []:
            item = item or {}
            name = str(item.get("repository_name") or "")
            path = item.get("repository_path") or ""
            if name and path and name not in result:
                result[name] = path
        return result

    def _verify_git_snapshots(self, snapshots, repositories, repository_paths=None):
        paths = self._repository_path_map(repositories, repository_paths)
        for snapshot in snapshots or []:
            old_commit = snapshot.get("old_commit_sha")
            new_commit = snapshot.get("new_commit_sha") or snapshot.get("commit_sha")
            if not old_commit or not new_commit:
                continue
            name = str(snapshot.get("repository_name") or "")
            repo_path = paths.get(name) or snapshot.get("repository_path")
            if not repo_path:
                raise GitTechnicalFailure(
                    "repository path is required for Git verification: {}".format(name)
                )
            provider = GitSnapshotProvider(repo_path)
            provider.ensure_commit(old_commit)
            provider.ensure_commit(new_commit)
            if not provider.is_ancestor(old_commit, new_commit):
                raise ValueError("GIT_ANCESTRY_FAILED")

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
            seal_payload = self._checkpoint_payload(checkpoint)
            seal_payload["scan_id"] = scan_id
            return self.checkpoints.advance(
                conn, job_id, checkpoint["checkpoint_seq"], fencing_token,
                "SEALED", payload=seal_payload,
            )

    def publish(self, connection, job_id, expected_current_scan_id=None,
                read_set=None):
        try:
            return self._publish_transaction(
                connection, job_id,
                expected_current_scan_id=expected_current_scan_id,
                read_set=read_set,
            )
        except Exception as exc:
            # A publish precondition failure must leave a terminal Candidate
            # and release its physical lock.  The original transaction has
            # already rolled back, so the recovery ledger can safely record
            # the failure in a new transaction without risking a half-publish.
            error_code = str(exc or "").split(":", 1)[0]
            scan_status = "ABORTED" if error_code in {
                "CURRENT_POINTER_CHANGED", "PREDECESSOR_MISMATCH",
                "READ_SET_CHANGED", "IMPORT_NOT_SEALED",
            } else "FAILED"
            try:
                from app.scan_import.recovery import ScanImportRecoveryService
                ScanImportRecoveryService(
                    coordinator=self, jobs=self.jobs, projects=self.projects,
                    checkpoints=self.checkpoints, locks=self.locks,
                ).record_failure(
                    connection, job_id, "PUBLISH", type(exc).__name__, str(exc),
                    scan_status=scan_status,
                )
            except Exception:
                # Preserve the original publish error.  Recovery itself is
                # observable through the job/lock audit and must not rewrite
                # the exception that caused the failed publish.
                pass
            raise

    def _publish_transaction(self, connection, job_id,
                             expected_current_scan_id=None, read_set=None):
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
            checkpoint_payload = self._checkpoint_payload(checkpoint)
            persisted_read_set = checkpoint_payload.get("read_set") or []

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
                read_set=(read_set if read_set is not None else persisted_read_set),
                fence=assert_fences,
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
