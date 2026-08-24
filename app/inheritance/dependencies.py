"""Actual candidate-line dependency checks.

The first implementation only searched the analysis of the file currently
being compared.  That silently treated a same-repository helper in a header or
another translation unit as an external function.  ``SourceAnalysisIndex`` is
the immutable per-commit universe supplied by the engine; unresolved or
ambiguous facts remain ordinary no-inherit decisions.
"""

from __future__ import absolute_import

import hashlib
import re
import sys
from collections import OrderedDict

from app.inheritance.normalizer import normalize_cpp


_NON_DEPENDENCY_TOKENS = frozenset((
    "alignas", "alignof", "and", "and_eq", "asm", "auto", "bitand",
    "bitor", "bool", "break", "case", "catch", "char", "char8_t",
    "char16_t", "char32_t", "class", "compl", "concept", "const",
    "consteval", "constexpr", "constinit", "const_cast", "continue",
    "co_await", "co_return", "co_yield", "decltype", "default", "delete",
    "do", "double", "dynamic_cast", "else", "enum", "explicit", "export",
    "extern", "false", "float", "for", "friend", "goto", "if", "inline",
    "int", "long", "mutable", "namespace", "new", "noexcept", "not",
    "not_eq", "nullptr", "operator", "or", "or_eq", "private", "protected",
    "public", "register", "reinterpret_cast", "requires", "return", "short",
    "signed", "sizeof", "static", "static_assert", "static_cast", "struct",
    "switch", "template", "this", "thread_local", "throw", "true", "try",
    "typedef", "typeid", "typename", "union", "unsigned", "using", "virtual",
    "void", "volatile", "wchar_t", "while", "xor", "xor_eq",
))


class DependencyResult(object):
    def __init__(self, ok=True, reason_code="", fingerprint=""):
        self.ok = bool(ok)
        self.reason_code = str(reason_code or "")
        self.fingerprint = fingerprint or hashlib.sha256(b"").hexdigest()


class SourceAnalysisIndex(object):
    """Read-only indexes over one repository commit's parsed source files."""

    def __init__(self, analyses=None):
        self.analyses = dict(analyses or {})
        self._functions = {}
        self._macros = {}
        self._constants = {}
        for path, analysis in sorted(self.analyses.items()):
            for function in analysis.get("functions", []) or []:
                self._functions.setdefault(function.identity.name, []).append(function)
            for name, definition in (analysis.get("macros") or {}).items():
                self._macros.setdefault(str(name), []).append((path, definition))
            for name, definition in (analysis.get("constants") or {}).items():
                self._constants.setdefault(str(name), []).append((path, definition))

    def functions(self, name, local_analysis=None):
        values = list(self._functions.get(str(name), ()))
        if values or local_analysis is None:
            return values
        return [item for item in local_analysis.get("functions", [])
                if item.identity.name == str(name)]

    def definitions(self, kind, name, local_analysis=None):
        source = self._macros if kind == "macros" else self._constants
        values = list(source.get(str(name), ()))
        if values or local_analysis is None:
            return values
        definitions = (local_analysis.get(kind) or {}).get(str(name))
        return [(local_analysis.get("path") or "", definitions)] if definitions else []


class LazySourceAnalysisIndex(SourceAnalysisIndex):
    """Lazy dependency closure with a byte-aware bounded LRU.

    The repository file list is cheap metadata. Source text and parser objects
    are loaded only when a dependency symbol is queried. If the configured
    byte budget cannot admit another file, lookup returns the same
    conservative unresolved result as an ambiguous/missing dependency.
    """

    def __init__(self, paths=None, loader=None, analyses=None,
                 max_cached_bytes=32 * 1024 * 1024, metrics=None,
                 on_cache_change=None, candidate_index=None,
                 candidate_index_loader=None, max_resolution_cache_entries=4096):
        self._lazy_paths = [str(path) for path in (paths or [])]
        self._lazy_loader = loader
        self._max_cached_bytes = max(1, int(max_cached_bytes))
        self._cached_bytes = 0
        self._cache = OrderedDict()
        self._pinned = set((analyses or {}).keys())
        self._metrics = metrics if isinstance(metrics, dict) else {}
        self._on_cache_change = on_cache_change
        self._candidate_index = self._normalize_candidate_index(candidate_index)
        self._candidate_index_loader = candidate_index_loader
        self._candidate_index_ready = candidate_index is not None
        self._candidate_index_entries = (
            self._count_candidate_index_entries(self._candidate_index)
            if self._candidate_index_ready else 0
        )
        self._candidate_index_bytes = (
            self._estimate_structure_size(self._candidate_index)
            if self._candidate_index_ready else 0
        )
        self.candidate_index_unavailable = False
        self._max_resolution_cache_entries = max(
            1, int(max_resolution_cache_entries)
        )
        self._resolution_cache = OrderedDict()
        self._resolution_cache_sizes = {}
        self._resolution_cache_bytes = 0
        self._resolution_cache_evictions = 0
        self.dependency_index_memory_budget_exhausted = False
        self.budget_exhausted = False
        super(LazySourceAnalysisIndex, self).__init__(analyses=analyses or {})
        for path in sorted(self.analyses):
            self._cache[path] = self._estimate_size(self.analyses[path])
            self._cached_bytes += self._cache[path]
        if self._cached_bytes > self._max_cached_bytes:
            # Pinned analysis entries cannot be evicted. Treat an over-budget
            # initial universe exactly like a failed lazy admission instead of
            # exposing a partial/empty dependency universe as external code.
            self.budget_exhausted = True
            self._metrics["source_budget_exhausted"] = (
                int(self._metrics.get("source_budget_exhausted") or 0) + 1
            )

    @staticmethod
    def _normalize_candidate_index(value):
        result = {"functions": {}, "macros": {}, "constants": {}}
        if not isinstance(value, dict):
            return result
        for kind in result:
            source = value.get(kind) or {}
            if not isinstance(source, dict):
                continue
            for name, paths in source.items():
                if isinstance(paths, str):
                    paths = (paths,)
                result[kind][str(name)] = tuple(sorted(set(
                    str(path) for path in (paths or ()) if str(path)
                )))
        return result

    @staticmethod
    def _count_candidate_index_entries(value):
        return sum(
            len(paths)
            for source in (value or {}).values()
            for paths in (source or {}).values()
        )

    @staticmethod
    def _estimate_structure_size(value, seen=None):
        """Estimate container/string memory, including Python object overhead."""
        seen = seen if seen is not None else set()
        identity = id(value)
        if identity in seen:
            return 0
        seen.add(identity)
        try:
            size = sys.getsizeof(value)
        except TypeError:
            size = 0
        if isinstance(value, dict):
            size += sum(
                LazySourceAnalysisIndex._estimate_structure_size(item, seen)
                for pair in value.items() for item in pair
            )
        elif isinstance(value, (list, tuple, set, frozenset)):
            size += sum(
                LazySourceAnalysisIndex._estimate_structure_size(item, seen)
                for item in value
            )
        elif isinstance(value, (str, bytes)):
            # ``getsizeof`` is authoritative for these leaf objects.  Keep a
            # serialized lower bound for alternate Python implementations.
            try:
                size = max(size, len(value if isinstance(value, bytes)
                                   else value.encode("utf-8")))
            except (TypeError, UnicodeEncodeError):
                pass
        return max(1, int(size))

    def _record_index_cache_metrics(self):
        self._metrics["candidate_index_entries"] = int(
            self._candidate_index_entries
        )
        self._metrics["candidate_index_bytes"] = int(
            self._candidate_index_bytes
        )
        self._metrics["resolution_cache_bytes"] = int(
            self._resolution_cache_bytes
        )
        self._metrics["total_index_bytes"] = int(
            self._cached_bytes + self._candidate_index_bytes +
            self._resolution_cache_bytes
        )

    def _ensure_candidate_index(self):
        if self._candidate_index_ready:
            return
        self._candidate_index_ready = True
        if self._candidate_index_loader is None:
            return
        try:
            loaded = self._candidate_index_loader()
            if not isinstance(loaded, dict):
                raise ValueError("candidate index must be a mapping")
            self._candidate_index = self._normalize_candidate_index(loaded)
            self._candidate_index_entries = self._count_candidate_index_entries(
                self._candidate_index
            )
            self._candidate_index_bytes = self._estimate_structure_size(
                self._candidate_index
            )
            self._metrics["source_candidate_index_builds"] = (
                int(self._metrics.get("source_candidate_index_builds") or 0) + 1
            )
            self._record_index_cache_metrics()
            self._notify_cache_change()
        except Exception:
            # A lightweight candidate index is an optimization, but an
            # unavailable index must never turn an unknown dependency into an
            # external/library dependency.  The resolver will fail closed.
            self.candidate_index_unavailable = True
            self._metrics["source_candidate_index_failures"] = (
                int(self._metrics.get("source_candidate_index_failures") or 0) + 1
            )

    def _candidate_paths(self, kind, name):
        key = (str(kind), str(name))
        cached = self._resolution_cache.get(key)
        if cached is not None:
            self._resolution_cache.move_to_end(key)
            self._metrics["dependency_resolution_cache_hits"] = (
                int(self._metrics.get("dependency_resolution_cache_hits") or 0) + 1
            )
            return cached
        self._metrics["dependency_resolution_cache_misses"] = (
            int(self._metrics.get("dependency_resolution_cache_misses") or 0) + 1
        )
        self._ensure_candidate_index()
        paths = tuple(self._candidate_index.get(str(kind), {}).get(str(name), ()))
        self._resolution_cache[key] = paths
        entry_size = self._estimate_structure_size((key, paths))
        self._resolution_cache_sizes[key] = entry_size
        self._resolution_cache_bytes += entry_size
        while len(self._resolution_cache) > self._max_resolution_cache_entries:
            evicted_key, _ = self._resolution_cache.popitem(last=False)
            self._resolution_cache_bytes -= int(
                self._resolution_cache_sizes.pop(evicted_key, 0)
            )
            self._resolution_cache_evictions += 1
        self._metrics["dependency_candidate_paths"] = (
            int(self._metrics.get("dependency_candidate_paths") or 0) + len(paths)
        )
        self._record_index_cache_metrics()
        self._notify_cache_change()
        return paths

    @staticmethod
    def _estimate_size(analysis):
        try:
            return len(repr(analysis).encode("utf-8"))
        except (TypeError, ValueError):
            return 1

    def _rebuild(self):
        self._functions = {}
        self._macros = {}
        self._constants = {}
        for path, analysis in sorted(self.analyses.items()):
            for function in analysis.get("functions", []) or []:
                self._functions.setdefault(function.identity.name, []).append(function)
            for name, definition in (analysis.get("macros") or {}).items():
                self._macros.setdefault(str(name), []).append((path, definition))
            for name, definition in (analysis.get("constants") or {}).items():
                self._constants.setdefault(str(name), []).append((path, definition))

    def _notify_cache_change(self):
        if self._on_cache_change is not None:
            self._on_cache_change(self)

    def _load_path(self, path):
        path = str(path)
        if path in self.analyses:
            self._cache.move_to_end(path)
            self._notify_cache_change()
            return True
        if self._lazy_loader is None:
            return False
        try:
            loaded = self._lazy_loader(path)
        except Exception:
            return False
        if isinstance(loaded, tuple) and len(loaded) == 2:
            analysis, size = loaded
        else:
            analysis, size = loaded, None
        if not isinstance(analysis, dict):
            return False
        size = max(1, int(size or self._estimate_size(analysis)))
        if size > self._max_cached_bytes:
            self.budget_exhausted = True
            self._metrics["source_budget_exhausted"] = (
                int(self._metrics.get("source_budget_exhausted") or 0) + 1
            )
            self._notify_cache_change()
            return False
        while self._cached_bytes + size > self._max_cached_bytes:
            evicted = next(
                (item for item in self._cache if item not in self._pinned), None
            )
            if evicted is None:
                self.budget_exhausted = True
                self._metrics["source_budget_exhausted"] = (
                    int(self._metrics.get("source_budget_exhausted") or 0) + 1
                )
                self._notify_cache_change()
                return False
            evicted_size = self._cache.pop(evicted)
            self.analyses.pop(evicted, None)
            self._cached_bytes -= int(evicted_size)
        self.analyses[path] = analysis
        self._cache[path] = size
        self._cached_bytes += size
        self._rebuild()
        self._metrics["source_files_loaded"] = (
            int(self._metrics.get("source_files_loaded") or 0) + 1
        )
        self._metrics["source_cache_bytes"] = self._cached_bytes
        self._record_index_cache_metrics()
        self._notify_cache_change()
        return True

    def _ensure_symbol(self, kind, name):
        source = self._functions if kind == "functions" else (
            self._macros if kind == "macros" else self._constants
        )
        symbol = str(name)
        paths = (self._lazy_paths if self._candidate_index_loader is None and
                 not self._candidate_index_ready else
                 self._candidate_paths(kind, symbol))
        if self.candidate_index_unavailable:
            return
        for path in paths:
            values = source.get(symbol, ())
            if len(values) > 1:
                break
            if path not in self.analyses and not self._load_path(path):
                if self.budget_exhausted:
                    break
            source = self._functions if kind == "functions" else (
                self._macros if kind == "macros" else self._constants
            )
            if len(source.get(symbol, ())) > 1:
                break

    def functions(self, name, local_analysis=None):
        self._ensure_symbol("functions", name)
        if self.budget_exhausted:
            return []
        values = list(self._functions.get(str(name), ()))
        if values or local_analysis is None:
            return values
        return [item for item in local_analysis.get("functions", [])
                if item.identity.name == str(name)]

    def definitions(self, kind, name, local_analysis=None):
        self._ensure_symbol(kind, name)
        if self.budget_exhausted:
            return []
        source = self._macros if kind == "macros" else self._constants
        values = list(source.get(str(name), ()))
        if values or local_analysis is None:
            return values
        definitions = (local_analysis.get(kind) or {}).get(str(name))
        return [(local_analysis.get("path") or "", definitions)] if definitions else []

    def cache_stats(self):
        self._record_index_cache_metrics()
        return {
            "cached_files": len(self._cache),
            "cache_bytes": int(self._cached_bytes),
            "parsed_cache_bytes": int(self._cached_bytes),
            "max_cached_bytes": int(self._max_cached_bytes),
            "candidate_index_entries": int(self._candidate_index_entries),
            "candidate_index_bytes": int(self._candidate_index_bytes),
            "budget_exhausted": bool(self.budget_exhausted),
            "dependency_index_memory_budget_exhausted": bool(
                self.dependency_index_memory_budget_exhausted
            ),
            "candidate_index_ready": bool(self._candidate_index_ready),
            "candidate_index_unavailable": bool(self.candidate_index_unavailable),
            "resolution_cache_entries": len(self._resolution_cache),
            "resolution_cache_bytes": int(self._resolution_cache_bytes),
            "total_index_bytes": int(
                self._cached_bytes + self._candidate_index_bytes +
                self._resolution_cache_bytes
            ),
            "resolution_cache_evictions": int(self._resolution_cache_evictions),
        }


class DependencyResolver(object):
    def compare(self, old_analysis, new_analysis, old_line, new_line,
                old_context=None, new_context=None, old_index=None,
                new_index=None):
        unresolved_reason = self._unresolved_reason(old_index, new_index)
        if unresolved_reason:
            return DependencyResult(
                False, unresolved_reason,
                self._fingerprint("budget", old_line, new_line),
            )
        names = set()
        for value in (old_line, new_line) + tuple(old_context or ()) + tuple(new_context or ()):
            if isinstance(value, (tuple, list)):
                names.update(str(item) for item in value
                             if re.match(r"^[A-Za-z_]\w*$", str(item)))
            else:
                names.update(token for token in normalize_cpp(value)
                             if re.match(r"^[A-Za-z_]\w*$", token))
        names.difference_update(_NON_DEPENDENCY_TOKENS)

        fingerprints = []
        for name in sorted(names):
            old_definitions = self._definitions(old_index, "macros", name, old_analysis)
            new_definitions = self._definitions(new_index, "macros", name, new_analysis)
            unresolved_reason = self._unresolved_reason(old_index, new_index)
            if unresolved_reason:
                return DependencyResult(
                    False, unresolved_reason,
                    self._fingerprint(name, old_definitions, new_definitions),
                )
            if old_definitions or new_definitions:
                if (len(old_definitions) != 1 or len(new_definitions) != 1 or
                        old_definitions[0][1] != new_definitions[0][1]):
                    return DependencyResult(False, "MACRO_CHANGED",
                                            self._fingerprint(name, old_definitions,
                                                              new_definitions))
                fingerprints.append(("macro", name, old_definitions[0][1]))

            old_constants = self._definitions(old_index, "constants", name, old_analysis)
            new_constants = self._definitions(new_index, "constants", name, new_analysis)
            unresolved_reason = self._unresolved_reason(old_index, new_index)
            if unresolved_reason:
                return DependencyResult(
                    False, unresolved_reason,
                    self._fingerprint(name, old_constants, new_constants),
                )
            if old_constants or new_constants:
                if (len(old_constants) != 1 or len(new_constants) != 1 or
                        old_constants[0][1] != new_constants[0][1]):
                    return DependencyResult(False, "CONST_CHANGED",
                                            self._fingerprint(name, old_constants,
                                                              new_constants))
                fingerprints.append(("const", name, old_constants[0][1]))

        old_line_number = int(old_analysis.get("line_number", 0) or 0)
        new_line_number = int(new_analysis.get("line_number", 0) or 0)
        old_calls = set((old_analysis.get("calls") or {}).get(old_line_number, ()))
        new_calls = set((new_analysis.get("calls") or {}).get(new_line_number, ()))
        for name in sorted(old_calls | new_calls):
            old_functions = self._functions(old_index, name, old_analysis)
            new_functions = self._functions(new_index, name, new_analysis)
            unresolved_reason = self._unresolved_reason(old_index, new_index)
            if unresolved_reason:
                return DependencyResult(
                    False, unresolved_reason,
                    self._fingerprint(name, old_functions, new_functions),
                )
            # A name absent from both repository universes is a library/API
            # call, not a same-repository dependency.  A present-but-ambiguous
            # name is different: without overload/include resolution it must
            # fail closed.
            if not old_functions and not new_functions:
                continue
            if len(old_functions) != 1 or len(new_functions) != 1:
                return DependencyResult(False, "CALLEE_UNRESOLVED",
                                        self._fingerprint(name, old_functions,
                                                          new_functions))
            old_function = old_functions[0]
            new_function = new_functions[0]
            if old_function.uncertain or new_function.uncertain:
                return DependencyResult(False, "CALLEE_UNRESOLVED",
                                        self._fingerprint(name,
                                                          old_function.body_fingerprint(),
                                                          new_function.body_fingerprint()))
            if old_function.identity.canonical() != new_function.identity.canonical():
                return DependencyResult(False, "CALLEE_CHANGED",
                                        self._fingerprint(name,
                                                          old_function.identity.canonical(),
                                                          new_function.identity.canonical()))
            if old_function.body_fingerprint() != new_function.body_fingerprint():
                return DependencyResult(False, "CALLEE_CHANGED",
                                        self._fingerprint(name,
                                                          old_function.body_fingerprint(),
                                                          new_function.body_fingerprint()))
            fingerprints.append(("callee", name, old_function.body_fingerprint()))
        return DependencyResult(True, "", self._fingerprint(fingerprints))

    @staticmethod
    def _budget_exhausted(index):
        return bool(index is not None and (
            getattr(index, "budget_exhausted", False) or
            getattr(index, "candidate_index_unavailable", False)
        ))

    @classmethod
    def _unresolved_reason(cls, *indexes):
        if any(getattr(index, "dependency_index_memory_budget_exhausted", False)
               for index in indexes if index is not None):
            return "DEPENDENCY_INDEX_MEMORY_BUDGET_EXHAUSTED"
        if any(getattr(index, "candidate_index_unavailable", False)
               for index in indexes if index is not None):
            return "DEPENDENCY_CANDIDATE_INDEX_UNAVAILABLE"
        if any(cls._budget_exhausted(index) for index in indexes):
            return "DEPENDENCY_BUDGET_EXHAUSTED"
        return ""

    @staticmethod
    def _definitions(index, kind, name, analysis):
        if index is not None:
            return index.definitions(kind, name, local_analysis=analysis)
        definition = (analysis.get(kind) or {}).get(name)
        return [(analysis.get("path") or "", definition)] if definition else []

    @staticmethod
    def _functions(index, name, analysis):
        if index is not None:
            return index.functions(name, local_analysis=analysis)
        return [item for item in analysis.get("functions", [])
                if item.identity.name == str(name)]

    @staticmethod
    def _fingerprint(*values):
        return hashlib.sha256(repr(values).encode("utf-8")).hexdigest()
