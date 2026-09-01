"""Run a disposable, real-MariaDB VNext transaction and API integration.

This command is intentionally opt-in. It never targets the repository's
existing ``coverage`` database: ``--create-disposable`` generates a fresh
database name, applies the VNext schema, runs the checks, and drops that
database in ``finally``.
"""

from __future__ import print_function

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.request
from urllib.parse import quote

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from scripts.diagnostics.contract import with_contract
except ModuleNotFoundError:
    from contract import with_contract

import pymysql

from app.bootstrap import VNextRuntime, create_vnext_server
from app.release_identity import (
    generate_release_identity, save_release_manifest,
)
from app.time_utils import utc_iso
from app.code_detail.code_region import FunctionRange
from app.code_detail.sidecar_store import SidecarStore
from app.code_detail.source_reader import (
    SourceContext,
    SourceLineDTO,
    calc_sidecar_file_key,
)
from app.db.manager import DatabaseManager
from app.db.repositories.base import fetchone
from app.db.repositories import FileStateRepository, ProjectStateRepository
from app.scan_import import RepositoryBusyError
from app.services.file_state_service import FileStateService
from scripts.upgrade.migration_runner import apply_schema
from scripts.upgrade.migration_runner import (
    capture_vnext_semantic_snapshot,
    migrate_legacy,
    _stream_vnext_semantic_hash,
    validate_migration_database_separation,
)
from scripts.upgrade.domain_migration import apply_analysis_domain
from scripts.upgrade.legacy_fixture import (
    create_legacy_fixture_schema, seed_legacy_fixture,
)


def _env(name, default=None):
    value = os.environ.get(name)
    return default if value in (None, "") else value


def _revision():
    try:
        return subprocess.check_output(
            ["git", "-C", _REPO_ROOT, "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode("ascii").strip()
    except Exception:
        return ""


def _connect(host, port, user, password, database=None, autocommit=True):
    kwargs = {
        "host": host,
        "port": int(port),
        "user": user,
        "password": password,
        "charset": "utf8mb4",
        "connect_timeout": 5,
        "autocommit": bool(autocommit),
    }
    if database:
        kwargs["database"] = database
    return pymysql.connect(**kwargs)


def _database_name():
    return "coverage_vnext_audit_{}".format(uuid.uuid4().hex[:12])


def _prepare_runtime_release_identity(root):
    """Create the exact manifest required by the disposable runtime root.

    The integration process deliberately runs from a temporary artifact root
    without ``.git`` and without copied frontend assets.  VNext runtime
    verification must still see a real, exact checkout SHA; a missing manifest
    is a production fail-closed condition, not a reason for this harness to
    bypass verification.
    """
    source_identity = generate_release_identity(_REPO_ROOT)
    identity = generate_release_identity(
        root, asset_files=[], commit_sha=source_identity["commit_sha"],
        build_provenance="integration-fixture",
    )
    save_release_manifest(os.path.join(root, "release_manifest.json"), identity)
    return identity


def _command_or_action(args):
    parts = [
        "python scripts/diagnostics/mysql_vnext_integration.py",
        "--create-disposable",
    ]
    if getattr(args, "migration_rehearsal", False):
        parts.append("--migration-rehearsal")
    if getattr(args, "scan_import_rehearsal", False):
        parts.append("--scan-import-rehearsal")
    required_version = str(getattr(args, "require_version_prefix", "") or "")
    if required_version:
        parts.append("--require-version-prefix {}".format(required_version))
    return " ".join(parts)


def _line_records():
    return [{
        "line_number": number,
        "line_text": "audit_line_{}();".format(number),
        "coverage_state": "uncovered",
        "block_start_line": number,
        "block_end_line": number,
        "block_type": "single",
        "function_name": "",
        "function_hash": "",
        "code_line_hash": "audit-line-{}".format(number),
        "code_occurrence": 1,
        "suggested_reviewer": "git-audit" if number == 1 else "",
    } for number in (1, 2)]


def _source_context():
    lines = []
    for number in (1, 2):
        lines.append(SourceLineDTO(
            number,
            source="audit_line_{}();".format(number),
            coverage_state="uncovered",
            analysis_state="未确认",
            is_pending_analysis=True,
            block_start_line=number,
            block_end_line=number,
            block_type="single",
            suggested_reviewer="git-audit" if number == 1 else "",
            is_block_entry=True,
        ))
    return SourceContext(
        "MySQLAudit", "src/mysql_audit.c", lines,
        function_ranges=[FunctionRange(1, 2, "audit")],
        report_id="report_mysql_audit",
    )


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _file_state_ready_evidence(runtime, connection, scan):
    """Return the complete FileState readiness proof for one MariaDB Scan.

    The disposable integration already exercises the production Ready owner
    through ingest, analysis writes and an explicit rebuild.  Keep the
    evidence structured so a compatibility lane cannot pass while only
    checking that a migration marker exists: the state version, completeness,
    pending conservation and authoritative reconciliation must all be present
    and successful.
    """
    state = runtime.states.get(connection, int(scan["project_id"])) or {}
    data_version = int(state.get("data_version") or 0)
    file_state_version = int(state.get("file_state_version") or 0)
    gate = runtime.file_state_service.validate_rebuilt(
        connection, int(scan["project_id"]), int(scan["id"]), data_version
    )
    _assert(
        data_version == file_state_version,
        "MariaDB FileState version was not Ready for the current data version",
    )
    _assert(
        gate.get("status") == "PASSED",
        "MariaDB FileState Ready gate failed: {}".format(gate),
    )
    _assert(
        (gate.get("completeness") or {}).get("status") == "PASSED",
        "MariaDB FileState completeness gate failed",
    )
    _assert(
        (gate.get("pending_conservation") or {}).get("status") == "PASSED",
        "MariaDB pending conservation gate failed",
    )
    _assert(
        (gate.get("reconciliation") or {}).get("status") == "PASSED",
        "MariaDB FileState reconciliation gate failed",
    )
    return {
        "status": "PASSED",
        "project_id": int(scan["project_id"]),
        "scan_id": int(scan["id"]),
        "data_version": data_version,
        "file_state_version": file_state_version,
        "gate": gate,
    }


def _create_database(connection, database, collation="utf8mb4_unicode_ci"):
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE DATABASE `{}` CHARACTER SET utf8mb4 "
            "COLLATE {}".format(database, collation)
        )


def _database_collation(connection):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT @@character_set_database, @@collation_database"
        )
        row = cursor.fetchone()
    return {
        "character_set": str(row[0]),
        "collation": str(row[1]),
    }


def _legacy_file_state_ready_evidence(connection, fixtures):
    """Validate migrated FileState partitions for each generated project."""
    service = FileStateService(FileStateRepository(), ProjectStateRepository())
    evidence = {}
    for project_name, fixture in fixtures.items():
        project = fetchone(
            connection,
            "SELECT id FROM coverage_projects WHERE project_name=?",
            (project_name,),
        )
        _assert(project, "migrated MariaDB project is missing: {}".format(project_name))
        scan = fetchone(
            connection,
            "SELECT id FROM coverage_scans WHERE project_id=? ORDER BY id DESC LIMIT 1",
            (project["id"],),
        )
        _assert(scan, "migrated MariaDB scan is missing: {}".format(project_name))
        state = fetchone(
            connection,
            "SELECT data_version, file_state_version, current_scan_id "
            "FROM coverage_project_state WHERE project_id=?",
            (project["id"],),
        ) or {}
        data_version = int(state.get("data_version") or 0)
        file_state_version = int(state.get("file_state_version") or 0)
        gate = service.validate_rebuilt(
            connection, int(project["id"]), int(scan["id"]), data_version
        )
        aggregate = fetchone(
            connection,
            "SELECT COALESCE(SUM(pending_total), 0) AS pending_total, "
            "COALESCE(SUM(ordinary_pending_total), 0) AS ordinary_pending_total, "
            "COALESCE(SUM(inherited_pending_total), 0) AS inherited_pending_total, "
            "COALESCE(SUM(manual_draft_pending_total), 0) AS manual_draft_pending_total "
            "FROM coverage_file_state WHERE scan_id=?",
            (scan["id"],),
        ) or {}
        expected_ordinary = max(
            0, int(fixture["lines"]) - int(fixture["analyses"])
        )
        expected_drafts = int(fixture.get("drafts") or 0)
        _assert(data_version == file_state_version,
                "migrated MariaDB FileState version is not Ready")
        _assert(gate.get("status") == "PASSED",
                "migrated MariaDB FileState gate failed: {}".format(gate))
        _assert(int(aggregate.get("ordinary_pending_total") or 0)
                == expected_ordinary,
                "migrated MariaDB ordinary pending partition is incorrect")
        _assert(int(aggregate.get("inherited_pending_total") or 0) == 0,
                "migrated MariaDB unexpectedly created inherited pending facts")
        _assert(int(aggregate.get("manual_draft_pending_total") or 0)
                == expected_drafts,
                "migrated MariaDB manual draft partition is incorrect")
        _assert(int(aggregate.get("pending_total") or 0)
                == expected_ordinary + expected_drafts,
                "migrated MariaDB pending partition is not conserved")
        _assert(int(state.get("current_scan_id") or 0) == int(scan["id"]),
                "migrated MariaDB CURRENT scan was not published")
        evidence[project_name] = {
            "status": "PASSED",
            "scan_id": int(scan["id"]),
            "data_version": data_version,
            "file_state_version": file_state_version,
            "expected_ordinary_pending_total": expected_ordinary,
            "expected_manual_draft_pending_total": expected_drafts,
            "aggregate": aggregate,
            "gate": gate,
        }
    return evidence


def _drop_database(host, port, user, password, database):
    connection = None
    try:
        connection = _connect(host, port, user, password)
        with connection.cursor() as cursor:
            cursor.execute("DROP DATABASE IF EXISTS `{}`".format(database))
    finally:
        if connection is not None:
            connection.close()


def _run_legacy_migration_rehearsal(args):
    """Run a disposable real-MariaDB Legacy -> Empty VNext rehearsal.

    The data is a generated fixture, so the result is explicitly marked
    ``synthetic=true``.  It proves MariaDB SQL/transaction compatibility and
    migration idempotency; it cannot satisfy Gate A's separate verified
    production-backup requirement.
    """
    source_database = "coverage_legacy_audit_{}".format(uuid.uuid4().hex[:12])
    target_database = "coverage_target_audit_{}".format(uuid.uuid4().hex[:12])
    source = target = admin = None
    started = time.time()
    try:
        admin = _connect(args.host, args.port, args.user, args.password)
        _create_database(admin, source_database, "utf8mb4_unicode_ci")
        _create_database(admin, target_database, "utf8mb4_general_ci")
        admin.close()
        admin = None

        source_config = {
            "host": args.host, "port": args.port, "user": args.user,
            "password": args.password, "database": source_database,
        }
        target_config = dict(source_config, database=target_database)
        source = _connect(
            args.host, args.port, args.user, args.password,
            database=source_database, autocommit=False,
        )
        target = _connect(
            args.host, args.port, args.user, args.password,
            database=target_database, autocommit=False,
        )
        source_collation = _database_collation(source)
        target_collation = _database_collation(target)
        _assert(
            source_collation["collation"] == "utf8mb4_unicode_ci",
            "MariaDB source rehearsal collation was not utf8mb4_unicode_ci",
        )
        _assert(
            target_collation["collation"] == "utf8mb4_general_ci",
            "MariaDB target rehearsal collation was not utf8mb4_general_ci",
        )
        separation = validate_migration_database_separation(
            source_config, target_config,
            source_connection=source, target_connection=target,
        )
        create_legacy_fixture_schema(source)
        total_lines = max(2, int(args.migration_lines))
        total_analyses = max(1, int(args.migration_analyses))
        first_lines = total_lines // 2
        second_lines = total_lines - first_lines
        first_analyses = min(total_analyses, max(1, total_analyses // 2))
        second_analyses = max(0, total_analyses - first_analyses)
        first_fixture = seed_legacy_fixture(
            source, project_name="fixture-a", line_count=first_lines,
            analysis_count=first_analyses, job_count=int(args.migration_jobs),
            draft_stride=7,
        )
        second_fixture = seed_legacy_fixture(
            source, project_name="fixture-b", line_count=second_lines,
            analysis_count=second_analyses, job_count=0,
        )
        source.commit()

        release_sha = "a" * 40
        schema_path = os.path.join(_REPO_ROOT, "scripts", "upgrade", "vnext_schema.sql")
        first_schema = apply_schema(target, schema_path, release_sha=release_sha)
        first = migrate_legacy(source, target, release_sha=release_sha)
        domain = apply_analysis_domain(target, release_sha=release_sha)
        first_file_state = _legacy_file_state_ready_evidence(
            target, {"fixture-a": first_fixture, "fixture-b": second_fixture}
        )
        source.rollback()
        first_snapshot = capture_vnext_semantic_snapshot(target)

        with target.cursor() as cursor:
            cursor.execute(
                "UPDATE coverage_project_state "
                "SET data_version = data_version + 1"
            )
        mutated_target_hash = _stream_vnext_semantic_hash(target)
        semantic_mutation_rejected = (
            mutated_target_hash != first["target_semantic_hash"]
        )
        target.rollback()
        _assert(
            semantic_mutation_rejected,
            "MariaDB data_version mutation did not change the semantic hash",
        )

        second = migrate_legacy(source, target, release_sha=release_sha)
        second_snapshot = capture_vnext_semantic_snapshot(target)
        second_file_state = _legacy_file_state_ready_evidence(
            target, {"fixture-a": first_fixture, "fixture-b": second_fixture}
        )
        domain_again = apply_analysis_domain(target, release_sha=release_sha)
        second_schema = apply_schema(target, schema_path, release_sha=release_sha)

        counts = {}
        with target.cursor() as cursor:
            for table in (
                    "coverage_projects", "coverage_scans", "coverage_files",
                    "coverage_lines", "coverage_analyses",
                    "coverage_legacy_provenance", "coverage_analysis_records",
                    "coverage_analysis_line_links", "coverage_background_jobs"):
                cursor.execute("SELECT COUNT(*) AS total FROM `{}`".format(table))
                row = cursor.fetchone()
                counts[table] = int(row.get("total") if isinstance(row, dict) else row[0])
        _assert(first["authoritative_semantic_match"],
                "first MariaDB migration semantic hash did not match")
        _assert(second["authoritative_semantic_match"],
                "second MariaDB migration semantic hash did not match")
        _assert(first_snapshot == second_snapshot,
                "MariaDB migration rerun changed semantic target facts")
        _assert(counts["coverage_analysis_line_links"] == counts["coverage_analyses"],
                "Analysis Domain backfill did not conserve analysis/link rows")
        return with_contract({
            "status": "PASSED",
            "evidence_class": "real_mariadb_legacy_migration_rehearsal",
            "database_engine": "MariaDB",
            "database_version": _database_version(args),
            "python_runtime": platform.python_version(),
            "synthetic": True,
            "synthetic_reason": "generated legacy fixture; not production backup evidence",
            "source_database": source_database,
            "target_database": target_database,
            "database_runtime_identity": separation.get("runtime_fingerprint", {}),
            "collations": {
                "source": source_collation,
                "target": target_collation,
            },
            "checks": {
                "source_target_separation": separation,
                "collation_independent_semantic_hash": True,
                "data_version_mutation_changes_semantic_hash": True,
                "core_schema_first_apply": first_schema,
                "core_schema_second_apply": second_schema,
                "legacy_migration_first_run": first,
                "analysis_domain_first_apply": domain,
                "analysis_domain_second_apply": domain_again,
                "file_state_ready_gate": {
                    "first_run": first_file_state,
                    "second_run": second_file_state,
                },
                "semantic_snapshot_stable_on_rerun": True,
                "target_counts": counts,
            },
            "workload": {
                "projects": 2,
                "lines": total_lines,
                "analyses": total_analyses,
                "jobs": int(args.migration_jobs),
            },
            "duration_ms": round((time.time() - started) * 1000, 2),
        })
    finally:
        if source is not None:
            source.close()
        if target is not None:
            target.close()
        if admin is not None:
            admin.close()
        _drop_database(args.host, args.port, args.user, args.password, source_database)
        _drop_database(args.host, args.port, args.user, args.password, target_database)


def _run_scan_import_rehearsal(args, runtime, root):
    """Exercise durable Scan Import recovery on the real MariaDB engine.

    The repository and LCOV input are generated disposable fixtures.  This
    rehearsal is therefore useful for SQL/transaction/fencing compatibility,
    but it is deliberately not production Gate C evidence.
    """
    coordinator = runtime.scan_import_coordinator
    recovery = runtime.scan_import_recovery
    project_name = "MySQLScanImportAudit_{}".format(os.getpid())
    info_path = os.path.join(root, "scan-import-recovery.info")
    staging_root = runtime.scan_import_staging_root
    common_dir = os.path.join(root, "git-common")
    worktree_root = os.path.join(root, "git-worktree")
    os.makedirs(common_dir)
    os.makedirs(worktree_root)
    with open(info_path, "w") as stream:
        stream.write("TN:\nSF:src/recovery.c\nDA:1,0\nend_of_record\n")

    def counts(connection):
        result = {}
        for table in (
                "coverage_scans", "coverage_background_jobs",
                "coverage_import_artifacts", "coverage_import_checkpoints"):
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM `{}`".format(table))
                result[table] = int(cursor.fetchone()[0])
        return result

    repositories = [{
        "repository_name": "repo-recovery",
        "repository_path": worktree_root,
        "branch_name": "main",
        "physical_resource_id": None,
        "verified": True,
    }]
    with runtime.connection_context(read_only=False) as connection:
        resource = runtime.repository_repository.ensure_resource(
            connection, common_dir, worktree_root
        )
        connection.commit()
        resource_id = int(resource["id"])
        repositories[0]["physical_resource_id"] = resource_id
        result = coordinator.create(
            connection, project_name, info_path,
            repository_resource_ids=[resource_id], repositories=repositories,
            staging_root=staging_root, requested_by="mariadb-rehearsal",
        )
        initial_counts = counts(connection)
        initial_state = fetchone(
            connection, "SELECT current_scan_id FROM coverage_project_state "
            "WHERE project_id=?", (result["scan"]["project_id"],)
        )

        busy_rejected = False
        busy_error = ""
        busy_info_path = os.path.join(root, "scan-import-busy.info")
        with open(busy_info_path, "w") as stream:
            stream.write("TN:\nSF:src/busy.c\nDA:1,0\nend_of_record\n")
        try:
            coordinator.create(
                connection, project_name + "_busy", busy_info_path,
                repository_resource_ids=[resource_id], repositories=repositories,
                staging_root=staging_root, requested_by="mariadb-rehearsal",
            )
        except RepositoryBusyError as exc:
            busy_rejected = True
            busy_error = str(exc)
        busy_counts = counts(connection)
        _assert(busy_rejected, "MariaDB busy repository was not rejected")
        _assert(initial_counts == busy_counts,
                "busy import left MariaDB Scan/job/artifact residue")

        old_fence = int(result["locks"][0]["fencing_token"])
        checkpoint = coordinator.advance(
            connection, result["job"]["job_id"], 0, old_fence, "SCAN_CREATED"
        )
        checkpoint = coordinator.advance(
            connection, result["job"]["job_id"], checkpoint["checkpoint_seq"],
            old_fence, "INFO_STAGED"
        )
        checkpoint_before_reclaim = dict(checkpoint)

    os.remove(info_path)

    # The next connection/owner represents the worker restart boundary.
    with runtime.connection_context(read_only=False) as connection:
        validated = recovery.validate(connection, result["job"]["job_id"])
        reclaimed = recovery.reclaim(
            connection, result["job"]["job_id"], staging_root,
            owner_token="restarted-owner-{}".format(uuid.uuid4().hex),
        )
        new_fence = int(reclaimed["locks"][0]["fencing_token"])
        _assert(new_fence > old_fence,
                "MariaDB recovery did not advance the fencing token")
        stale_rejected = False
        stale_error = ""
        try:
            coordinator.advance(
                connection, result["job"]["job_id"],
                checkpoint_before_reclaim["checkpoint_seq"], old_fence,
                "COVERAGE_IMPORTED",
            )
        except ValueError as exc:
            stale_rejected = str(exc) == "STALE_IMPORT_CHECKPOINT"
            stale_error = str(exc)
        _assert(stale_rejected,
                "stale MariaDB worker checkpoint write was not rejected")

    with runtime.connection_context(read_only=False) as connection:
        published_state = coordinator.execute(
            connection, result["job"]["job_id"],
            owner_token=reclaimed["owner_token"], fencing_token=new_fence,
        )
        replay_state = coordinator.execute(
            connection, result["job"]["job_id"],
            owner_token=reclaimed["owner_token"], fencing_token=new_fence,
        )
        scan = fetchone(
            connection, "SELECT status FROM coverage_scans WHERE id=?",
            (result["scan"]["id"],)
        )
        job = fetchone(
            connection, "SELECT state FROM coverage_background_jobs WHERE job_id=?",
            (result["job"]["job_id"],)
        )
        final_checkpoint = fetchone(
            connection, "SELECT phase, fencing_token FROM coverage_import_checkpoints "
            "WHERE job_id=?", (result["job"]["job_id"],)
        )
        final_state = fetchone(
            connection, "SELECT current_scan_id FROM coverage_project_state "
            "WHERE project_id=?", (result["scan"]["project_id"],)
        )
        line_count = fetchone(
            connection, "SELECT COUNT(*) AS total FROM coverage_lines l "
            "JOIN coverage_files f ON f.id=l.file_id WHERE f.scan_id=?",
            (result["scan"]["id"],)
        )
        lock_count = fetchone(
            connection, "SELECT COUNT(*) AS total FROM coverage_repository_resource_locks"
        )

    _assert(initial_state["current_scan_id"] is None,
            "durable Scan Import changed CURRENT before publish")
    _assert(validated["artifact"]["sha256"] == result["artifact"]["sha256"],
            "recovery did not validate the immutable staged artifact")
    _assert(published_state["current_scan_id"] == result["scan"]["id"],
            "durable Scan Import did not publish CURRENT atomically")
    _assert(replay_state["current_scan_id"] == result["scan"]["id"],
            "replaying a published import changed CURRENT")
    _assert(str(scan["status"]).upper() == "SEALED", "scan was not sealed")
    _assert(str(job["state"]).lower() == "completed", "import job was not completed")
    _assert(str(final_checkpoint["phase"]) == "PUBLISHED", "checkpoint was not published")
    _assert(int(final_checkpoint["fencing_token"]) == new_fence,
            "published checkpoint lost its fencing token")
    _assert(int(final_state["current_scan_id"]) == int(result["scan"]["id"]),
            "project CURRENT does not identify the published Scan")
    _assert(int(line_count["total"]) == 1, "recovered import did not ingest one line")
    _assert(int(lock_count["total"]) == 0, "published import did not release its lock")

    return with_contract({
        "status": "PASSED",
        "evidence_class": "real_mariadb_scan_import_rehearsal",
        "database_engine": "MariaDB",
        "database_version": _database_version(args),
        "synthetic": True,
        "synthetic_reason": "generated LCOV/repository fixture; not production Gate C evidence",
        "candidate_revision": _revision(),
        "host_identity": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
        },
        "command_or_action": _command_or_action(args),
        "checks": {
            "busy_repository_rejected": busy_rejected,
            "busy_error": busy_error,
            "busy_path_zero_residue": initial_counts == busy_counts,
            "current_unchanged_until_publish": initial_state["current_scan_id"] is None,
            "immutable_artifact_validated_after_original_deleted": True,
            "checkpoint_phase_before_reclaim": checkpoint_before_reclaim["phase"],
            "recovery_handler_version_validated": True,
            "fencing_token_monotonic": new_fence > old_fence,
            "stale_checkpoint_write_rejected": stale_rejected,
            "stale_checkpoint_error": stale_error,
            "atomic_current_publish": True,
            "repeated_recovery_idempotent": replay_state["current_scan_id"] == result["scan"]["id"],
            "scan_sealed": str(scan["status"]).upper() == "SEALED",
            "job_completed": str(job["state"]).lower() == "completed",
            "locks_released": int(lock_count["total"]) == 0,
        },
        "workload": {
            "project": project_name,
            "scan_id": result["scan"]["id"],
            "line_count": int(line_count["total"]),
            "old_fencing_token": old_fence,
            "reclaimed_fencing_token": new_fence,
        },
    })


def run(args):
    admin = None
    manager = None
    runtime = None
    server = None
    server_thread = None
    database = _database_name()
    checks = {}
    started_at = utc_iso()
    root = tempfile.mkdtemp(prefix="vnext-mysql-audit-")
    report_root = os.path.join(root, "report")
    os.makedirs(report_root)
    _prepare_runtime_release_identity(root)
    project_name = "MySQLAudit_{}".format(os.getpid())
    required_version_prefix = str(
        getattr(args, "require_version_prefix", "") or ""
    )
    observed_database_version = ""

    try:
        if required_version_prefix:
            observed_database_version = _database_version(args)
            checks["runtime_version_requirement"] = validate_runtime_version(
                observed_database_version, required_version_prefix
            )
        migration_rehearsal = None
        if args.migration_rehearsal:
            migration_rehearsal = _run_legacy_migration_rehearsal(args)
            checks["legacy_to_vnext_migration_rehearsal"] = migration_rehearsal
        admin = _connect(args.host, args.port, args.user, args.password)
        with admin.cursor() as cursor:
            cursor.execute(
                "CREATE DATABASE `{}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci".format(
                    database
                )
            )
        admin.close()
        admin = None

        schema_connection = _connect(
            args.host, args.port, args.user, args.password, database=database,
            autocommit=False,
        )
        try:
            apply_schema(
                schema_connection,
                os.path.join(_REPO_ROOT, "scripts/upgrade/vnext_schema.sql"),
                release_sha="mysql-audit",
            )
            apply_analysis_domain(schema_connection, release_sha="mysql-audit")
            schema_connection.commit()
            schema_row = fetchone(
                schema_connection,
                "SELECT schema_version, release_sha FROM coverage_schema_meta "
                "WHERE schema_key = ?",
                ("coverage_vnext",),
            )
            _assert(schema_row and int(schema_row["schema_version"]) == 1,
                    "VNext schema marker was not applied")
            domain_row = fetchone(
                schema_connection,
                "SELECT schema_version, release_sha FROM coverage_schema_meta "
                "WHERE schema_key = ?",
                ("coverage_analysis_domain",),
            )
            _assert(domain_row and int(domain_row["schema_version"]) == 2,
                    "Analysis Domain constraint marker was not applied")
            checks["schema_applied"] = True
        finally:
            schema_connection.close()

        config = {
            "project_name": project_name,
            "runtime_mode": "vnext",
            "schema_version": 1,
            "mysql": {
                "host": args.host,
                "port": int(args.port),
                "user": args.user,
                "password": args.password,
                "database": database,
                "charset": "utf8mb4",
                "connect_timeout": 5,
                "idle_ping_after_sec": 60,
                "retry_read_operations": True,
            },
            "auth": {"mode": "disabled"},
            "runtime_state": {"root": os.path.join(root, "state")},
            "report_roots": [report_root],
            "input_roots": [root],
            "jobs": {"max_workers": 1, "max_queue_size": 4},
        }
        manager = DatabaseManager(config)
        runtime = VNextRuntime(config, root, database_manager=manager)
        if getattr(args, "scan_import_rehearsal", False):
            checks["durable_scan_import_rehearsal"] = _run_scan_import_rehearsal(
                args, runtime, root
            )
        context = _source_context()
        file_path = "src/mysql_audit.c"
        file_key = calc_sidecar_file_key(file_path, "repo-a")
        SidecarStore([report_root], chunk_size=2).save_chunked_sidecar(
            report_root, "report_mysql_audit", file_key, context
        )

        with runtime.connection_context(read_only=False) as connection:
            scan = runtime.project_service.create_scan_and_ingest(
                connection,
                project_name,
                [{
                    "repository_name": "repo-a",
                    "file_path": file_path,
                    "file_path_hash": "",
                    "source_file_name": "mysql_audit.c",
                    "lines": _line_records(),
                }],
                info_file_name="mysql_audit.info",
                info_sha256="b" * 64,
                repositories=[{
                    "repository_name": "repo-a",
                    "repository_path": os.path.join(root, "repo-a"),
                    "branch_name": "main",
                    "verified": True,
                }],
                report={
                    "report_id": "report_mysql_audit",
                    "report_root": report_root,
                    "sidecar_schema": 2,
                    "asset_identity": "mysql-audit-v1",
                },
            )
            runtime.report_registry.register(
                "report_mysql_audit", [report_root], sidecar_required=True,
                report_root=report_root, scan_id=scan["id"],
            )
        _assert(str(scan["status"]) in ("ready", "sealed"),
                "scan was not sealed after MySQL ingest")
        checks["bulk_scan_ingest_and_seal"] = True
        with runtime.connection_context(read_only=True) as connection:
            ready_after_ingest = _file_state_ready_evidence(
                runtime, connection, scan
            )

        application = runtime.application()
        status, layout = application.dispatch(
            "GET", "/api/coverage/code-layout",
            query={
                "scan_id": scan["id"], "report_id": "report_mysql_audit",
                "repository_name": "repo-a", "file_path": file_path,
            },
        )
        _assert(status == 200 and layout["total_lines"] == 2,
                "real MySQL code layout request failed")
        status, lines = application.dispatch(
            "POST", "/api/coverage/code-lines/batch",
            body={
                "scan_id": scan["id"], "report_id": "report_mysql_audit",
                "repository_name": "repo-a", "file_path": file_path,
                "ranges": [{"start_line": 1, "end_line": 2}],
            },
        )
        _assert(status == 200 and len(lines["batches"][0]["lines"]) == 2,
                "real MySQL code-lines batch request failed")
        checks["code_detail_http_contract"] = True

        status, saved = application.dispatch(
            "POST", "/api/coverage/analysis",
            body={
                "project_name": project_name,
                "scan_id": scan["id"],
                "repository_name": "repo-a",
                "file_path": file_path,
                "records": [{
                    "line_start": 1, "line_end": 1,
                    "file_path": file_path, "repository_name": "repo-a",
                    "status": "可覆盖", "reviewer": "mysql-reviewer",
                    "coverage_method": "mysql-test", "is_draft": False,
                }],
            },
        )
        _assert(status == 200 and int(saved["saved"]) == 1,
                "real MySQL bulk analysis save failed")
        checks["bulk_analysis_upsert"] = True
        with runtime.connection_context(read_only=True) as connection:
            ready_after_analysis = _file_state_ready_evidence(
                runtime, connection, scan
            )

        with runtime.connection_context(read_only=False) as connection:
            runtime.progress_service.rebuild(connection, project_name, scan["id"])
        with runtime.connection_context(read_only=True) as connection:
            ready_after_rebuild = _file_state_ready_evidence(
                runtime, connection, scan
            )
        checks["file_state_ready_gate"] = {
            "after_ingest": ready_after_ingest,
            "after_analysis": ready_after_analysis,
            "after_explicit_rebuild": ready_after_rebuild,
        }
        status, summary = application.dispatch(
            "GET", "/api/coverage/progress",
            query={"project": project_name, "scan_id": scan["id"]},
        )
        _assert(
            status == 200
            and summary.get("source") == "coverage_file_state"
            and int(summary.get("confirmed_total") or 0) == 1
            and int(summary.get("pending_total") or 0) == 1,
            "real MySQL SQL progress aggregate is incorrect",
        )
        checks["sql_progress_aggregate"] = True

        status, files_page = application.dispatch(
            "GET", "/api/coverage/progress/files",
            query={
                "project": project_name, "scan_id": scan["id"],
                "page_size": 1,
            },
        )
        _assert(
            status == 200
            and int(files_page.get("data_version") or -1)
                == int(summary.get("data_version") or -2)
            and len(files_page.get("files") or []) == 1
            and int(files_page["files"][0].get("pending_total") or 0) == 1,
            "real MySQL FileState files page is incorrect",
        )
        files_status = status
        status, pending_page = application.dispatch(
            "GET", "/api/coverage/progress/pending",
            query={
                "project": project_name, "scan_id": scan["id"],
                "page_size": 1,
            },
        )
        _assert(
            status == 200
            and int(pending_page.get("total") or 0) == 1
            and len(pending_page.get("rows") or []) == 1,
            "real MySQL pending line page is incorrect",
        )
        pending_status = status
        status, incremental_page = application.dispatch(
            "GET", "/api/coverage/incremental/unanalyzed",
            query={
                "project": project_name, "scan_id": scan["id"],
                "page_size": 1,
            },
        )
        _assert(
            status == 200
            and int(incremental_page.get("data_version") or -1)
                == int(summary.get("data_version") or -2)
            and len(incremental_page.get("files") or []) == 1
            and int(incremental_page["files"][0].get("unanalyzed") or 0) == 1,
            "real MySQL incremental pending page is incorrect",
        )
        checks["file_state_paged_reads"] = {
            "files": {
                "status": files_status,
                "rows": len(files_page.get("files") or []),
            },
            "pending": {
                "status": pending_status,
                "total": int(pending_page.get("total") or 0),
                "rows": len(pending_page.get("rows") or []),
            },
            "incremental": {
                "status": status,
                "rows": len(incremental_page.get("files") or []),
            },
        }

        with runtime.connection_context(read_only=False) as connection:
            before = fetchone(
                connection,
                "SELECT data_version FROM coverage_project_state WHERE project_id = ?",
                (scan["project_id"],),
            )
            before_analysis = fetchone(
                connection,
                "SELECT COUNT(*) AS total FROM coverage_analyses",
            )
            try:
                runtime.analysis_service.save(
                    connection, project_name, scan["id"], [{
                        "file_path": file_path, "repository_name": "repo-a",
                        "line_number": 999, "status": "可覆盖",
                    }], reviewer="should-rollback",
                )
            except (KeyError, ValueError):
                pass
            else:
                raise AssertionError("invalid MySQL analysis write unexpectedly succeeded")
            after = fetchone(
                connection,
                "SELECT data_version FROM coverage_project_state WHERE project_id = ?",
                (scan["project_id"],),
            )
            after_analysis = fetchone(
                connection,
                "SELECT COUNT(*) AS total FROM coverage_analyses",
            )
        _assert(before["data_version"] == after["data_version"],
                "failed MySQL transaction changed data_version")
        _assert(before_analysis["total"] == after_analysis["total"],
                "failed MySQL transaction left analysis rows")
        checks["transaction_rollback"] = True

        # Exercise the actual stdlib HTTP transport with a second manager
        # reference so closing the server cannot close the first runtime's pool.
        http_manager = DatabaseManager(config)
        server = create_vnext_server(
            ("127.0.0.1", 0), config, repo_root=root,
            database_manager=http_manager,
        )
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        url = "http://127.0.0.1:{}/api/coverage/progress?project={}&scan_id={}".format(
            server.server_address[1],
            quote(project_name),
            scan["id"],
        )
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        _assert(payload.get("source") == "coverage_file_state",
                "real MySQL HTTP progress response is incorrect")
        checks["real_http_transport"] = True
        server.shutdown()
        server_thread.join(timeout=5)
        server.server_close()
        server = None
        server_thread = None

        health = manager.health()
        checks["pool_runtime_health"] = (
            int(health.get("acquires") or 0) > 0
            and int(health.get("rollbacks") or 0) > 0
        )
        _assert(checks["pool_runtime_health"],
                "real MySQL pool health did not record request/rollback activity")

        return with_contract({
            "status": "PASSED",
            "evidence_class": "real_mariadb_vnext_integration",
            "database_engine": "MariaDB",
            "database_version": observed_database_version or _database_version(args),
            "required_version_prefix": required_version_prefix,
            "python_runtime": platform.python_version(),
            "synthetic": True,
            "synthetic_reason": "generated disposable input/report; real database runtime only",
            "candidate_revision": _revision(),
            "release_identity": generate_release_identity(repo_root=_REPO_ROOT),
            "host_identity": {
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
            },
            "command_or_action": _command_or_action(args),
            "started_at": started_at,
            "finished_at": utc_iso(),
            "exit_code": 0,
            "checks": checks,
            "database": database,
            "disposable": True,
        })
    finally:
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            if server_thread is not None:
                server_thread.join(timeout=5)
            try:
                server.server_close()
            except Exception:
                pass
        if runtime is not None:
            try:
                runtime.close()
            except Exception:
                pass
        if manager is not None:
            try:
                manager.close()
            except Exception:
                pass
        if admin is not None:
            try:
                admin.close()
            except Exception:
                pass
        try:
            admin = _connect(args.host, args.port, args.user, args.password)
            with admin.cursor() as cursor:
                cursor.execute("DROP DATABASE IF EXISTS `{}`".format(database))
            admin.close()
        except Exception:
            # Preserve the original failure; cleanup failure is emitted by the
            # caller as a separate warning rather than masking the assertion.
            pass
        try:
            for dirpath, _, filenames in os.walk(root, topdown=False):
                for filename in filenames:
                    try:
                        os.remove(os.path.join(dirpath, filename))
                    except OSError:
                        pass
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
        except OSError:
            pass


def _database_version(args):
    connection = _connect(args.host, args.port, args.user, args.password)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION()")
            return str(cursor.fetchone()[0])
    finally:
        connection.close()


def validate_runtime_version(observed_version, required_prefix=""):
    """Fail closed when a compatibility rehearsal reaches the wrong engine."""
    observed = str(observed_version or "")
    required = str(required_prefix or "")
    if required and not observed.startswith(required):
        raise ValueError(
            "database version {!r} does not satisfy required prefix {!r}".format(
                observed, required
            )
        )
    return {
        "status": "PASSED",
        "required_version_prefix": required,
        "observed_version": observed,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=_env("COVERAGE_MYSQL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(_env("COVERAGE_MYSQL_PORT", "3306")))
    parser.add_argument("--user", default=_env("COVERAGE_MYSQL_USER", "root"))
    parser.add_argument("--password", default=_env("COVERAGE_MYSQL_PASSWORD", ""))
    parser.add_argument(
        "--create-disposable", action="store_true", required=True,
        help="required safety acknowledgement; only a generated temporary database is used",
    )
    parser.add_argument(
        "--migration-rehearsal", action="store_true",
        help="also run a generated Legacy -> Empty VNext MariaDB rehearsal",
    )
    parser.add_argument(
        "--scan-import-rehearsal", action="store_true",
        help="also run a generated durable Scan Import restart/fencing rehearsal",
    )
    parser.add_argument("--migration-lines", type=int, default=90000)
    parser.add_argument("--migration-analyses", type=int, default=51000)
    parser.add_argument("--migration-jobs", type=int, default=1)
    parser.add_argument(
        "--require-version-prefix", default="",
        help="fail unless SELECT VERSION() starts with this prefix",
    )
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except Exception as exc:
        result = with_contract({
            "status": "FAILED",
            "evidence_class": "real_mariadb_vnext_integration",
            "database_engine": "MariaDB",
            "required_version_prefix": str(
                getattr(args, "require_version_prefix", "") or ""
            ),
            "python_runtime": platform.python_version(),
            "synthetic": True,
            "synthetic_reason": "generated disposable input/report; real database runtime only",
            "candidate_revision": _revision(),
            "release_identity": generate_release_identity(repo_root=_REPO_ROOT),
            "host_identity": {
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
            },
            "command_or_action": _command_or_action(args),
            "started_at": utc_iso(),
            "finished_at": utc_iso(),
            "exit_code": 1,
            "violations": ["{}: {}".format(type(exc).__name__, exc)],
            "disposable": True,
        })
    # MariaDB returns DATETIME columns as ``datetime`` objects through
    # PyMySQL.  Evidence must remain JSON-serializable without silently
    # dropping those timestamp facts.
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
