"""Actual candidate-line macro/constant/direct-callee dependency checks."""

from __future__ import absolute_import

import hashlib
import re

from app.inheritance.normalizer import normalize_cpp


class DependencyResult(object):
    def __init__(self, ok=True, reason_code="", fingerprint=""):
        self.ok = bool(ok)
        self.reason_code = str(reason_code or "")
        self.fingerprint = fingerprint or hashlib.sha256(b"").hexdigest()


class DependencyResolver(object):
    def compare(self, old_analysis, new_analysis, old_line, new_line):
        names = set(token for token in (normalize_cpp(old_line) + normalize_cpp(new_line))
                    if re.match(r"^[A-Za-z_]\w*$", token))
        fingerprints = []
        for name in sorted(names):
            old_definition = (old_analysis.get("macros") or {}).get(name)
            new_definition = (new_analysis.get("macros") or {}).get(name)
            if old_definition or new_definition:
                if not old_definition or not new_definition or old_definition[1] != new_definition[1]:
                    return DependencyResult(False, "MACRO_CHANGED",
                                            self._fingerprint(name, old_definition, new_definition))
                fingerprints.append(("macro", name, old_definition[1]))
            old_constant = (old_analysis.get("constants") or {}).get(name)
            new_constant = (new_analysis.get("constants") or {}).get(name)
            if old_constant or new_constant:
                if not old_constant or not new_constant or old_constant[1] != new_constant[1]:
                    return DependencyResult(False, "CONST_CHANGED",
                                            self._fingerprint(name, old_constant, new_constant))
                fingerprints.append(("const", name, old_constant[1]))

        old_calls = set((old_analysis.get("calls") or {}).get(int(old_analysis.get("line_number", 0)), ()))
        new_calls = set((new_analysis.get("calls") or {}).get(int(new_analysis.get("line_number", 0)), ()))
        for name in sorted(old_calls | new_calls):
            old_function = self._function_body(old_analysis, name)
            new_function = self._function_body(new_analysis, name)
            # Language keywords/operators are not direct callees.
            if not old_function and not new_function:
                continue
            if not old_function or not new_function:
                return DependencyResult(False, "CALLEE_UNRESOLVED",
                                        self._fingerprint(name, old_function, new_function))
            if old_function.uncertain or new_function.uncertain:
                return DependencyResult(False, "CALLEE_UNRESOLVED",
                                        self._fingerprint(name, old_function.body_fingerprint(),
                                                          new_function.body_fingerprint()))
            if old_function.body_fingerprint() != new_function.body_fingerprint():
                return DependencyResult(False, "CALLEE_CHANGED",
                                        self._fingerprint(name, old_function.body_fingerprint(),
                                                          new_function.body_fingerprint()))
            fingerprints.append(("callee", name, old_function.body_fingerprint()))
        return DependencyResult(True, "", self._fingerprint(fingerprints))

    @staticmethod
    def _function_body(analysis, name):
        matches = [item for item in analysis.get("functions", []) if item.identity.name == name]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _fingerprint(*values):
        return hashlib.sha256(repr(values).encode("utf-8")).hexdigest()
