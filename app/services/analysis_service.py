"""Analysis write/read service with one authoritative transaction boundary."""

from app.db.repositories import (
    AnalysisDomainRepository,
    AnalysisRepository,
    FileStateRepository,
    LineIndexRepository,
    ProjectRepository,
    ProjectStateRepository,
)
from app.db.transaction import transaction
from app.db.repositories.base import fetchall
from app.code_detail.source_reader import compute_db_file_path_hash
from app.services.file_state_service import FileStateService


CONFIRMED_STATUSES = ("可覆盖", "无法覆盖", "冗余代码")
VALID_STATUSES = ("", "未确认") + CONFIRMED_STATUSES
MAX_EXPANDED_ANALYSIS_LINES = 20000


class AnalysisService(object):
    def __init__(self, analysis_repo=None, project_repo=None, state_repo=None,
                 line_repo=None, domain_repo=None, file_state_repo=None,
                 file_state_service=None):
        self.analyses = analysis_repo or AnalysisRepository()
        self.projects = project_repo or ProjectRepository()
        self.states = state_repo or ProjectStateRepository()
        self.lines = line_repo or LineIndexRepository()
        self.domain = domain_repo or AnalysisDomainRepository()
        self.file_states = file_state_repo or FileStateRepository()
        self.file_state_service = file_state_service or FileStateService(
            self.file_states, self.states
        )

    def _project(self, connection, project_name=None, project_id=None):
        if project_id is not None:
            row = self.projects.get_project(connection, project_id)
        else:
            row = self.projects.get_project_by_name(connection, project_name)
        if not row:
            raise KeyError("project not found")
        return row

    def save(self, connection, project_name, scan_id, records, reviewer="",
             enforce_current=False):
        project = self._project(connection, project_name=project_name)
        scan = self.projects.get_scan(connection, int(scan_id))
        if not scan or int(scan.get("project_id")) != int(project["id"]):
            raise KeyError("scan is not bound to project")
        records = list(records or [])
        with transaction(connection) as conn:
            # Analysis mutations never create or change the project CURRENT
            # pointer.  Publication owns that state transition exclusively.
            state = self.states.ensure(conn, project["id"])
            if enforce_current and int(state.get("current_scan_id") or 0) != int(scan_id):
                raise ValueError("MUTATION_REQUIRES_CURRENT_SCAN")
            if not records:
                return {"saved": 0, "data_version": state.get("data_version", 0)}
            resolved = self._resolve_records(conn, int(scan_id), records, reviewer)
            self._validate_concurrency(conn, int(scan_id), resolved)
            affected_file_ids = self._save_domain_records(conn, int(scan_id), resolved)
            self.domain.close_active_rejections_many(
                conn, int(scan_id),
                sorted({int(item["line_id"]) for item in resolved}),
                "MANUAL_REANALYSIS",
            )
            # The old table is a compatibility projection for old readers. It
            # is written only after the canonical Record/LineLink transaction
            # and is never read to decide current review state by new code.
            self.analyses.upsert_many(conn, resolved)
            saved = len(resolved)
            next_state = self.states.advance(conn, project["id"])
            next_state = self.file_state_service.rebuild_validate_and_mark_ready_in_transaction(
                conn, project["id"], int(scan_id),
                int(next_state["data_version"])
            )
        return {"saved": saved, "data_version": next_state["data_version"]}

    def _validate_concurrency(self, connection, scan_id, resolved):
        """Validate the complete relation/content CAS bundle before writes."""
        cas_items = [item for item in resolved if
                     item.get("expected_relation_revision") is not None or
                     item.get("expected_record_revision") is not None]
        if not cas_items:
            return
        relation_by_line = self.domain.get_links_many(
            connection, int(scan_id), [item["line_id"] for item in cas_items]
        )
        record_ids = set()
        for item in cas_items:
            relation = relation_by_line.get(int(item["line_id"]))
            if item.get("expected_record_revision") is not None:
                record_id = int(item.get("record_id") or
                                item.get("analysis_record_id") or
                                (relation or {}).get("analysis_record_id") or 0)
                if record_id:
                    record_ids.add(record_id)
        records_by_id = self.domain.get_records_many(connection, record_ids)
        for item in cas_items:
            expected_relation = item.get("expected_relation_revision")
            expected_record = item.get("expected_record_revision")
            line_id = int(item["line_id"])
            relation = relation_by_line.get(line_id)
            if expected_relation is not None:
                actual_relation = int((relation or {}).get("relation_revision") or 0)
                if actual_relation != int(expected_relation):
                    raise ValueError("STALE_RELATION_REVISION")
            if expected_record is not None:
                record_id = int(item.get("record_id") or
                                item.get("analysis_record_id") or
                                (relation or {}).get("analysis_record_id") or 0)
                record = records_by_id.get(record_id)
                if (not record or
                        int(record.get("content_revision") or 0) != int(expected_record)):
                    raise ValueError("STALE_CONTENT_REVISION")

    def _save_domain_records(self, connection, scan_id, resolved):
        """Persist content, exact human ranges and line relations in batches.

        Identity resolution and CAS validation happen before this method.  New
        content records and blocks are collected first and written with bounded
        multi-row INSERTs; the relation repository already performs one bulk
        CAS insert/update.  This keeps one user save at a fixed number of SQL
        phases instead of one Record/Block write per physical line.
        """
        line_rows = {
            int(row["id"]): row for row in self.lines.get_by_ids(
                connection, [item["line_id"] for item in resolved]
            )
        }
        line_file_ids = {
            line_id: int(row["file_id"])
            for line_id, row in line_rows.items()
        }
        groups = {}
        for item in resolved:
            groups.setdefault(int(item["_operation_key"]), []).append(item)

        plans = []
        new_record_values = []
        update_record_values = []
        requested_record_ids = {
            int(items[0].get("record_id") or items[0].get("analysis_record_id") or 0)
            for items in groups.values()
            if int(items[0].get("record_id") or items[0].get("analysis_record_id") or 0)
        }
        active_lines_by_record = self.domain.get_active_line_ids_by_record_ids(
            connection, requested_record_ids
        )
        for operation_key in sorted(groups):
            items = groups[operation_key]
            first = items[0]
            line_ids = [int(item["line_id"]) for item in items]
            requested_record_id = int(
                first.get("record_id") or first.get("analysis_record_id") or 0
            )
            content = None
            record_id = 0
            if requested_record_id:
                current_ids = active_lines_by_record.get(requested_record_id, set())
                if current_ids == set(line_ids):
                    expected_revision = first.get("expected_record_revision")
                    if expected_revision is None:
                        raise ValueError("EXPECTED_RECORD_REVISION_REQUIRED")
                    record_id = requested_record_id
                    update_record_values.append(dict(
                        first,
                        record_id=requested_record_id,
                        expected_record_revision=int(expected_revision),
                        _operation_key=operation_key,
                    ))
                # A shared record is immutable for a partial edit: only the
                # selected physical lines receive a new content record.
            if not record_id:
                new_record_values.append({
                    "_operation_key": operation_key,
                    "status": first.get("status"),
                    "coverage_method": first.get("coverage_method"),
                    "uncovered_reason": first.get("uncovered_reason"),
                    "comment": first.get("comment"),
                })
            file_ids = {
                line_file_ids[int(item["line_id"])]
                for item in items
            }
            plans.append({
                "operation_key": operation_key,
                "items": items,
                "line_ids": line_ids,
                "file_ids": file_ids,
                "record_id": record_id,
                "content": content,
            })

        plans_by_operation = {
            plan["operation_key"]: plan for plan in plans
        }
        if update_record_values:
            updated = self.domain.update_records_many(
                connection, update_record_values
            )
            if len(updated) != len(update_record_values):
                raise RuntimeError("bulk analysis record update is incomplete")
            updated_by_operation = {
                int(item["_operation_key"]): record
                for item, record in zip(update_record_values, updated)
            }
            for operation_key, record in updated_by_operation.items():
                next_plan = plans_by_operation.get(operation_key)
                if next_plan is not None:
                    next_plan["content"] = record

        if new_record_values:
            created = self.domain.create_manual_records_many(
                connection, new_record_values, origin="MANUAL"
            )
            if len(created) != len(new_record_values):
                raise RuntimeError("bulk manual record creation is incomplete")
            for item, record in zip(new_record_values, created):
                plan = plans_by_operation[item["_operation_key"]]
                plan["record_id"] = int(record["id"])
                plan["content"] = record

        block_values = []
        block_keys = []
        links = []
        for plan in plans:
            items = plan["items"]
            record = plan["content"]
            if not record or not plan["record_id"]:
                raise RuntimeError("analysis operation has no content record")
            file_ids = plan["file_ids"]
            if len(file_ids) == 1:
                first = items[0]
                block_keys.append((plan["operation_key"], None))
                block_values.append({
                    "scan_id": scan_id,
                    "file_id": next(iter(file_ids)),
                    "start_line": int(first.get("_range_start") or first["line_number"]),
                    "end_line": int(first.get("_range_end") or first["line_number"]),
                    "record_id": plan["record_id"],
                    "created_by": first.get("reviewer", ""),
                    "verified": True,
                    "content_hash": record.get("content_hash"),
                })
            else:
                # One user operation cannot create a cross-file human block.
                # Split into exact one-line blocks while retaining content.
                for item in items:
                    key = (plan["operation_key"], int(item["line_id"]))
                    block_keys.append(key)
                    block_values.append({
                        "scan_id": scan_id,
                        "file_id": line_file_ids[int(item["line_id"])],
                        "start_line": int(item["line_number"]),
                        "end_line": int(item["line_number"]),
                        "record_id": plan["record_id"],
                        "created_by": item.get("reviewer", ""),
                        "verified": True,
                        "content_hash": record.get("content_hash"),
                    })

        created_blocks = self.domain.create_blocks_many(
            connection, block_values, origin="MANUAL"
        )
        if len(created_blocks) != len(block_keys):
            raise RuntimeError("bulk analysis block creation is incomplete")
        block_by_key = {
            key: block for key, block in zip(block_keys, created_blocks)
        }
        for plan in plans:
            for item in plan["items"]:
                key = (plan["operation_key"], None)
                if len(plan["file_ids"]) != 1:
                    key = (plan["operation_key"], int(item["line_id"]))
                block = block_by_key[key]
                links.append({
                    "scan_id": scan_id, "line_id": item["line_id"],
                    "record_id": plan["record_id"], "block_id": block["id"],
                    "review_state": self._review_state(item),
                    "relation_origin": "MANUAL",
                    "reviewed_by": item.get("reviewer", ""),
                    "reviewed_at": item.get("reviewed_at"),
                    "expected_relation_revision": item.get(
                        "expected_relation_revision"
                    ),
                })
        self.domain.create_links_many(connection, links)
        return sorted({int(file_id) for plan in plans for file_id in plan["file_ids"]})

    @staticmethod
    def _review_state(item):
        if int(bool(item.get("is_draft", item.get("draft", False)))):
            return "MANUAL_DRAFT"
        return "MANUAL_CONFIRMED" if item.get("status") in CONFIRMED_STATUSES else "MANUAL_DRAFT"

    def _resolve_records(self, connection, scan_id, records, reviewer):
        """Resolve all line identities before issuing the single bulk write."""
        resolved = []
        for operation_key, item in enumerate(records or [], 1):
            item = dict(item or {})
            item["_operation_key"] = operation_key
            ranges = item.pop("line_ranges", None)
            if ranges is None and item.get("line_start") is not None:
                line_start = item.pop("line_start")
                line_end = item.pop("line_end", line_start)
                ranges = [(line_start, line_end)]
            if ranges is None and isinstance(item.get("line_numbers"), (list, tuple)):
                ranges = [(line, line) for line in item.pop("line_numbers")]
            if ranges is None and item.get("line_id") and item.get("line_number") is None:
                item["_range_start"] = item.get("line_number") or 0
                item["_range_end"] = item.get("line_number") or 0
                resolved.append(item)
                continue
            if ranges is None:
                ranges = [(item.get("line_number"), item.get("line_number"))]
            for range_item in ranges:
                if isinstance(range_item, dict):
                    start_line = range_item.get("start_line")
                    end_line = range_item.get("end_line", start_line)
                else:
                    start_line, end_line = range_item
                start_line, end_line = int(start_line), int(end_line)
                if start_line < 1 or end_line < start_line:
                    raise ValueError("invalid analysis line range")
                if end_line - start_line + 1 > MAX_EXPANDED_ANALYSIS_LINES:
                    raise ValueError("analysis line range is too large")
                for line_number in range(start_line, end_line + 1):
                    row = dict(item)
                    row["line_number"] = line_number
                    row["_range_start"] = start_line
                    row["_range_end"] = end_line
                    resolved.append(row)
                    if len(resolved) > MAX_EXPANDED_ANALYSIS_LINES:
                        raise ValueError("too many analysis lines in one batch")
        for item in resolved:
            if not item.get("file_path_hash") and item.get("file_path"):
                item["file_path_hash"] = compute_db_file_path_hash(
                    item.get("file_path"), item.get("repository_name", "")
                )
        direct_ids = {int(item["line_id"]) for item in resolved if item.get("line_id")}
        identity_requests = {
            (str(item.get("repository_name") or ""), str(item.get("file_path_hash") or ""))
            for item in resolved if not item.get("line_id")
        }
        direct_rows = {
            int(row["id"]): row for row in self.lines.get_by_ids(connection, direct_ids)
        }
        file_rows = self.projects.get_files_by_identities(
            connection, scan_id, identity_requests
        )
        pair_requests = set()
        deduped = {}
        for item in resolved:
            if item.get("line_id"):
                continue
            identity = (
                str(item.get("repository_name") or ""),
                str(item.get("file_path_hash") or ""),
            )
            file_row = file_rows.get(identity)
            if not file_row:
                raise KeyError("file identity not found")
            pair_requests.add((int(file_row["id"]), int(item.get("line_number") or 0)))
        number_rows = {
            (int(row["file_id"]), int(row["line_number"])): row
            for row in self.lines.get_by_file_numbers(connection, pair_requests)
        }

        for item in resolved:
            if item.get("line_id"):
                line_id = int(item["line_id"])
                line = direct_rows.get(line_id)
                if not line or int(line.get("scan_id")) != int(scan_id):
                    raise KeyError("physical source line is not in scan")
            else:
                identity = (
                    str(item.get("repository_name") or ""),
                    str(item.get("file_path_hash") or ""),
                )
                file_row = file_rows[identity]
                line = number_rows.get((int(file_row["id"]), int(item.get("line_number") or 0)))
                if not line:
                    raise KeyError("physical source line not found")
                line_id = int(line["id"])
            item["line_id"] = line_id
            item["line_number"] = int(item.get("line_number") or line.get("line_number") or 0)
            if not item.get("_range_start"):
                item["_range_start"] = item["line_number"]
            if not item.get("_range_end"):
                item["_range_end"] = item["line_number"]
            if reviewer and not item.get("reviewer"):
                item["reviewer"] = reviewer
            status = item.get("status") or ""
            if status not in VALID_STATUSES:
                raise ValueError("invalid analysis status: {}".format(status))
            # Overlapping ranges are common when a browser retries a batch.
            # Keep the last logical record per physical line and avoid
            # executing duplicate upserts in the same transaction.
            deduped[int(item["line_id"])] = item
        return list(deduped.values())

    @staticmethod
    def _line_by_number(connection, file_id, line_number):
        from app.db.repositories.base import fetchone
        return fetchone(connection, """
            SELECT * FROM coverage_lines WHERE file_id = ? AND line_number = ?
        """, (file_id, int(line_number)))

    @staticmethod
    def _line_identity(connection, line_id):
        from app.db.repositories.base import fetchone
        return fetchone(connection, """
            SELECT l.*, f.scan_id, f.repository_name, f.file_path, f.file_path_hash
            FROM coverage_lines l JOIN coverage_files f ON f.id = l.file_id
            WHERE l.id = ?
        """, (int(line_id),))

    def read_for_file(self, connection, file_id):
        domain_rows = self.domain.read_scan(connection, self._scan_for_file(connection, file_id))
        return [row for row in domain_rows if int(row.get("file_id") or 0) == int(file_id)] \
            if domain_rows else self.analyses.get_by_file(connection, file_id)

    def read_for_scan(self, connection, scan_id):
        rows = self.domain.read_scan(connection, scan_id)
        return rows if rows else self.analyses.get_by_scan(connection, scan_id)

    @staticmethod
    def _scan_for_file(connection, file_id):
        from app.db.repositories.base import fetchone
        row = fetchone(connection, "SELECT scan_id FROM coverage_files WHERE id=?", (int(file_id),))
        return int(row["scan_id"]) if row else 0

    def effective_reviewer(self, connection, line_id):
        from app.db.repositories.base import fetchone
        line = fetchone(connection, "SELECT suggested_reviewer FROM coverage_lines WHERE id = ?",
                        (int(line_id),))
        line_identity = self._line_identity(connection, line_id) or {}
        domain = self.domain.read_line(connection, int(line_identity.get("scan_id") or 0), int(line_id))
        analysis = domain or self.analyses.get_by_line(connection, int(line_id))
        saved = str((analysis or {}).get("reviewed_by", (analysis or {}).get("reviewer", "")) or "").strip()
        suggested = str((line or {}).get("suggested_reviewer") or "").strip()
        return saved or suggested or ""
