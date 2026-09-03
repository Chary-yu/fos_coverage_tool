"""Existing-VNext to the next VNext runtime upgrade.

The source connection is a read-only witness.  All schema and projection
writes are directed to the disposable blue/green target connection.  The
fact snapshot deliberately removes auto-increment identifiers and replaces
foreign keys with stable logical identities so a restored database can be
verified even when physical IDs were allocated differently.
"""

from __future__ import print_function

import hashlib
import json
import os
import sys
from datetime import date, datetime, time
from decimal import Decimal

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.db.repositories.base import fetchall, fetchone, is_sqlite
from app.db.repositories.file_state_repository import FileStateRepository
from app.db.repositories.project_state_repository import ProjectStateRepository
from app.services.file_state_service import (
    FileStateReadyGateError, FileStateService,
)
from scripts.upgrade.database_generation import (
    UNKNOWN, VNEXT, inspect_database_generation,
)
from scripts.upgrade.database_identity import assert_separate_connections
from scripts.upgrade.migration_runner import (
    apply_vnext_schema_v3, _table_exists,
)


EXISTING_VNEXT_UPGRADE_ID = "existing-vnext-to-runtime-v3"
FACT_SNAPSHOT_VERSION = "existing-vnext-authoritative-facts-v3"

# This is the only business-table column added after the e9 production
# baseline.  All other VNext runtime/provenance columns are already part of
# that baseline and therefore remain in the semantic conservation contract.
_ADDITIVE_COLUMNS = {
    "coverage_reports": {"report_mode"},
    # These are deterministic compatibility indexes.  runtime-v3 may
    # backfill them on the target, so they are deliberately not authoritative
    # business facts in the source/target conservation hash.
    "coverage_incremental_results": {"incremental_key_hash"},
    "coverage_import_failures": {"failure_key_hash"},
}

_FACT_TABLES = (
    "projects", "repositories", "repository_aliases", "repository_resources",
    "repository_resource_locks", "scans", "scan_repositories", "reports",
    "files", "lines", "analyses", "analysis_records", "analysis_blocks",
    "inheritance_groups", "analysis_line_links", "inheritance_decisions",
    "inheritance_rejections", "project_state", "jobs", "incremental_results",
    "legacy_provenance", "import_artifacts", "import_checkpoints",
    "import_failures", "migration_checkpoints",
)


def _json_default(value):
    if isinstance(value, (datetime, date, time, Decimal)):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError("unsupported fact value: {}".format(type(value).__name__))


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=_json_default,
    )


def _hash_records(records):
    digest = hashlib.sha256()
    for record in sorted(records, key=_canonical):
        digest.update(_canonical(record).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _rows(connection, table_name):
    if not _table_exists(connection, table_name):
        return []
    return fetchall(connection, "SELECT * FROM {}".format(table_name))


def _id_map(rows):
    result = {}
    for row in rows:
        if row.get("id") is None:
            continue
        result[int(row["id"])] = row
    return result


def _required_ref(mapping, value, label):
    if value is None:
        return None
    try:
        key = int(value)
    except (TypeError, ValueError):
        raise ValueError("{} reference is not an integer: {}".format(label, value))
    if key not in mapping:
        raise ValueError("{} reference is missing: {}".format(label, key))
    return mapping[key]


def _clean_row(row, drop=(), replacements=None):
    drop = set(drop or ())
    replacements = dict(replacements or {})
    result = {}
    for key, value in row.items():
        if key in drop or key in _ADDITIVE_COLUMNS.get(replacements.get("__table__", ""), set()):
            continue
        result[key] = replacements.get(key, value)
    return result


def _record_key(row, drop=(), replacements=None):
    return hashlib.sha256(_canonical(_clean_row(
        row, drop=drop, replacements=replacements
    )).encode("utf-8")).hexdigest()


def _scan_identity(row):
    value = str(row.get("scan_key") or "").strip()
    if not value:
        raise ValueError("VNext scan has no stable scan_key")
    return value


def _file_identity(row, scan_map):
    scan = _required_ref(scan_map, row.get("scan_id"), "file.scan_id")
    return _canonical({
        "scan_key": _scan_identity(scan),
        "repository_name": row.get("repository_name") or "",
        "file_path_hash": row.get("file_path_hash") or "",
        "file_path": row.get("file_path") or "",
    })


def _line_identity(row, file_map):
    file_row = _required_ref(file_map, row.get("file_id"), "line.file_id")
    return _canonical({
        "file": _file_identity(file_row, _CURRENT_SCAN_ROWS),
        "line_number": int(row.get("line_number") or 0),
    })


# The helper above is intentionally not used directly while building the
# snapshot because the scan map is local to one connection.  This variable is
# set only during that short construction phase and never escapes the call.
_CURRENT_SCAN_ROWS = {}


def _fact_snapshot(connection):
    global _CURRENT_SCAN_ROWS

    project_rows = _rows(connection, "coverage_projects")
    project_map = _id_map(project_rows)
    project_names = {
        key: str(row.get("project_name") or "")
        for key, row in project_map.items()
    }
    if any(not value for value in project_names.values()):
        raise ValueError("VNext project has an empty project_name")

    resource_rows = _rows(connection, "coverage_repository_resources")
    resource_map = _id_map(resource_rows)
    resource_keys = {}
    resource_facts = []
    for row in resource_rows:
        if row.get("id") is None:
            raise ValueError("repository resource has no stable physical id")
        resource_key = str(row.get("resource_key") or "").strip()
        if not resource_key:
            raise ValueError("repository resource has an empty resource_key")
        if resource_key in resource_keys.values():
            raise ValueError("repository resource_key is not unique")
        resource_keys[int(row["id"])] = resource_key
        resource_facts.append(_clean_row(row, drop=("id",)))

    repository_rows = _rows(connection, "coverage_repositories")
    repository_map = _id_map(repository_rows)
    repository_keys = {}
    repository_facts = []
    for row in repository_rows:
        project = _required_ref(
            project_map, row.get("project_id"), "repository.project_id"
        )
        replacements = {}
        physical_resource_id = row.get("physical_resource_id")
        if physical_resource_id is not None:
            resource = _required_ref(
                resource_map, physical_resource_id,
                "repository.physical_resource_id",
            )
            replacements["physical_resource_id"] = resource_keys[int(resource["id"])]
        item = _clean_row(
            row, drop=("id", "project_id"), replacements=replacements,
        )
        item["project_name"] = project.get("project_name")
        repository_facts.append(item)
        repository_keys[int(row["id"])] = _record_key(item)

    scan_rows = _rows(connection, "coverage_scans")
    scan_map = _id_map(scan_rows)
    _CURRENT_SCAN_ROWS = scan_map
    scan_keys = {key: _scan_identity(row) for key, row in scan_map.items()}
    scan_records = []
    for row in scan_rows:
        project = _required_ref(project_map, row.get("project_id"), "scan.project_id")
        replacements = {
            "__table__": "coverage_scans",
            "project_name": project.get("project_name"),
        }
        predecessor_scan_id = row.get("predecessor_scan_id")
        if predecessor_scan_id is not None:
            predecessor = _required_ref(
                scan_map, predecessor_scan_id, "scan.predecessor_scan_id"
            )
            replacements["predecessor_scan_id"] = _scan_identity(predecessor)
        scan_records.append(_clean_row(
            row,
            drop=("id", "project_id"),
            replacements=replacements,
        ))
        scan_records[-1]["project_name"] = project.get("project_name")

    file_rows = _rows(connection, "coverage_files")
    file_map = _id_map(file_rows)
    file_keys = {key: _file_identity(row, scan_map) for key, row in file_map.items()}
    file_records = []
    for row in file_rows:
        scan = _required_ref(scan_map, row.get("scan_id"), "file.scan_id")
        item = _clean_row(
            row,
            drop=("id", "scan_id"),
            replacements={"__table__": "coverage_files"},
        )
        item["scan_key"] = _scan_identity(scan)
        file_records.append(item)

    line_rows = _rows(connection, "coverage_lines")
    line_map = _id_map(line_rows)
    line_keys = {}
    for key, row in line_map.items():
        file = _required_ref(file_map, row.get("file_id"), "line.file_id")
        line_keys[key] = _canonical({
            "file": file_keys[int(file["id"])],
            "line_number": int(row.get("line_number") or 0),
        })
    line_records = []
    for row in line_rows:
        file = _required_ref(file_map, row.get("file_id"), "line.file_id")
        item = _clean_row(row, drop=("id", "file_id"))
        item["file_identity"] = file_keys[int(file["id"])]
        line_records.append(item)

    compatibility_rows = _rows(connection, "coverage_analyses")
    compatibility_map = _id_map(compatibility_rows)
    compatibility_keys = {
        key: _record_key(row, drop=("id", "line_id"))
        for key, row in compatibility_map.items()
    }
    analysis_records = []
    for row in compatibility_rows:
        item = _clean_row(row, drop=("id", "line_id"))
        item["line_identity"] = line_keys[int(row.get("line_id"))]
        analysis_records.append(item)

    record_rows = _rows(connection, "coverage_analysis_records")
    record_map = _id_map(record_rows)
    record_keys = {}
    for key, row in record_map.items():
        replacements = {}
        source_id = row.get("legacy_source_analysis_id")
        if source_id is not None:
            _required_ref(
                compatibility_map, source_id,
                "analysis_record.legacy_source_analysis_id",
            )
            replacements["legacy_source_analysis_id"] = compatibility_keys[int(source_id)]
        record_keys[key] = _record_key(
            row, drop=("id",), replacements=replacements
        )
    record_facts = []
    for row in record_rows:
        replacements = {}
        source_id = row.get("legacy_source_analysis_id")
        if source_id is not None:
            _required_ref(
                compatibility_map, source_id,
                "analysis_record.legacy_source_analysis_id",
            )
            replacements["legacy_source_analysis_id"] = compatibility_keys[int(source_id)]
        record_facts.append(_clean_row(row, drop=("id",), replacements=replacements))

    block_rows = _rows(connection, "coverage_analysis_blocks")
    block_map = _id_map(block_rows)
    block_keys = {}
    for key, row in block_map.items():
        scan = _required_ref(scan_map, row.get("scan_id"), "block.scan_id")
        file = _required_ref(file_map, row.get("file_id"), "block.file_id")
        replacements = {
            "scan_id": _scan_identity(scan),
            "file_id": file_keys[int(file.get("id"))],
        }
        repository_id = row.get("repository_id")
        if repository_id is not None:
            repository = _required_ref(
                repository_map, repository_id, "block.repository_id"
            )
            replacements["repository_id"] = repository_keys[int(repository["id"])]
        record_id = row.get("originating_record_id")
        if record_id is not None:
            record = _required_ref(
                record_map, record_id, "block.originating_record_id"
            )
            replacements["originating_record_id"] = record_keys[int(record["id"])]
        block_keys[key] = _record_key(row, drop=("id",), replacements=replacements)
    block_facts = []
    for row in block_rows:
        scan = _required_ref(scan_map, row.get("scan_id"), "block.scan_id")
        file = _required_ref(file_map, row.get("file_id"), "block.file_id")
        replacements = {
            "scan_id": _scan_identity(scan),
            "file_id": file_keys[int(file.get("id"))],
        }
        repository_id = row.get("repository_id")
        if repository_id is not None:
            repository = _required_ref(
                repository_map, repository_id, "block.repository_id"
            )
            replacements["repository_id"] = repository_keys[int(repository["id"])]
        record_id = row.get("originating_record_id")
        if record_id is not None:
            record = _required_ref(
                record_map, record_id, "block.originating_record_id"
            )
            replacements["originating_record_id"] = record_keys[int(record["id"])]
        block_facts.append(_clean_row(row, drop=("id",), replacements=replacements))

    group_rows = _rows(connection, "coverage_inheritance_groups")
    group_map = _id_map(group_rows)
    group_keys = {}
    for key, row in group_map.items():
        candidate_scan = _required_ref(scan_map, row.get("candidate_scan_id"), "group.candidate_scan_id")
        source_scan = _required_ref(scan_map, row.get("source_scan_id"), "group.source_scan_id")
        source_block = _required_ref(block_map, row.get("source_analysis_block_id"), "group.source_analysis_block_id")
        candidate_file = _required_ref(file_map, row.get("candidate_file_id"), "group.candidate_file_id")
        replacements = {
            "candidate_scan_id": _scan_identity(candidate_scan),
            "source_scan_id": _scan_identity(source_scan),
            "source_analysis_block_id": block_keys[int(source_block.get("id"))],
            "candidate_file_id": file_keys[int(candidate_file.get("id"))],
        }
        repository_id = row.get("repository_id")
        if repository_id is not None:
            repository = _required_ref(
                repository_map, repository_id, "group.repository_id"
            )
            replacements["repository_id"] = repository_keys[int(repository["id"])]
        group_keys[key] = _record_key(row, drop=("id",), replacements=replacements)
    group_facts = []
    for row in group_rows:
        candidate_scan = _required_ref(scan_map, row.get("candidate_scan_id"), "group.candidate_scan_id")
        source_scan = _required_ref(scan_map, row.get("source_scan_id"), "group.source_scan_id")
        source_block = _required_ref(block_map, row.get("source_analysis_block_id"), "group.source_analysis_block_id")
        candidate_file = _required_ref(file_map, row.get("candidate_file_id"), "group.candidate_file_id")
        replacements = {
            "candidate_scan_id": _scan_identity(candidate_scan),
            "source_scan_id": _scan_identity(source_scan),
            "source_analysis_block_id": block_keys[int(source_block.get("id"))],
            "candidate_file_id": file_keys[int(candidate_file.get("id"))],
        }
        repository_id = row.get("repository_id")
        if repository_id is not None:
            repository = _required_ref(
                repository_map, repository_id, "group.repository_id"
            )
            replacements["repository_id"] = repository_keys[int(repository["id"])]
        group_facts.append(_clean_row(row, drop=("id",), replacements=replacements))

    link_rows = _rows(connection, "coverage_analysis_line_links")
    link_map = _id_map(link_rows)
    link_keys = {}
    for key, row in link_map.items():
        scan = _required_ref(scan_map, row.get("scan_id"), "link.scan_id")
        line = _required_ref(line_map, row.get("line_id"), "link.line_id")
        record = _required_ref(record_map, row.get("analysis_record_id"), "link.analysis_record_id")
        replacements = {
            "scan_id": _scan_identity(scan),
            "line_id": line_keys[int(line.get("id"))],
            "analysis_record_id": record_keys[int(record.get("id"))],
        }
        block_id = row.get("analysis_block_id")
        if block_id is not None:
            block = _required_ref(block_map, block_id, "link.analysis_block_id")
            replacements["analysis_block_id"] = block_keys[int(block["id"])]
        group_id = row.get("inheritance_group_id")
        if group_id is not None:
            group = _required_ref(group_map, group_id, "link.inheritance_group_id")
            replacements["inheritance_group_id"] = group_keys[int(group["id"])]
        source_scan_id = row.get("source_scan_id")
        if source_scan_id is not None:
            source_scan = _required_ref(
                scan_map, source_scan_id, "link.source_scan_id"
            )
            replacements["source_scan_id"] = _scan_identity(source_scan)
        source_line_id = row.get("source_line_id")
        if source_line_id is not None:
            source_line = _required_ref(
                line_map, source_line_id, "link.source_line_id"
            )
            replacements["source_line_id"] = line_keys[int(source_line["id"])]
        # source_relation_id is a self/reference identity; the link's own
        # stable identity excludes it so the mapping remains non-recursive.
        source_relation_id = row.get("source_relation_id")
        if source_relation_id is not None:
            _required_ref(link_map, source_relation_id, "link.source_relation_id")
        link_keys[key] = _record_key(
            row, drop=("id", "source_relation_id"), replacements=replacements
        )
    link_facts = []
    for row in link_rows:
        scan = _required_ref(scan_map, row.get("scan_id"), "link.scan_id")
        line = _required_ref(line_map, row.get("line_id"), "link.line_id")
        record = _required_ref(record_map, row.get("analysis_record_id"), "link.analysis_record_id")
        replacements = {
            "scan_id": _scan_identity(scan),
            "line_id": line_keys[int(line.get("id"))],
            "analysis_record_id": record_keys[int(record.get("id"))],
        }
        block_id = row.get("analysis_block_id")
        if block_id is not None:
            block = _required_ref(block_map, block_id, "link.analysis_block_id")
            replacements["analysis_block_id"] = block_keys[int(block["id"])]
        group_id = row.get("inheritance_group_id")
        if group_id is not None:
            group = _required_ref(group_map, group_id, "link.inheritance_group_id")
            replacements["inheritance_group_id"] = group_keys[int(group["id"])]
        source_scan_id = row.get("source_scan_id")
        if source_scan_id is not None:
            source_scan = _required_ref(
                scan_map, source_scan_id, "link.source_scan_id"
            )
            replacements["source_scan_id"] = _scan_identity(source_scan)
        source_line_id = row.get("source_line_id")
        if source_line_id is not None:
            source_line = _required_ref(
                line_map, source_line_id, "link.source_line_id"
            )
            replacements["source_line_id"] = line_keys[int(source_line["id"])]
        relation_id = row.get("source_relation_id")
        if relation_id is not None:
            relation = _required_ref(link_map, relation_id, "link.source_relation_id")
            replacements["source_relation_id"] = link_keys[int(relation["id"])]
        link_facts.append(_clean_row(
            row, drop=("id",), replacements=replacements
        ))

    decision_rows = _rows(connection, "coverage_inheritance_decisions")
    decision_facts = []
    for row in decision_rows:
        replacements = {}
        for field, mapping, label in (
                ("candidate_scan_id", scan_map, "decision.candidate_scan_id"),
                ("source_scan_id", scan_map, "decision.source_scan_id"),
                ("candidate_line_id", line_map, "decision.candidate_line_id"),
                ("source_line_id", line_map, "decision.source_line_id")):
            value = row.get(field)
            if value is not None:
                referenced = _required_ref(mapping, value, label)
                if mapping is scan_map:
                    replacements[field] = _scan_identity(referenced)
                else:
                    replacements[field] = line_keys[int(referenced.get("id"))]
        relation_id = row.get("source_relation_id")
        if relation_id is not None:
            relation = _required_ref(
                link_map, relation_id, "decision.source_relation_id"
            )
            replacements["source_relation_id"] = link_keys[int(relation["id"])]
        decision_facts.append(_clean_row(row, drop=("id",), replacements=replacements))

    rejection_rows = _rows(connection, "coverage_inheritance_rejections")
    rejection_facts = []
    for row in rejection_rows:
        replacements = {}
        for field, mapping, label in (
                ("scan_id", scan_map, "rejection.scan_id"),
                ("line_id", line_map, "rejection.line_id"),
                ("rejected_analysis_record_id", record_map, "rejection.rejected_analysis_record_id"),
        ):
            value = row.get(field)
            if value is not None:
                referenced = _required_ref(mapping, value, label)
                if mapping is scan_map:
                    replacements[field] = _scan_identity(referenced)
                elif mapping is line_map:
                    replacements[field] = line_keys[int(referenced.get("id"))]
                else:
                    replacements[field] = record_keys[int(referenced.get("id"))]
        relation_id = row.get("rejected_relation_id")
        if relation_id is not None:
            relation = _required_ref(
                link_map, relation_id, "rejection.rejected_relation_id"
            )
            replacements["rejected_relation_id"] = link_keys[int(relation["id"])]
        for field, mapping, label in (
                ("rejected_source_scan_id", scan_map, "rejection.rejected_source_scan_id"),
                ("rejected_source_line_id", line_map, "rejection.rejected_source_line_id")):
            value = row.get(field)
            if value is not None:
                referenced = _required_ref(mapping, value, label)
                replacements[field] = (_scan_identity(referenced)
                                       if mapping is scan_map else
                                       line_keys[int(referenced.get("id"))])
        source_relation_id = row.get("rejected_source_relation_id")
        if source_relation_id is not None:
            source_relation = _required_ref(
                link_map, source_relation_id,
                "rejection.rejected_source_relation_id",
            )
            replacements["rejected_source_relation_id"] = link_keys[
                int(source_relation["id"])
            ]
        rejection_facts.append(_clean_row(row, drop=("id",), replacements=replacements))

    state_rows = _rows(connection, "coverage_project_state")
    state_facts = []
    for row in state_rows:
        project = _required_ref(project_map, row.get("project_id"), "state.project_id")
        item = _clean_row(
            row, drop=("project_id", "current_scan_id", "file_state_version", "updated_at")
        )
        item["project_name"] = project.get("project_name")
        current_scan_id = row.get("current_scan_id")
        item["current_scan_key"] = (
            _scan_identity(_required_ref(scan_map, current_scan_id, "state.current_scan_id"))
            if current_scan_id is not None else None
        )
        state_facts.append(item)

    scan_repository_rows = _rows(connection, "coverage_scan_repositories")
    scan_repository_facts = []
    for row in scan_repository_rows:
        scan = _required_ref(scan_map, row.get("scan_id"), "scan_repository.scan_id")
        replacements = {
            "__table__": "coverage_scan_repositories",
        }
        repository_id = row.get("repository_id")
        if repository_id is not None:
            repository = _required_ref(
                repository_map, repository_id, "scan_repository.repository_id"
            )
            replacements["repository_id"] = repository_keys[int(repository["id"])]
        item = _clean_row(
            row, drop=("id", "scan_id"), replacements=replacements
        )
        item["scan_key"] = _scan_identity(scan)
        scan_repository_facts.append(item)

    report_rows = _rows(connection, "coverage_reports")
    report_facts = []
    for row in report_rows:
        scan = _required_ref(scan_map, row.get("scan_id"), "report.scan_id")
        item = _clean_row(
            row, drop=("id", "scan_id"), replacements={"__table__": "coverage_reports"}
        )
        item["scan_key"] = _scan_identity(scan)
        report_facts.append(item)

    job_rows = _rows(connection, "coverage_background_jobs")
    job_facts = []
    for row in job_rows:
        replacements = {"__table__": "coverage_background_jobs"}
        project_id = row.get("project_id")
        scan_id = row.get("scan_id")
        if project_id is not None:
            replacements["project_id"] = _required_ref(project_map, project_id, "job.project_id").get("project_name")
        if scan_id is not None:
            replacements["scan_id"] = _scan_identity(_required_ref(scan_map, scan_id, "job.scan_id"))
        job_facts.append(_clean_row(row, drop=(), replacements=replacements))

    alias_rows = _rows(connection, "coverage_repository_aliases")
    alias_facts = []
    for row in alias_rows:
        project = _required_ref(
            project_map, row.get("project_id"), "repository_alias.project_id"
        )
        repository = _required_ref(
            repository_map, row.get("repository_id"),
            "repository_alias.repository_id",
        )
        alias_facts.append(_clean_row(
            row, drop=("id",), replacements={
                "project_id": project.get("project_name"),
                "repository_id": repository_keys[int(repository["id"])],
            }
        ))

    resource_lock_rows = _rows(connection, "coverage_repository_resource_locks")
    resource_lock_facts = []
    for row in resource_lock_rows:
        resource = _required_ref(
            resource_map, row.get("physical_resource_id"),
            "repository_resource_lock.physical_resource_id",
        )
        resource_lock_facts.append(_clean_row(
            row, replacements={
                "physical_resource_id": resource_keys[int(resource["id"])],
            }
        ))

    incremental_rows = _rows(connection, "coverage_incremental_results")
    incremental_facts = []
    for row in incremental_rows:
        scan = _required_ref(
            scan_map, row.get("scan_id"), "incremental_result.scan_id"
        )
        item = _clean_row(
            row,
            drop=("id", "scan_id"),
            replacements={"__table__": "coverage_incremental_results"},
        )
        item["scan_key"] = _scan_identity(scan)
        incremental_facts.append(item)

    import_artifact_rows = _rows(connection, "coverage_import_artifacts")
    import_artifact_facts = [
        _clean_row(row, drop=()) for row in import_artifact_rows
    ]

    import_checkpoint_rows = _rows(connection, "coverage_import_checkpoints")
    import_checkpoint_facts = []
    for row in import_checkpoint_rows:
        replacements = {}
        for field in ("scan_id", "expected_current_scan_id"):
            value = row.get(field)
            if value is not None:
                scan = _required_ref(
                    scan_map, value, "import_checkpoint.{}".format(field)
                )
                replacements[field] = _scan_identity(scan)
        import_checkpoint_facts.append(_clean_row(
            row, replacements=replacements
        ))

    import_failure_rows = _rows(connection, "coverage_import_failures")
    import_failure_facts = []
    for row in import_failure_rows:
        replacements = {}
        scan_id = row.get("scan_id")
        if scan_id is not None:
            scan = _required_ref(
                scan_map, scan_id, "import_failure.scan_id"
            )
            replacements["scan_id"] = _scan_identity(scan)
        replacements["__table__"] = "coverage_import_failures"
        import_failure_facts.append(_clean_row(
            row, drop=("id",), replacements=replacements
        ))

    migration_checkpoint_rows = _rows(connection, "coverage_migration_checkpoints")
    migration_checkpoint_facts = [
        _clean_row(row, drop=()) for row in migration_checkpoint_rows
    ]

    provenance_rows = _rows(connection, "coverage_legacy_provenance")
    provenance_facts = []
    for row in provenance_rows:
        entity_type = str(row.get("target_entity_type") or "").strip()
        target_id = row.get("target_entity_id")
        if entity_type in ("line", "legacy_analysis"):
            entity_map = line_map if entity_type == "line" else compatibility_map
            entity_keys = line_keys if entity_type == "line" else compatibility_keys
            target = _required_ref(
                entity_map, target_id,
                "legacy_provenance.{}.target_entity_id".format(entity_type),
            )
            target_key = entity_keys[int(target["id"])]
        elif entity_type == "project_state":
            target = _required_ref(
                project_map, target_id,
                "legacy_provenance.project_state.target_entity_id",
            )
            target_key = target.get("project_name")
        elif entity_type == "job":
            # Legacy migration provenance uses a deterministic numeric digest
            # for jobs because VNext jobs are keyed by job_id, not an integer
            # target id.  The source identity is the stable business key.
            target_key = str(row.get("source_identity") or "").strip()
            if not target_key:
                raise ValueError("legacy_provenance.job has no source identity")
        else:
            raise ValueError(
                "legacy_provenance has unsupported target entity type: {}".format(
                    entity_type
                )
            )
        provenance_item = _clean_row(
            row,
            drop=("id", "target_entity_id", "provenance_key_hash"),
        )
        provenance_item["target_entity_key"] = target_key
        provenance_facts.append(provenance_item)

    components = {
        "projects": [_clean_row(row, drop=("id",)) for row in project_rows],
        "repositories": repository_facts,
        "repository_aliases": alias_facts,
        "repository_resources": resource_facts,
        "repository_resource_locks": resource_lock_facts,
        "scans": scan_records,
        "scan_repositories": scan_repository_facts,
        "reports": report_facts,
        "files": file_records,
        "lines": line_records,
        "analyses": analysis_records,
        "analysis_records": record_facts,
        "analysis_blocks": block_facts,
        "inheritance_groups": group_facts,
        "analysis_line_links": link_facts,
        "inheritance_decisions": decision_facts,
        "inheritance_rejections": rejection_facts,
        "project_state": state_facts,
        "jobs": job_facts,
        "incremental_results": incremental_facts,
        "legacy_provenance": provenance_facts,
        "import_artifacts": import_artifact_facts,
        "import_checkpoints": import_checkpoint_facts,
        "import_failures": import_failure_facts,
        "migration_checkpoints": migration_checkpoint_facts,
    }
    component_hashes = {
        name: _hash_records(components.get(name) or [])
        for name in _FACT_TABLES
    }
    semantic_hash = hashlib.sha256(_canonical(component_hashes).encode("utf-8")).hexdigest()
    return {
        "snapshot_version": FACT_SNAPSHOT_VERSION,
        "components": components,
        "counts": {name: len(components.get(name) or []) for name in _FACT_TABLES},
        "component_hashes": component_hashes,
        "semantic_hash": semantic_hash,
    }


def capture_vnext_authoritative_snapshot(connection):
    """Capture authoritative VNext facts with auto IDs removed."""
    return _fact_snapshot(connection)


def compare_vnext_authoritative_facts(source_snapshot, target_snapshot):
    source_snapshot = source_snapshot or {}
    target_snapshot = target_snapshot or {}
    differences = []
    for component in _FACT_TABLES:
        source_hash = (source_snapshot.get("component_hashes") or {}).get(component)
        target_hash = (target_snapshot.get("component_hashes") or {}).get(component)
        if source_hash != target_hash:
            differences.append({
                "component": component,
                "source_hash": source_hash,
                "target_hash": target_hash,
                "source_count": (source_snapshot.get("counts") or {}).get(component, 0),
                "target_count": (target_snapshot.get("counts") or {}).get(component, 0),
            })
    return {
        "status": "PASSED" if not differences else "FAILED",
        "source_semantic_hash": source_snapshot.get("semantic_hash", ""),
        "target_semantic_hash": target_snapshot.get("semantic_hash", ""),
        "differences": differences,
    }


def _project_scan_rows(connection):
    return fetchall(connection, """
        SELECT p.id AS project_id, p.project_name,
               s.id AS scan_id, s.scan_key,
               ps.data_version, ps.file_state_version
        FROM coverage_projects p
        LEFT JOIN coverage_project_state ps ON ps.project_id = p.id
        LEFT JOIN coverage_scans s ON s.id = ps.current_scan_id
        ORDER BY p.project_name
    """)


def _file_state_ready_gate(connection, project_row, service):
    project_id = int(project_row.get("project_id"))
    data_version = int(project_row.get("data_version") or 0)
    scan_id = project_row.get("scan_id")
    if scan_id is None:
        scan_count = fetchone(connection, """
            SELECT COUNT(*) AS count FROM coverage_scans WHERE project_id=?
        """, (project_id,)) or {}
        if int(scan_count.get("count") or 0):
            raise ValueError(
                "MIGRATION FAILED: project has scans but no current_scan: {}".format(
                    project_row.get("project_name")
                )
            )
        return {
            "status": "PASSED", "project_id": project_id, "scan_id": None,
            "data_version": data_version, "reason": "NO_CURRENT_SCAN",
        }
    scan_id = int(scan_id)
    try:
        service.rebuild_validate_and_mark_ready(
            connection, project_id, scan_id, data_version
        )
        gate = service.validate_rebuilt(
            connection, project_id, scan_id, data_version
        )
    except FileStateReadyGateError as exc:
        gate = dict(exc.gate or {})
        gate["status"] = "FAILED"
        gate["reason"] = gate.get("reason") or "FILE_STATE_READY_GATE_FAILED"
        raise ValueError(
            "MIGRATION FAILED: FileState Ready Gate failed for project {} scan {}: {}".format(
                project_id, scan_id, gate
            )
        )
    except Exception as exc:
        raise ValueError(
            "MIGRATION FAILED: FileState rebuild failed for project {} scan {}: {}".format(
                project_id, scan_id, exc
            )
        )
    completeness = gate.get("completeness") or {}
    completeness["orphan_file_state_count"] = int(
        completeness.get("orphan_state_count") or 0
    )
    conservation = gate.get("pending_conservation") or {}
    required = (
        int(completeness.get("expected_file_count") or 0) ==
        int(completeness.get("state_file_count") or 0),
        int(completeness.get("missing_file_count") or 0) == 0,
        int(completeness.get("orphan_file_state_count") or 0) == 0,
        int(completeness.get("stale_file_count") or 0) == 0,
        int(conservation.get("pending_total") or 0) ==
        int(conservation.get("ordinary_pending_total") or 0) +
        int(conservation.get("inherited_pending_total") or 0) +
        int(conservation.get("manual_draft_pending_total") or 0),
        gate.get("reconciliation", {}).get("status") == "PASSED",
        int((fetchone(connection, """
            SELECT file_state_version FROM coverage_project_state WHERE project_id=?
        """, (project_id,)) or {}).get("file_state_version") or 0) == data_version,
    )
    if not all(required) or gate.get("status") != "PASSED":
        raise ValueError(
            "MIGRATION FAILED: FileState Ready Gate did not satisfy all conditions: {}".format(
                gate
            )
        )
    gate["completeness"] = completeness
    gate["explicit_conditions"] = {
        "expected_file_count_equals_file_state_count": required[0],
        "missing_file_count_zero": required[1],
        "orphan_file_state_count_zero": required[2],
        "stale_file_count_zero": required[3],
        "pending_conservation": required[4],
        "authoritative_reconciliation": required[5],
        "file_state_version_equals_data_version": required[6],
    }
    return gate


def upgrade_existing_vnext(source_connection, target_connection,
                            release_sha="", schema_path=""):
    """Upgrade a restored Existing-VNext database into a new VNext target."""
    if source_connection is None or target_connection is None:
        raise ValueError("Existing-VNext upgrade requires source and target connections")
    if source_connection is target_connection:
        raise ValueError("Existing-VNext upgrade source and target must be distinct")
    assert_separate_connections(source_connection, target_connection)
    source_generation = inspect_database_generation(source_connection)
    target_generation = inspect_database_generation(target_connection)
    if source_generation.get("generation") != VNEXT:
        raise ValueError("Existing-VNext source is not classified as VNEXT")
    if target_generation.get("generation") != VNEXT:
        raise ValueError("Existing-VNext target is not classified as VNEXT")

    source_before = capture_vnext_authoritative_snapshot(source_connection)
    target_before = capture_vnext_authoritative_snapshot(target_connection)
    source_target = compare_vnext_authoritative_facts(source_before, target_before)
    if source_target.get("status") != "PASSED":
        raise ValueError(
            "MIGRATION FAILED: target is not a consistent source backup: {}".format(
                source_target
            )
        )

    if not schema_path:
        schema_path = os.path.join(
            ROOT, "scripts", "upgrade", "vnext_schema_v3.sql"
        )
    schema_result = apply_vnext_schema_v3(
        target_connection, schema_path, release_sha=release_sha
    )

    # The only owner allowed to publish FileState readiness is the service
    # already used by the runtime.  Do not duplicate the projection algorithm
    # in this controller.
    service = FileStateService(FileStateRepository(), ProjectStateRepository())
    file_state_gates = []
    for project_row in _project_scan_rows(target_connection):
        file_state_gates.append(
            _file_state_ready_gate(target_connection, project_row, service)
        )

    target_after = capture_vnext_authoritative_snapshot(target_connection)
    source_after = capture_vnext_authoritative_snapshot(source_connection)
    target_integrity = compare_vnext_authoritative_facts(source_before, target_after)
    source_stability = compare_vnext_authoritative_facts(source_before, source_after)
    if target_integrity.get("status") != "PASSED":
        raise ValueError(
            "MIGRATION FAILED: authoritative VNext facts changed: {}".format(
                target_integrity
            )
        )
    if source_stability.get("status") != "PASSED":
        raise ValueError(
            "MIGRATION FAILED: source changed while it was expected to be read-only: {}".format(
                source_stability
            )
        )
    return {
        "status": "PASSED",
        "migration_id": EXISTING_VNEXT_UPGRADE_ID,
        "generation": {"source": VNEXT, "target": VNEXT},
        "schema_migration": schema_result,
        "source_snapshot": {
            "semantic_hash": source_before.get("semantic_hash"),
            "counts": source_before.get("counts"),
        },
        "target_snapshot": {
            "semantic_hash": target_after.get("semantic_hash"),
            "counts": target_after.get("counts"),
        },
        "authoritative_data_integrity": target_integrity,
        "source_read_only_stability": source_stability,
        "file_state_ready_gate": file_state_gates,
        "idempotent_schema": bool(schema_result.get("idempotent")),
    }


# Short aliases are useful to callers that already use the migration runner's
# terminology, while the explicit name remains the public contract.
capture_vnext_fact_snapshot = capture_vnext_authoritative_snapshot
run_existing_vnext_upgrade = upgrade_existing_vnext
