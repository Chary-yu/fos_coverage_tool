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
