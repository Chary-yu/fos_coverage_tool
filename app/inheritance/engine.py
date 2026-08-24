"""Deterministic Analysis Inheritance Engine.

The engine is deliberately conservative: a missing/ambiguous parser or Git
fact produces an ordinary no-inherit decision; infrastructure failures are
raised so the import coordinator can keep the Candidate unpublished.
"""

from __future__ import absolute_import

import hashlib
import json
import os
from collections import OrderedDict

from app.db.repositories.analysis_domain_repository import (
    AnalysisDomainRepository, CARRIED_COVERED, INHERITED_PENDING,
)
from app.db.repositories.base import (
    adapt_sql, bind_chunk_size, execute, fetchall, fetchone,
)
from app.inheritance.cpp_parser import CppSourceAnalyzer
from app.inheritance.dependencies import (
    DependencyResolver, LazySourceAnalysisIndex, SourceAnalysisIndex,
)
from app.inheritance.git_snapshot import GitSnapshotProvider, GitTechnicalFailure
from app.inheritance.line_map import GitLineMapEngine
from app.inheritance.normalizer import normalize_cpp
from app.inheritance.predecessor import PredecessorResolver
from app.inheritance.read_set import ReadSetAccumulator
from app.time_utils import utc_sql


ALGORITHM_VERSION = "inheritance-v1"
NO_INHERIT = "NO_INHERIT"
DEPENDENCY_INDEX_MEMORY_BUDGET_EXHAUSTED = (
    "DEPENDENCY_INDEX_MEMORY_BUDGET_EXHAUSTED"
)


class InheritanceTechnicalFailure(RuntimeError):
    error_class = "INHERITANCE_TECHNICAL_FAILURE"


class InheritanceEngine(object):
    def __init__(self, predecessor=None, line_mapper=None, parser=None,
                 dependency_resolver=None, domain_repository=None,
                 performance=None, max_source_cache_bytes=32 * 1024 * 1024,
                 max_source_cache_total_bytes=None,
                 max_ancestry_cache_entries=2048):
        self.predecessor = predecessor or PredecessorResolver()
        self.line_mapper = line_mapper or GitLineMapEngine()
        self.parser = parser or CppSourceAnalyzer()
        self.dependencies = dependency_resolver or DependencyResolver()
        self.domain = domain_repository or AnalysisDomainRepository()
        self._source_index_cache = OrderedDict()
        self._source_index_cache_sizes = {}
        self._source_index_cache_bytes = 0
        self._source_index_cache_evictions = 0
        self._active_index_pairs = set()
        self._active_source_index_keys = set()
        self._failed_index_pairs = set()
        self._ancestry_cache = OrderedDict()
        self._ancestry_cache_evictions = 0
        self._metrics = {}
        self.max_source_cache_bytes = max(1, int(max_source_cache_bytes))
        self.max_source_cache_total_bytes = max(
            self.max_source_cache_bytes,
            int(max_source_cache_total_bytes or self.max_source_cache_bytes * 4),
        )
        self.max_ancestry_cache_entries = max(1, int(max_ancestry_cache_entries))
        self.performance = performance

    def clear_caches(self):
        """Release commit-scoped source and ancestry cache ownership."""
        self._source_index_cache.clear()
        self._source_index_cache_sizes.clear()
        self._source_index_cache_bytes = 0
        self._active_index_pairs.clear()
        self._active_source_index_keys.clear()
        self._failed_index_pairs.clear()
        self._ancestry_cache.clear()
        self._record_source_cache_metrics()
        self._record_ancestry_cache_metrics()

    def _record_source_cache_metrics(self):
        count = len(self._source_index_cache)
        total = int(self._source_index_cache_bytes)
        evictions = int(self._source_index_cache_evictions)
        candidate_bytes = 0
        candidate_entries = 0
        resolution_bytes = 0
        for index in self._source_index_cache.values():
            stats = index.cache_stats() or {}
            candidate_bytes += int(stats.get("candidate_index_bytes") or 0)
            candidate_entries += int(stats.get("candidate_index_entries") or 0)
            resolution_bytes += int(stats.get("resolution_cache_bytes") or 0)
        self._metrics["source_index_count"] = count
        self._metrics["source_index_total_bytes"] = total
        self._metrics["source_index_evictions"] = evictions
        self._metrics["active_index_pair_count"] = len(self._active_index_pairs)
        self._metrics["active_source_index_count"] = len(
            self._active_source_index_keys
        )
        self._metrics["source_index_pair_budget_failures"] = len(
            self._failed_index_pairs
        )
        self._metrics["candidate_index_entries"] = candidate_entries
        self._metrics["candidate_index_bytes"] = candidate_bytes
        self._metrics["resolution_cache_bytes"] = resolution_bytes
        # Keep the compact names as well; these are the stable cache evidence
        # fields consumed by operators without exposing source paths.
        self._metrics["index_count"] = count
        self._metrics["total_bytes"] = total
        self._metrics["evictions"] = evictions

    def _record_ancestry_cache_metrics(self):
        self._metrics["ancestry_cache_count"] = len(self._ancestry_cache)
        self._metrics["ancestry_cache_evictions"] = int(self._ancestry_cache_evictions)

    @staticmethod
    def _source_index_bytes(index):
        stats = index.cache_stats() or {}
        # ``cache_bytes`` is retained as the parsed-analysis compatibility
        # field.  The outer LRU must account for every index-owned structure.
        return int(stats.get("total_index_bytes", stats.get("cache_bytes")) or 0)

    def _source_index_key(self, provider, commit):
        parser_version = getattr(self.parser, "version", self.parser.__class__.__name__)
        return (provider.repo_path, str(commit), str(parser_version))

    def _active_pair_for_key(self, key):
        return [pair for pair in self._active_index_pairs if key in pair]

    def _mark_index_pair_budget_exhausted(self, pair_key, drop=False):
        if pair_key not in self._failed_index_pairs:
            self._failed_index_pairs.add(pair_key)
        for key in pair_key:
            index = self._source_index_cache.get(key)
            if index is not None:
                index.dependency_index_memory_budget_exhausted = True
        if not drop:
            self._record_source_cache_metrics()
            return
        self._active_index_pairs.discard(pair_key)
        self._active_source_index_keys = set(
            key for pair in self._active_index_pairs for key in pair
        )
        for key in pair_key:
            if key in self._active_source_index_keys:
                continue
            self._source_index_cache.pop(key, None)
            self._source_index_cache_bytes -= int(
                self._source_index_cache_sizes.pop(key, 0)
            )
        self._source_index_cache_bytes = max(0, self._source_index_cache_bytes)
        self._record_source_cache_metrics()

    def _mark_active_pairs_over_budget(self, key):
        if self._source_index_cache_bytes <= self.max_source_cache_total_bytes:
            return
        for pair_key in self._active_pair_for_key(key):
            self._mark_index_pair_budget_exhausted(pair_key)

    def _source_index_changed(self, key, index):
        if key not in self._source_index_cache:
            return
        current = self._source_index_bytes(index)
        previous = int(self._source_index_cache_sizes.get(key) or 0)
        self._source_index_cache_sizes[key] = current
        self._source_index_cache_bytes += current - previous
        self._source_index_cache.move_to_end(key)
        while self._source_index_cache_bytes > self.max_source_cache_total_bytes:
            evicted_key = next(
                (item for item in self._source_index_cache
                 if item != key and item not in self._active_source_index_keys),
                None,
            )
            if evicted_key is None:
                # A single active index is allowed to consume its configured
                # per-index budget; the total budget is always at least that
                # large.  Keep the guard for callers that mutate limits at
                # runtime rather than corrupting the byte accounting.
                break
            self._source_index_cache.pop(evicted_key, None)
            self._source_index_cache_bytes -= int(
                self._source_index_cache_sizes.pop(evicted_key, 0)
            )
            self._source_index_cache_evictions += 1
            if self.performance is not None:
                self.performance.record_cache(evictions=1)
        self._mark_active_pairs_over_budget(key)
        self._record_source_cache_metrics()
        if self.performance is not None:
            self.performance.record_cache(current_bytes=self._source_index_cache_bytes)

    def _ancestry_cache_put(self, key, value):
        self._ancestry_cache[key] = bool(value)
        self._ancestry_cache.move_to_end(key)
        while len(self._ancestry_cache) > self.max_ancestry_cache_entries:
            self._ancestry_cache.popitem(last=False)
            self._ancestry_cache_evictions += 1
            if self.performance is not None:
                self.performance.record_cache(evictions=1)
        self._record_ancestry_cache_metrics()

    def compare_line(self, old_line, new_line, old_analysis=None, new_analysis=None,
                     old_line_number=None, new_line_number=None,
                     old_index=None, new_index=None):
        old_analysis = old_analysis or {}
        new_analysis = new_analysis or {}
        old_line_number = int(old_line_number or old_analysis.get("line_number") or 0)
        new_line_number = int(new_line_number or new_analysis.get("line_number") or 0)
        if any(getattr(index, "dependency_index_memory_budget_exhausted", False)
               for index in (old_index, new_index) if index is not None):
            return self._result(
                False, DEPENDENCY_INDEX_MEMORY_BUDGET_EXHAUSTED
            )
        if normalize_cpp(old_line) != normalize_cpp(new_line):
            return self._result(False, "LINE_CODE_CHANGED")
        old_function = self.parser.function_for_line(old_analysis, old_line_number)
        new_function = self.parser.function_for_line(new_analysis, new_line_number)
        if not old_function or not new_function:
            return self._result(False, "FUNCTION_ID_UNRESOLVED")
        if old_function.identity != new_function.identity:
            return self._result(False, "FUNCTION_CHANGED",
                                function_identity_fingerprint=self._pair_hash(
                                    old_function.identity.fingerprint(),
                                    new_function.identity.fingerprint(),
                                ))
        if old_function.uncertain or new_function.uncertain:
            return self._result(False, "PARSER_UNRELIABLE")
        if old_analysis.get("controls", {}).get(old_line_number, ()) != \
                new_analysis.get("controls", {}).get(new_line_number, ()):
            return self._result(False, "CONTROL_CONTEXT_CHANGED")
        if old_analysis.get("preprocessor", {}).get(old_line_number, ()) != \
                new_analysis.get("preprocessor", {}).get(new_line_number, ()):
            return self._result(False, "PP_CONTEXT_CHANGED")
        old_dep = dict(old_analysis, line_number=old_line_number)
        new_dep = dict(new_analysis, line_number=new_line_number)
        old_context = self._dependency_context(old_analysis, old_line_number)
        new_context = self._dependency_context(new_analysis, new_line_number)
        dependency = self.dependencies.compare(
            old_dep, new_dep, old_line, new_line,
            old_context=old_context, new_context=new_context,
            old_index=old_index, new_index=new_index,
        )
        if any(getattr(index, "dependency_index_memory_budget_exhausted", False)
               for index in (old_index, new_index) if index is not None):
            return self._result(
                False, DEPENDENCY_INDEX_MEMORY_BUDGET_EXHAUSTED
            )
        if not dependency.ok:
            return self._result(False, dependency.reason_code,
                                dependency_fingerprint=dependency.fingerprint)
        return self._result(
            True, "INHERITED",
            function_identity_fingerprint=old_function.identity.fingerprint(),
            control_context_fingerprint=self._pair_hash(
                old_analysis.get("controls", {}).get(old_line_number, ()),
                new_analysis.get("controls", {}).get(new_line_number, ()),
            ),
            preprocessor_context_fingerprint=self._pair_hash(
                old_analysis.get("preprocessor", {}).get(old_line_number, ()),
                new_analysis.get("preprocessor", {}).get(new_line_number, ()),
            ),
            dependency_fingerprint=dependency.fingerprint,
        )

    def run(self, connection, candidate_scan_id, repository_paths=None,
            decision_run_id=None, algorithm_version=ALGORITHM_VERSION,
            collect_decisions=False, batch_size=500):
        # Source indexes are useful within one scan only. Drop historical
        # commit ownership before a long-lived runtime starts the next scan.
        self.clear_caches()
        self._metrics = {
            "parser_candidate_total": 0,
            "parser_unresolved_total": 0,
            "callee_unresolved_total": 0,
            "macro_unresolved_total": 0,
            "const_unresolved_total": 0,
            "parser_cache_hit": 0,
            "parser_cache_miss": 0,
            "parser_unresolved_by_reason": {},
            "callee_unresolved_by_reason": {},
            "dependency_budget_exhausted_by_reason": {},
            "macro_unresolved_by_reason": {},
            "const_unresolved_by_reason": {},
            "relation_total": 0,
            "file_work_units": 0,
            "git_snapshot_total": 0,
            "mapping_total": 0,
            "parser_file_total": 0,
            "decision_read_total": 0,
            "source_files_total": 0,
            "source_files_loaded": 0,
            "source_cache_bytes": 0,
            "source_budget_exhausted": 0,
            "dependency_budget_exhausted_total": 0,
            "dependency_index_memory_budget_exhausted_total": 0,
            "source_candidate_index_builds": 0,
            "source_candidate_index_failures": 0,
            "dependency_resolution_cache_hits": 0,
            "dependency_resolution_cache_misses": 0,
            "dependency_candidate_paths": 0,
            "source_relation_page_peak": 0,
            "target_line_page_peak": 0,
            "read_set_relation_total": 0,
            "read_set_record_observation_total": 0,
        }
        self._record_source_cache_metrics()
        self._record_ancestry_cache_metrics()
        candidate = fetchone(connection, "SELECT * FROM coverage_scans WHERE id=?",
                             (int(candidate_scan_id),))
        if not candidate:
            raise KeyError("candidate scan not found")
        predecessor = self.predecessor.resolve(connection, candidate_scan_id)
        predecessor_id = predecessor.get("predecessor_scan_id")
        repository_resolution = {
            str(item.get("candidate_repository") or ""): item
            for item in (predecessor.get("repositories") or [])
        }
        repository_resolution_available = "repositories" in predecessor
        run_id = decision_run_id or hashlib.sha256(json.dumps({
            "candidate_scan_id": int(candidate_scan_id),
            "predecessor_scan_id": predecessor_id,
            "algorithm_version": algorithm_version,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        decisions = [] if collect_decisions else None
        repository_paths = repository_paths or {}
        read_set = ReadSetAccumulator()

        def persist(candidate_line, relation, reason, result=None, mapping=None):
            decision = self._write_decision(
                connection, run_id, candidate_scan_id, candidate_line, relation,
                reason, algorithm_version, result=result, mapping=mapping,
            )
            if decisions is not None:
                decisions.append(decision)
            return decision

        if not predecessor_id:
            for candidate_line in self._iter_candidate_lines(
                    connection, candidate_scan_id, batch_size=batch_size):
                if self._is_review_candidate(candidate_line):
                    persist(candidate_line, None, "NO_PREDECESSOR")
            summary = self._decision_summary(connection, run_id)
            return self._run_result(
                run_id, decisions, summary, [], self._metrics,
            )

        # Read the source side a file work-unit at a time.  Relation pages and
        # target-line pages are bounded; no full source or candidate file is
        # retained in Python.
        for source_file in self._iter_relation_files(
                connection, predecessor_id, batch_size=batch_size):
            repository_name = str(source_file.get("repository_name") or "")
            source_relation_pages = self._iter_source_relation_pages(
                connection, predecessor_id, int(source_file["file_id"]),
                batch_size=batch_size,
            )
            repository_status = repository_resolution.get(repository_name)
            if repository_resolution_available and (
                    not repository_status or
                    str(repository_status.get("reason_code") or "NO_PREDECESSOR") != "READY"):
                for relation_page in source_relation_pages:
                    self._record_read_set_page(relation_page, read_set)
                continue
            key = (repository_name, str(source_file.get("file_path") or ""))
            target_file = self._candidate_file(
                connection, candidate_scan_id, repository_name, key[1]
            )
            if not target_file:
                for relation_page in source_relation_pages:
                    self._record_read_set_page(relation_page, read_set)
                continue

            source_snapshot = None
            for relation_page in source_relation_pages:
                self._record_read_set_page(relation_page, read_set)
                self._metrics["relation_total"] += len(relation_page)
                if not relation_page:
                    continue
                if source_snapshot is None:
                    relation = relation_page[0]
                    repo_path = repository_paths.get(repository_name)
                    candidate_snapshot = self._repository_snapshot(
                        connection, candidate_scan_id, repository_name
                    )
                    predecessor_snapshot = self._repository_snapshot(
                        connection, predecessor_id, repository_name
                    )
                    source_snapshot = self._snapshot_for_relation(
                        relation, repo_path, predecessor_snapshot,
                        candidate_snapshot,
                    )
                    self._metrics["file_work_units"] += 1
                    if not source_snapshot.get("reason_code"):
                        source_snapshot["old_lines"] = source_snapshot["old_text"].splitlines()
                        source_snapshot["new_lines"] = source_snapshot["new_text"].splitlines()
                        source_snapshot["old_analysis"] = self.parser.analyze(
                            source_snapshot["old_text"], key[1]
                        )
                        source_snapshot["new_analysis"] = self.parser.analyze(
                            source_snapshot["new_text"], key[1]
                        )
                        self._metrics["parser_file_total"] += 2

                if source_snapshot.get("reason_code"):
                    if (source_snapshot.get("reason_code") ==
                            DEPENDENCY_INDEX_MEMORY_BUDGET_EXHAUSTED):
                        self._metrics[
                            "dependency_index_memory_budget_exhausted_total"
                        ] += len(relation_page)
                    for relation in relation_page:
                        persist(None, relation, source_snapshot["reason_code"])
                    continue

                mapping = source_snapshot.get("mapping")
                if mapping is None:
                    mapping = self.line_mapper.map_text(
                        source_snapshot["old_text"], source_snapshot["new_text"]
                    )
                    self._metrics["mapping_total"] += 1
                mapped_relations = []
                target_line_numbers = set()
                for relation in relation_page:
                    new_line_number = mapping.get(
                        int(relation["source_line_number"])
                    )
                    if new_line_number is None:
                        reason = (
                            "LINE_DELETED"
                            if int(relation["source_line_number"]) in mapping.deleted
                            else "LINE_AMBIGUOUS"
                        )
                        persist(None, relation, reason, mapping=mapping)
                        continue
                    new_line_number = int(new_line_number)
                    mapped_relations.append((relation, mapping, new_line_number))
                    target_line_numbers.add(new_line_number)

                target_lines = self._candidate_lines_for_numbers(
                    connection, int(target_file["id"]), target_line_numbers,
                )
                self._metrics["target_line_page_peak"] = max(
                    self._metrics["target_line_page_peak"], len(target_lines)
                )
                for relation, mapping, new_line_number in mapped_relations:
                    target_line = target_lines.get(new_line_number)
                    if not target_line:
                        persist(None, relation, "NO_TARGET_LINE", mapping=mapping)
                        continue
                    self._persist_relation_decision(
                        connection, run_id, candidate_scan_id, predecessor_id,
                        relation, target_line, mapping, source_snapshot, persist,
                    )

        # A source relation is not a license to omit a new candidate line.
        # The anti-join makes retries idempotent without a resident line-id set.
        for candidate_line in self._iter_candidate_lines(
                connection, candidate_scan_id, run_id=run_id,
                batch_size=batch_size):
            if not self._is_review_candidate(candidate_line):
                continue
            repository_name = str(candidate_line.get("repository_name") or "")
            reason = "NO_SOURCE_RELATION"
            if repository_resolution_available:
                repository_reason = str(
                    (repository_resolution.get(repository_name) or {}).get(
                        "reason_code"
                    ) or "NO_PREDECESSOR"
                )
                if repository_reason != "READY":
                    reason = repository_reason
            persist(candidate_line, None, reason)
        summary = self._decision_summary(connection, run_id)
        return self._run_result(
            run_id, decisions, summary,
            read_set.to_payload(
                candidate_scan_id=candidate_scan_id,
                predecessor_scan_id=predecessor_id,
            ),
            self._metrics,
        )

    @staticmethod
    def _run_result(run_id, decisions, summary, read_set, metrics):
        return {
            "status": "PASSED", "decision_run_id": run_id,
            "decisions": decisions if decisions is not None else [],
            "decision_count": int(summary.get("total") or 0),
            "inherited": int(summary.get("inherited") or 0),
            "pending": int(summary.get("pending") or 0),
            "read_set": read_set,
            "metrics": dict(metrics),
        }

    def _persist_relation_decision(self, connection, run_id, candidate_scan_id,
                                   predecessor_id, relation, target_line, mapping,
                                   source_snapshot, persist):
        self._metrics["decision_read_total"] += 1
        existing_decision = fetchone(connection, """
            SELECT * FROM coverage_inheritance_decisions
            WHERE decision_run_id=? AND candidate_line_id=?
        """, (run_id, int(target_line["id"])))
        old_lines = source_snapshot["old_lines"]
        new_lines = source_snapshot["new_lines"]
        old_line_number = int(relation["source_line_number"])
        new_line_number = int(mapping.get(old_line_number))
        old_line_text = (
            old_lines[old_line_number - 1]
            if 0 < old_line_number <= len(old_lines)
            else relation.get("source_line_text") or ""
        )
        new_line_text = (
            new_lines[new_line_number - 1]
            if 0 < new_line_number <= len(new_lines)
            else target_line.get("line_text") or ""
        )
        result = self.compare_line(
            old_line_text, new_line_text,
            source_snapshot["old_analysis"], source_snapshot["new_analysis"],
            old_line_number, new_line_number,
            old_index=source_snapshot.get("old_index"),
            new_index=source_snapshot.get("new_index"),
        )
        self._record_parser_result(result)
        result.mapping_fingerprint = mapping.fingerprint
        if result.ok and (not relation.get("analysis_block_id") or
                          not int(relation.get("block_identity_verified") or 0)):
            result = self._result(
                False, "BLOCK_AMBIGUOUS",
                line_mapping_fingerprint=mapping.fingerprint,
            )
        persist(
            target_line, relation, result.reason_code,
            result=result, mapping=mapping,
        )
        if not result.ok:
            return
        if (existing_decision and
                str(existing_decision.get("decision") or "") == "INHERITED" and
                self._active_link_for_line(
                    connection, candidate_scan_id, target_line["id"]
                )):
            return
        record = self.domain.get_record(
            connection, relation["analysis_record_id"]
        )
        state = (
            CARRIED_COVERED
            if str(target_line.get("coverage_state") or "").lower()
            in ("covered", "1") else INHERITED_PENDING
        )
        clone = self.domain.create_record(connection, {
            "conclusion_status": record.get("conclusion_status", ""),
            "coverage_method": record.get("coverage_method", ""),
            "uncovered_reason": record.get("uncovered_reason", ""),
            "comment": record.get("comment", ""),
        }, origin="MANUAL")
        group_id = self._ensure_group(
            connection, run_id, candidate_scan_id, predecessor_id,
            relation, target_line, mapping,
        )
        self.domain.create_link(
            connection, candidate_scan_id, target_line["id"], clone["id"],
            review_state=state, relation_origin="INHERITANCE",
            inheritance_group_id=group_id,
            source_scan_id=predecessor_id,
            source_line_id=relation["line_id"],
            source_relation_id=relation["id"],
        )

    def _record_read_set_page(self, relation_page, read_set):
        self._metrics["source_relation_page_peak"] = max(
            self._metrics["source_relation_page_peak"], len(relation_page)
        )
        for relation in relation_page:
            relation_id = relation.get("id")
            if relation_id is not None:
                read_set.add_relation(
                    relation_id, relation.get("relation_revision") or 0
                )
                self._metrics["read_set_relation_total"] += 1
            record_id = relation.get("analysis_record_id")
            if record_id is not None and relation.get("source_content_revision") is not None:
                read_set.add_record(
                    record_id, relation.get("source_content_revision") or 0
                )
                self._metrics["read_set_record_observation_total"] += 1

    def _record_parser_result(self, result):
        self._metrics["parser_candidate_total"] += 1
        reason = result.reason_code
        if reason in (
                "FUNCTION_ID_UNRESOLVED", "PARSER_UNRELIABLE",
                "CALLEE_UNRESOLVED", "DEPENDENCY_BUDGET_EXHAUSTED",
                "DEPENDENCY_INDEX_MEMORY_BUDGET_EXHAUSTED",
                "DEPENDENCY_CANDIDATE_INDEX_UNAVAILABLE",
                "MACRO_CHANGED", "CONST_CHANGED"):
            self._metrics["parser_unresolved_total"] += 1
            values = self._metrics["parser_unresolved_by_reason"]
            values[reason] = int(values.get(reason) or 0) + 1
        counter = {
            "CALLEE_UNRESOLVED": "callee_unresolved_total",
            "DEPENDENCY_BUDGET_EXHAUSTED": "dependency_budget_exhausted_total",
            "DEPENDENCY_INDEX_MEMORY_BUDGET_EXHAUSTED":
                "dependency_index_memory_budget_exhausted_total",
            "MACRO_CHANGED": "macro_unresolved_total",
            "CONST_CHANGED": "const_unresolved_total",
        }.get(reason)
        if counter:
            self._metrics[counter] += 1
            values = self._metrics[counter.replace("_total", "_by_reason")]
            values[reason] = int(values.get(reason) or 0) + 1

    def _decision_summary(self, connection, run_id):
        row = fetchone(connection, """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN decision='INHERITED' THEN 1 ELSE 0 END), 0)
                       AS inherited
            FROM coverage_inheritance_decisions WHERE decision_run_id=?
        """, (run_id,)) or {}
        total = int(row.get("total") or 0)
        inherited = int(row.get("inherited") or 0)
        return {"total": total, "inherited": inherited, "pending": total - inherited}

    def _iter_relation_files(self, connection, scan_id, batch_size=500):
        last_id = 0
        while True:
            rows = fetchall(connection, """
                SELECT f.id AS file_id, f.file_path, f.repository_name,
                       f.file_path_hash
                FROM coverage_files f
                WHERE f.scan_id=? AND f.id>? AND EXISTS (
                    SELECT 1 FROM coverage_lines l
                    JOIN coverage_analysis_line_links q ON q.line_id=l.id
                    WHERE l.file_id=f.id AND q.scan_id=? AND q.is_active=1
                )
                ORDER BY f.id LIMIT ?
            """, (int(scan_id), int(last_id), int(scan_id),
                   int(max(1, batch_size))))
            if not rows:
                break
            for row in rows:
                yield row
            last_id = int(rows[-1].get("file_id") or 0)

    def _iter_source_relation_pages(self, connection, scan_id, file_id,
                                    batch_size=500):
        last_id = 0
        while True:
            rows = fetchall(connection, """
                SELECT q.*, l.file_id AS source_file_id,
                       l.line_number AS source_line_number,
                       l.line_text AS source_line_text,
                       l.coverage_state AS source_coverage_state,
                       f.file_path, f.repository_name, f.file_path_hash,
                       b.block_identity_verified, b.id AS source_block_id,
                       r.content_revision AS source_content_revision
                FROM coverage_analysis_line_links q
                JOIN coverage_lines l ON l.id=q.line_id
                JOIN coverage_files f ON f.id=l.file_id
                LEFT JOIN coverage_analysis_blocks b ON b.id=q.analysis_block_id
                LEFT JOIN coverage_analysis_records r ON r.id=q.analysis_record_id
                WHERE q.scan_id=? AND q.is_active=1 AND l.file_id=? AND q.id>?
                ORDER BY q.id LIMIT ?
            """, (int(scan_id), int(file_id), int(last_id), int(max(1, batch_size))))
            if not rows:
                break
            yield rows
            last_id = int(rows[-1].get("id") or 0)

    def _iter_source_relations(self, connection, scan_id, file_id, batch_size=500):
        """Compatibility iterator over the bounded relation pages."""
        for page in self._iter_source_relation_pages(
                connection, scan_id, file_id, batch_size=batch_size):
            for row in page:
                yield row

    def _candidate_file(self, connection, scan_id, repository_name, file_path):
        return fetchone(connection, """
            SELECT * FROM coverage_files
            WHERE scan_id=? AND repository_name=? AND file_path=?
            ORDER BY id LIMIT 1
        """, (int(scan_id), str(repository_name or ""), str(file_path or "")))

    def _iter_file_lines(self, connection, file_id, batch_size=500):
        last_id = 0
        while True:
            rows = fetchall(connection, """
                SELECT l.* FROM coverage_lines l
                WHERE l.file_id=? AND l.id>? ORDER BY l.id LIMIT ?
            """, (int(file_id), int(last_id), int(max(1, batch_size))))
            if not rows:
                break
            for row in rows:
                yield row
            last_id = int(rows[-1].get("id") or 0)

    def _candidate_lines_for_numbers(self, connection, file_id, line_numbers):
        numbers = sorted(set(int(item) for item in (line_numbers or [])))
        if not numbers:
            return {}
        chunk_size = bind_chunk_size(
            connection, parameter_width=1, reserved=1, maximum=500
        )
        result = {}
        for offset in range(0, len(numbers), chunk_size):
            chunk = numbers[offset:offset + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            rows = fetchall(connection, """
                SELECT l.* FROM coverage_lines l
                WHERE l.file_id=? AND l.line_number IN ({})
            """.format(placeholders), [int(file_id)] + chunk)
            for row in rows:
                result[int(row.get("line_number") or 0)] = row
        return result

    def _iter_candidate_lines(self, connection, scan_id, run_id=None,
                              batch_size=500):
        last_id = 0
        while True:
            decision_join = ""
            decision_filter = ""
            params = []
            if run_id:
                decision_join = """
                    LEFT JOIN coverage_inheritance_decisions d
                      ON d.candidate_line_id=l.id AND d.decision_run_id=?
                """
                params.append(str(run_id))
                decision_filter = " AND d.id IS NULL"
            params.extend((int(scan_id), int(last_id), int(max(1, batch_size))))
            rows = fetchall(connection, """
                SELECT l.*, f.file_path, f.repository_name, f.file_path_hash
                FROM coverage_lines l JOIN coverage_files f ON f.id=l.file_id
                {decision_join}
                WHERE f.scan_id=? AND l.id>? {decision_filter}
                ORDER BY l.id LIMIT ?
            """.format(decision_join=decision_join, decision_filter=decision_filter), params)
            if not rows:
                break
            for row in rows:
                yield row
            last_id = int(rows[-1].get("id") or 0)

    @staticmethod
    def _read_set_for_relations(relations):
        """Return the legacy list form for compatibility/unit-test callers.

        The relation revision protects the line-level mapping/review fact and
        the content revision protects the AnalysisRecord payload.  Runtime
        inheritance uses :class:`ReadSetAccumulator` instead, so a durable
        checkpoint is not proportional to the number of source relations.
        """
        relations_by_id = {}
        records_by_id = {}
        for relation in relations or []:
            relation_id = relation.get("id")
            if relation_id is not None:
                relations_by_id[int(relation_id)] = {
                    "relation_id": int(relation_id),
                    "relation_revision": int(relation.get("relation_revision") or 0),
                }
            record_id = relation.get("analysis_record_id")
            if record_id is not None and relation.get("source_content_revision") is not None:
                records_by_id[int(record_id)] = {
                    "record_id": int(record_id),
                    "content_revision": int(relation.get("source_content_revision") or 0),
                }
        return sorted(
            list(relations_by_id.values()) + list(records_by_id.values()),
            key=lambda item: (0 if "relation_id" in item else 1,
                              int(item.get("relation_id", item.get("record_id", 0)))),
        )

    def _ordinary_pending_decisions(self, connection, scan_id, run_id, version, reason):
        rows = fetchall(connection, """
            SELECT l.* FROM coverage_lines l JOIN coverage_files f ON f.id=l.file_id
            WHERE f.scan_id=? AND LOWER(COALESCE(l.coverage_state,'')) IN
                ('uncovered','uncovered_line','uncovered-code','0','未覆盖')
        """, (int(scan_id),))
        return [self._write_decision(connection, run_id, scan_id, row, None,
                                      reason, version) for row in rows]

    @staticmethod
    def _is_review_candidate(row):
        return str(row.get("coverage_state") or "").lower() in (
            "uncovered", "uncovered_line", "uncovered-code", "0", "未覆盖"
        )

    @staticmethod
    def _active_link_for_line(connection, scan_id, line_id):
        return fetchone(connection, """
            SELECT id FROM coverage_analysis_line_links
            WHERE scan_id=? AND line_id=? AND is_active=1
        """, (int(scan_id), int(line_id)))

    def _repository_snapshot(self, connection, scan_id, repository_name):
        return fetchone(connection, """
            SELECT s.*, r.canonical_remote
            FROM coverage_scan_repositories s
            LEFT JOIN coverage_repositories r ON r.id=s.repository_id
            WHERE s.scan_id=? AND s.repository_name=?
        """, (int(scan_id), str(repository_name or ""))) or {}

    def _snapshot_for_relation(self, relation, repo_path, old_snapshot, new_snapshot):
        old_branch = str(old_snapshot.get("branch_name") or "").strip()
        new_branch = str(new_snapshot.get("branch_name") or "").strip()
        if old_branch and new_branch and old_branch != new_branch:
            return {"reason_code": "BRANCH_MISMATCH"}
        old_commit = old_snapshot.get("commit_sha")
        new_commit = new_snapshot.get("commit_sha")
        if old_commit and new_commit and not (
                int(old_snapshot.get("identity_verified") or 0) and
                int(new_snapshot.get("identity_verified") or 0)):
            return {"reason_code": "REPOSITORY_IDENTITY_UNVERIFIED"}
        if not old_commit or not new_commit:
            return {"reason_code": "REPOSITORY_IDENTITY_UNVERIFIED"}
        if repo_path and old_commit and new_commit:
            try:
                provider = GitSnapshotProvider(
                    repo_path,
                    fetch_remote=(old_snapshot.get("canonical_remote") or
                                  new_snapshot.get("canonical_remote") or None),
                    performance=self.performance,
                )
                provider.ensure_commit(old_commit)
                provider.ensure_commit(new_commit)
                ancestry_key = (provider.repo_path, str(old_commit), str(new_commit))
                if ancestry_key in self._ancestry_cache:
                    self._ancestry_cache.move_to_end(ancestry_key)
                    self._record_ancestry_cache_metrics()
                else:
                    self._ancestry_cache_put(
                        ancestry_key, provider.is_ancestor(old_commit, new_commit)
                    )
                if not self._ancestry_cache[ancestry_key]:
                    return {"reason_code": "NON_ANCESTOR"}
                old_text = provider.read_file(old_commit, relation["file_path"])
                new_text = provider.read_file(new_commit, relation["file_path"])
                map_git_text = getattr(self.line_mapper, "map_git_text", None)
                mapping = (map_git_text(
                    repo_path, old_commit, new_commit, relation["file_path"],
                    old_text, new_text,
                ) if map_git_text else self.line_mapper.map_git_file(
                    repo_path, old_commit, new_commit, relation["file_path"],
                ))
                old_index, new_index, pair_reason = self._source_index_pair(
                    provider, old_commit, new_commit
                )
                if pair_reason:
                    return {"reason_code": pair_reason}
                return {"old_text": old_text, "new_text": new_text, "mapping": mapping,
                        "old_index": old_index, "new_index": new_index}
            except GitTechnicalFailure as exc:
                raise InheritanceTechnicalFailure(str(exc))
            except (OSError, ValueError) as exc:
                raise InheritanceTechnicalFailure(str(exc))
        # A production inheritance run cannot substitute a live/current text
        # or a database line for an immutable Git snapshot.  Pure parser unit
        # tests call compare_line directly; the durable run remains fail-closed.
        return {"reason_code": "REPOSITORY_PATH_UNAVAILABLE"}

    def _source_index(self, provider, commit):
        key = self._source_index_key(provider, commit)
        if key in self._source_index_cache:
            self._metrics["parser_cache_hit"] += 1
            if self.performance is not None:
                self.performance.record_cache(hit=True)
            self._source_index_cache.move_to_end(key)
            self._source_index_changed(key, self._source_index_cache[key])
            return self._source_index_cache[key]
        self._metrics["parser_cache_miss"] += 1
        if self.performance is not None:
            self.performance.record_cache(miss=True)
        paths = provider.list_source_files(commit)
        self._metrics["source_files_total"] += len(paths)

        def load(path):
            text = provider.read_file(commit, path)
            self._metrics["parser_file_total"] += 1
            return self.parser.analyze(text, path), len(text.encode("utf-8"))

        index = LazySourceAnalysisIndex(
            paths=paths, loader=load, max_cached_bytes=self.max_source_cache_bytes,
            metrics=self._metrics,
            candidate_index_loader=(
                (lambda source_paths=tuple(paths), source_commit=str(commit):
                 provider.build_symbol_candidate_index(source_commit, source_paths))
                if hasattr(provider, "build_symbol_candidate_index") else None
            ),
            on_cache_change=lambda value, cache_key=key: self._source_index_changed(
                cache_key, value
            ),
        )
        self._source_index_cache[key] = index
        self._source_index_cache_sizes[key] = self._source_index_bytes(index)
        self._source_index_cache_bytes += self._source_index_cache_sizes[key]
        self._source_index_changed(key, index)
        return index

    def _source_index_pair(self, provider, old_commit, new_commit):
        """Return a jointly pinned old/new dependency index pair.

        A generic LRU may evict an inactive commit, but it must never evict
        one side of the pair needed by the same inheritance comparison.  If
        the pair (or the already active pairs for this run) cannot fit, mark
        the pair failed closed and remember that result so subsequent source
        files do not rebuild the same candidate indexes.
        """
        old_key = self._source_index_key(provider, old_commit)
        new_key = self._source_index_key(provider, new_commit)
        pair_key = (old_key, new_key)
        if pair_key in self._failed_index_pairs:
            return None, None, DEPENDENCY_INDEX_MEMORY_BUDGET_EXHAUSTED
        self._active_index_pairs.add(pair_key)
        self._active_source_index_keys.update((old_key, new_key))
        self._record_source_cache_metrics()
        old_index = self._source_index(provider, old_commit)
        new_index = self._source_index(provider, new_commit)
        pair_bytes = sum(
            int(self._source_index_cache_sizes.get(key) or 0)
            for key in set((old_key, new_key))
        )
        if (pair_key in self._failed_index_pairs or
                pair_bytes > self.max_source_cache_total_bytes or
                self._source_index_cache_bytes > self.max_source_cache_total_bytes):
            self._mark_index_pair_budget_exhausted(pair_key, drop=True)
            return None, None, DEPENDENCY_INDEX_MEMORY_BUDGET_EXHAUSTED
        return old_index, new_index, ""

    @staticmethod
    def _dependency_context(analysis, line_number):
        context = []
        context.extend((analysis.get("controls") or {}).get(int(line_number), ()) or ())
        context.extend((analysis.get("preprocessor") or {}).get(int(line_number), ()) or ())
        return context

    @staticmethod
    def _ensure_group(connection, run_id, candidate_scan_id, source_scan_id,
                      relation, candidate_line, mapping):
        block_id = relation.get("source_block_id") or relation.get("analysis_block_id")
        if not block_id:
            return None
        source_repo = fetchone(connection, """
            SELECT repository_id FROM coverage_scan_repositories
            WHERE scan_id=? AND repository_name=?
        """, (int(source_scan_id), relation.get("repository_name") or "")) or {}
        repository_id = source_repo.get("repository_id") or 0
        if not repository_id:
            return None
        fingerprint = mapping.fingerprint
        existing = fetchone(connection, """
            SELECT id FROM coverage_inheritance_groups
            WHERE decision_run_id=? AND source_analysis_block_id=?
              AND candidate_file_id=? AND mapping_fingerprint=?
        """, (run_id, int(block_id), int(candidate_line["file_id"]), fingerprint))
        if existing:
            return existing["id"]
        cursor = execute(connection, """
            INSERT INTO coverage_inheritance_groups(
                decision_run_id, candidate_scan_id, source_scan_id,
                source_analysis_block_id, repository_id, candidate_file_id,
                mapping_fingerprint, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (run_id, int(candidate_scan_id), int(source_scan_id), int(block_id),
              int(repository_id), int(candidate_line["file_id"]), fingerprint, utc_sql()))
        group_id = getattr(cursor, "lastrowid", None)
        cursor.close()
        return int(group_id or 0) or None

    def _write_decision(self, connection, run_id, candidate_scan_id, candidate_line,
                        source_relation, reason, algorithm_version, result=None,
                        mapping=None):
        candidate_line_id = int((candidate_line or {}).get("id") or 0)
        values = {
            "decision_run_id": run_id, "candidate_scan_id": int(candidate_scan_id),
            "candidate_line_id": candidate_line_id,
            "source_scan_id": None,
            "source_line_id": (source_relation or {}).get("line_id"),
            "source_relation_id": (source_relation or {}).get("id"),
            "decision": "INHERITED" if reason == "INHERITED" else NO_INHERIT,
            "reason_code": reason or "NO_INHERIT", "algorithm_version": algorithm_version,
            "line_mapping_fingerprint": (mapping.fingerprint if mapping else ""),
            "function_identity_fingerprint": getattr(result, "function_identity_fingerprint", "") if result else "",
            "control_context_fingerprint": getattr(result, "control_context_fingerprint", "") if result else "",
            "preprocessor_context_fingerprint": getattr(result, "preprocessor_context_fingerprint", "") if result else "",
            "dependency_fingerprint": getattr(result, "dependency_fingerprint", "") if result else "",
            "evaluated_at": utc_sql(),
        }
        if source_relation:
            values["source_scan_id"] = source_relation.get("scan_id")
        if not candidate_line_id:
            return dict(values, decision="NO_TARGET_LINE")
        if not self._decision_exists(connection, run_id, candidate_line_id):
            cursor = execute(connection, """
                INSERT INTO coverage_inheritance_decisions(
                    decision_run_id, candidate_scan_id, candidate_line_id,
                    source_scan_id, source_line_id, source_relation_id,
                    decision, reason_code, algorithm_version,
                    line_mapping_fingerprint, function_identity_fingerprint,
                    control_context_fingerprint, preprocessor_context_fingerprint,
                    dependency_fingerprint, evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (values["decision_run_id"], values["candidate_scan_id"],
                  values["candidate_line_id"], values["source_scan_id"],
                  values["source_line_id"], values["source_relation_id"],
                  values["decision"], values["reason_code"], values["algorithm_version"],
                  values["line_mapping_fingerprint"], values["function_identity_fingerprint"],
                  values["control_context_fingerprint"], values["preprocessor_context_fingerprint"],
                  values["dependency_fingerprint"], values["evaluated_at"]))
            cursor.close()
        return values

    @staticmethod
    def _decision_exists(connection, run_id, line_id):
        return bool(fetchone(connection, """
            SELECT id FROM coverage_inheritance_decisions
            WHERE decision_run_id=? AND candidate_line_id=?
        """, (run_id, int(line_id))))

    @staticmethod
    def _result(ok, reason_code, **kwargs):
        result = type("InheritanceResult", (object,), {})()
        result.ok = bool(ok)
        result.reason_code = reason_code
        for key, value in kwargs.items():
            setattr(result, key, value)
        return result

    @staticmethod
    def _pair_hash(left, right):
        return hashlib.sha256(repr((left, right)).encode("utf-8")).hexdigest()
