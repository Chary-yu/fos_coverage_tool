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
from collections import OrderedDict

from app.inheritance.normalizer import normalize_cpp


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
                 max_cached_bytes=32 * 1024 * 1024, metrics=None):
        self._lazy_paths = [str(path) for path in (paths or [])]
        self._lazy_loader = loader
        self._max_cached_bytes = max(1, int(max_cached_bytes))
        self._cached_bytes = 0
        self._cache = OrderedDict()
        self._pinned = set((analyses or {}).keys())
        self._metrics = metrics if isinstance(metrics, dict) else {}
        self.budget_exhausted = False
        super(LazySourceAnalysisIndex, self).__init__(analyses=analyses or {})
        for path in sorted(self.analyses):
            self._cache[path] = self._estimate_size(self.analyses[path])
            self._cached_bytes += self._cache[path]

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

    def _load_path(self, path):
        path = str(path)
        if path in self.analyses:
            self._cache.move_to_end(path)
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
        return True

    def _ensure_symbol(self, kind, name):
        source = self._functions if kind == "functions" else (
            self._macros if kind == "macros" else self._constants
        )
        if str(name) in source:
            # A second definition may still be hidden. Continue loading until
            # all paths are visited or the byte budget makes the result
            # explicitly unresolved.
            pass
        for path in self._lazy_paths:
            if path not in self.analyses:
                if not self._load_path(path) and self.budget_exhausted:
                    break

    def functions(self, name, local_analysis=None):
        self._ensure_symbol("functions", name)
        values = list(self._functions.get(str(name), ()))
        if values or local_analysis is None:
            return values
        return [item for item in local_analysis.get("functions", [])
                if item.identity.name == str(name)]

    def definitions(self, kind, name, local_analysis=None):
        self._ensure_symbol(kind, name)
        source = self._macros if kind == "macros" else self._constants
        values = list(source.get(str(name), ()))
        if values or local_analysis is None:
            return values
        definitions = (local_analysis.get(kind) or {}).get(str(name))
        return [(local_analysis.get("path") or "", definitions)] if definitions else []

    def cache_stats(self):
        return {
            "cached_files": len(self._cache),
            "cache_bytes": int(self._cached_bytes),
            "max_cached_bytes": int(self._max_cached_bytes),
            "budget_exhausted": bool(self.budget_exhausted),
        }


class DependencyResolver(object):
    def compare(self, old_analysis, new_analysis, old_line, new_line,
                old_context=None, new_context=None, old_index=None,
                new_index=None):
        names = set()
        for value in (old_line, new_line) + tuple(old_context or ()) + tuple(new_context or ()):
            if isinstance(value, (tuple, list)):
                names.update(str(item) for item in value
                             if re.match(r"^[A-Za-z_]\w*$", str(item)))
            else:
                names.update(token for token in normalize_cpp(value)
                             if re.match(r"^[A-Za-z_]\w*$", token))

        fingerprints = []
        for name in sorted(names):
            old_definitions = self._definitions(old_index, "macros", name, old_analysis)
            new_definitions = self._definitions(new_index, "macros", name, new_analysis)
            if old_definitions or new_definitions:
                if (len(old_definitions) != 1 or len(new_definitions) != 1 or
                        old_definitions[0][1] != new_definitions[0][1]):
                    return DependencyResult(False, "MACRO_CHANGED",
                                            self._fingerprint(name, old_definitions,
                                                              new_definitions))
                fingerprints.append(("macro", name, old_definitions[0][1]))

            old_constants = self._definitions(old_index, "constants", name, old_analysis)
            new_constants = self._definitions(new_index, "constants", name, new_analysis)
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
