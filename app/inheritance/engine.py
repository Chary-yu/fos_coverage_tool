"""Deterministic Analysis Inheritance Engine.

The engine is deliberately conservative: a missing/ambiguous parser or Git
fact produces an ordinary no-inherit decision; infrastructure failures are
raised so the import coordinator can keep the Candidate unpublished.
"""

from __future__ import absolute_import

import hashlib
import json
import os

from app.db.repositories.analysis_domain_repository import (
    AnalysisDomainRepository, CARRIED_COVERED, INHERITED_PENDING,
)
from app.db.repositories.base import adapt_sql, execute, fetchall, fetchone
from app.inheritance.cpp_parser import CppSourceAnalyzer
from app.inheritance.dependencies import DependencyResolver, SourceAnalysisIndex
from app.inheritance.git_snapshot import GitSnapshotProvider, GitTechnicalFailure
from app.inheritance.line_map import GitLineMapEngine
from app.inheritance.normalizer import normalize_cpp
from app.inheritance.predecessor import PredecessorResolver
from app.time_utils import utc_sql


ALGORITHM_VERSION = "inheritance-v1"
NO_INHERIT = "NO_INHERIT"


class InheritanceTechnicalFailure(RuntimeError):
    error_class = "INHERITANCE_TECHNICAL_FAILURE"


class InheritanceEngine(object):
    def __init__(self, predecessor=None, line_mapper=None, parser=None,
                 dependency_resolver=None, domain_repository=None):
        self.predecessor = predecessor or PredecessorResolver()
        self.line_mapper = line_mapper or GitLineMapEngine()
        self.parser = parser or CppSourceAnalyzer()
        self.dependencies = dependency_resolver or DependencyResolver()
        self.domain = domain_repository or AnalysisDomainRepository()
        self._source_index_cache = {}
        self._ancestry_cache = {}
        self._metrics = {}

    def compare_line(self, old_line, new_line, old_analysis=None, new_analysis=None,
                     old_line_number=None, new_line_number=None,
                     old_index=None, new_index=None):
        old_analysis = old_analysis or {}
        new_analysis = new_analysis or {}
        old_line_number = int(old_line_number or old_analysis.get("line_number") or 0)
        new_line_number = int(new_line_number or new_analysis.get("line_number") or 0)
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
            decision_run_id=None, algorithm_version=ALGORITHM_VERSION):
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
            "macro_unresolved_by_reason": {},
            "const_unresolved_by_reason": {},
        }
        candidate = fetchone(connection, "SELECT * FROM coverage_scans WHERE id=?",
                             (int(candidate_scan_id),))
        if not candidate:
            raise KeyError("candidate scan not found")
        predecessor = self.predecessor.resolve(connection, candidate_scan_id)
        predecessor_id = predecessor.get("predecessor_scan_id")
        run_id = decision_run_id or hashlib.sha256(json.dumps({
            "candidate_scan_id": int(candidate_scan_id),
            "predecessor_scan_id": predecessor_id,
            "algorithm_version": algorithm_version,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        if not predecessor_id:
            decisions = self._ordinary_pending_decisions(
                connection, candidate_scan_id, run_id, algorithm_version, "NO_PREDECESSOR"
            )
            return {"status": "PASSED", "decision_run_id": run_id,
                    "decisions": decisions, "inherited": 0,
                    "pending": len(decisions), "read_set": [],
                    "metrics": dict(self._metrics)}
        repository_paths = repository_paths or {}
        source_relations = fetchall(connection, """
            SELECT q.*, l.line_number AS source_line_number, l.line_text AS source_line_text,
                   l.coverage_state AS source_coverage_state, f.file_path,
                   f.repository_name, f.file_path_hash, b.block_identity_verified,
                   b.id AS source_block_id, r.content_revision AS source_content_revision
            FROM coverage_analysis_line_links q
            JOIN coverage_lines l ON l.id=q.line_id
            JOIN coverage_files f ON f.id=l.file_id
            LEFT JOIN coverage_analysis_blocks b ON b.id=q.analysis_block_id
            LEFT JOIN coverage_analysis_records r ON r.id=q.analysis_record_id
            WHERE q.scan_id=? AND q.is_active=1
            ORDER BY f.repository_name, f.file_path, l.line_number
        """, (int(predecessor_id),))
        read_set = self._read_set_for_relations(source_relations)
        candidate_lines = fetchall(connection, """
            SELECT l.*, f.file_path, f.repository_name, f.file_path_hash
            FROM coverage_lines l JOIN coverage_files f ON f.id=l.file_id
            WHERE f.scan_id=?
            ORDER BY f.repository_name, f.file_path, l.line_number
        """, (int(candidate_scan_id),))
        candidate_by_path = {
            (str(row.get("repository_name") or ""), str(row.get("file_path") or "")): row
            for row in candidate_lines
        }
        candidate_by_file_line = {
            (int(row.get("file_id") or 0), int(row.get("line_number") or 0)): row
            for row in candidate_lines
        }
        decisions = []
        decided_line_ids = set()
        for relation in source_relations:
            key = (str(relation.get("repository_name") or ""),
                   str(relation.get("file_path") or ""))
            target_file = candidate_by_path.get(key)
            if not target_file:
                continue
            repo_path = repository_paths.get(key[0])
            candidate_snapshot = self._repository_snapshot(
                connection, candidate_scan_id, key[0]
            )
            predecessor_snapshot = self._repository_snapshot(
                connection, predecessor_id, key[0]
            )
            source_snapshot = self._snapshot_for_relation(
                relation, repo_path, predecessor_snapshot, candidate_snapshot
            )
            if source_snapshot.get("reason_code"):
                decisions.append(self._write_decision(
                    connection, run_id, candidate_scan_id, None, relation,
                    source_snapshot["reason_code"], algorithm_version,
                ))
                continue
            mapping = source_snapshot.get("mapping") or self.line_mapper.map_text(
                source_snapshot["old_text"], source_snapshot["new_text"]
            )
            new_line_number = mapping.get(int(relation["source_line_number"]))
            if new_line_number is None:
                reason = "LINE_DELETED" if int(relation["source_line_number"]) in mapping.deleted else "LINE_AMBIGUOUS"
                decisions.append(self._write_decision(
                    connection, run_id, candidate_scan_id, None, relation,
                    reason, algorithm_version, mapping=mapping,
                ))
                continue
            target_line = candidate_by_file_line.get(
                (int(target_file.get("id") or 0), int(new_line_number))
            )
            if not target_line:
                continue
            existing_decision = fetchone(connection, """
                SELECT * FROM coverage_inheritance_decisions
                WHERE decision_run_id=? AND candidate_line_id=?
            """, (run_id, int(target_line["id"])))
            old_analysis = self.parser.analyze(
                source_snapshot["old_text"], relation.get("file_path") or ""
            )
            new_analysis = self.parser.analyze(
                source_snapshot["new_text"], relation.get("file_path") or ""
            )
            old_lines = source_snapshot["old_text"].splitlines()
            new_lines = source_snapshot["new_text"].splitlines()
            old_line_text = (old_lines[int(relation["source_line_number"]) - 1]
                             if 0 < int(relation["source_line_number"]) <= len(old_lines)
                             else relation.get("source_line_text") or "")
            new_line_text = (new_lines[int(new_line_number) - 1]
                             if 0 < int(new_line_number) <= len(new_lines)
                             else target_line.get("line_text") or "")
            result = self.compare_line(
                old_line_text, new_line_text,
                old_analysis, new_analysis,
                relation.get("source_line_number"), new_line_number,
                old_index=source_snapshot.get("old_index"),
                new_index=source_snapshot.get("new_index"),
            )
            self._metrics["parser_candidate_total"] += 1
            if result.reason_code in (
                    "FUNCTION_ID_UNRESOLVED", "PARSER_UNRELIABLE",
                    "CALLEE_UNRESOLVED", "MACRO_CHANGED", "CONST_CHANGED"):
                self._metrics["parser_unresolved_total"] += 1
                by_reason = self._metrics["parser_unresolved_by_reason"]
                by_reason[result.reason_code] = int(
                    by_reason.get(result.reason_code) or 0
                ) + 1
            if result.reason_code == "CALLEE_UNRESOLVED":
                self._metrics["callee_unresolved_total"] += 1
                by_reason = self._metrics["callee_unresolved_by_reason"]
                by_reason[result.reason_code] = int(
                    by_reason.get(result.reason_code) or 0
                ) + 1
            if result.reason_code == "MACRO_CHANGED":
                self._metrics["macro_unresolved_total"] += 1
                by_reason = self._metrics["macro_unresolved_by_reason"]
                by_reason[result.reason_code] = int(
                    by_reason.get(result.reason_code) or 0
                ) + 1
            if result.reason_code == "CONST_CHANGED":
                self._metrics["const_unresolved_total"] += 1
                by_reason = self._metrics["const_unresolved_by_reason"]
                by_reason[result.reason_code] = int(
                    by_reason.get(result.reason_code) or 0
                ) + 1
            result.mapping_fingerprint = mapping.fingerprint
            if result.ok and (not relation.get("analysis_block_id") or
                              not int(relation.get("block_identity_verified") or 0)):
                result = self._result(False, "BLOCK_AMBIGUOUS",
                                      line_mapping_fingerprint=mapping.fingerprint)
            decision = self._write_decision(
                connection, run_id, candidate_scan_id, target_line, relation,
                result.reason_code, algorithm_version, result=result,
                mapping=mapping,
            )
            decisions.append(decision)
            decided_line_ids.add(int(target_line["id"]))
            if result.ok:
                if (existing_decision and
                        str(existing_decision.get("decision") or "") == "INHERITED" and
                        self._active_link_for_line(
                            connection, candidate_scan_id, target_line["id"]
                        )):
                    continue
                record = self.domain.get_record(connection, relation["analysis_record_id"])
                state = (CARRIED_COVERED if str(target_line.get("coverage_state") or "").lower()
                         in ("covered", "1") else INHERITED_PENDING)
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
        # A predecessor relation is not a license to silently omit a new or
        # renamed candidate line.  Every uncovered candidate receives an
        # ordinary, explainable no-inherit decision when no source relation
        # reached it; covered lines are outside the review-candidate set.
        for candidate_line in candidate_lines:
            line_id = int(candidate_line.get("id") or 0)
            if line_id in decided_line_ids or not self._is_review_candidate(candidate_line):
                continue
            decisions.append(self._write_decision(
                connection, run_id, candidate_scan_id, candidate_line, None,
                "NO_SOURCE_RELATION", algorithm_version,
            ))
        return {"status": "PASSED", "decision_run_id": run_id,
                "decisions": decisions,
                "inherited": len([item for item in decisions if item.get("decision") == "INHERITED"]),
                "pending": len([item for item in decisions if item.get("decision") != "INHERITED"]),
                "read_set": read_set,
                "metrics": dict(self._metrics)}

    @staticmethod
    def _read_set_for_relations(relations):
        """Return the immutable predecessor facts consulted by inheritance.

        The relation revision protects the line-level mapping/review fact and
        the content revision protects the AnalysisRecord payload.  Keep the
        set deterministic and de-duplicated because it is persisted in the
        durable checkpoint and revalidated during the short publish CAS.
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
            SELECT * FROM coverage_scan_repositories
            WHERE scan_id=? AND repository_name=?
        """, (int(scan_id), str(repository_name or ""))) or {}

    def _snapshot_for_relation(self, relation, repo_path, old_snapshot, new_snapshot):
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
                provider = GitSnapshotProvider(repo_path)
                provider.ensure_commit(old_commit)
                provider.ensure_commit(new_commit)
                ancestry_key = (provider.repo_path, str(old_commit), str(new_commit))
                if ancestry_key not in self._ancestry_cache:
                    self._ancestry_cache[ancestry_key] = provider.is_ancestor(
                        old_commit, new_commit
                    )
                if not self._ancestry_cache[ancestry_key]:
                    return {"reason_code": "NON_ANCESTOR"}
                old_text = provider.read_file(old_commit, relation["file_path"])
                new_text = provider.read_file(new_commit, relation["file_path"])
                mapping = self.line_mapper.map_git_file(
                    repo_path, old_commit, new_commit,
                    relation["file_path"],
                )
                old_index = self._source_index(provider, old_commit)
                new_index = self._source_index(provider, new_commit)
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
        key = (provider.repo_path, str(commit), self.parser.__class__.__name__)
        if key in self._source_index_cache:
            self._metrics["parser_cache_hit"] += 1
            return self._source_index_cache[key]
        self._metrics["parser_cache_miss"] += 1
        analyses = {}
        for path in provider.list_source_files(commit):
            text = provider.read_file(commit, path)
            analyses[path] = self.parser.analyze(text, path)
        index = SourceAnalysisIndex(analyses)
        self._source_index_cache[key] = index
        return index

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
