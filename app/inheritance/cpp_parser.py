"""Dependency-free C/C++ source analyzer used by the conservative engine.

It is intentionally an adapter: a production deployment may replace this
implementation with a verified clang backend, but the domain consumes the
same deterministic FunctionIdentity/context contract.  Uncertain constructs
return ``None`` and remain ordinary pending rather than being guessed.
"""

from __future__ import absolute_import

import hashlib
import re

from app.inheritance.normalizer import CppLexer, normalize_cpp


SUPPORTED_EXTENSIONS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}


class FunctionIdentity(object):
    def __init__(self, path, scope, name, parameters, qualifiers="", trailing_return=""):
        self.path = str(path or "")
        self.scope = tuple(scope or ())
        self.name = str(name or "")
        self.parameters = tuple(parameters or ())
        self.qualifiers = tuple(qualifiers or ())
        self.trailing_return = tuple(trailing_return or ())

    def canonical(self):
        return (self.path, self.scope, self.name, self.parameters,
                self.qualifiers, self.trailing_return)

    def fingerprint(self):
        return hashlib.sha256(repr(self.canonical()).encode("utf-8")).hexdigest()

    def __eq__(self, other):
        return isinstance(other, FunctionIdentity) and self.canonical() == other.canonical()

    def __hash__(self):
        return hash(self.canonical())

    def __repr__(self):
        return "FunctionIdentity({})".format(self.canonical())


class FunctionRange(object):
    def __init__(self, identity, start_line, end_line, body_tokens=None,
                 uncertain=False):
        self.identity = identity
        self.start_line = int(start_line)
        self.end_line = int(end_line)
        self.body_tokens = tuple(body_tokens or ())
        self.uncertain = bool(uncertain)

    def body_fingerprint(self):
        return hashlib.sha256(repr(self.body_tokens).encode("utf-8")).hexdigest()


class CppSourceAnalyzer(object):
    CONTROL_WORDS = ("if", "else", "for", "while", "switch", "case", "catch", "do")

    def __init__(self, lexer=None):
        self.lexer = lexer or CppLexer()

    def analyze(self, text, path=""):
        extension = "." + str(path).rsplit(".", 1)[-1].lower() if "." in str(path) else ""
        if extension and extension not in SUPPORTED_EXTENSIONS:
            return {"supported": False, "functions": [], "controls": {}, "preprocessor": {},
                    "macros": {}, "constants": {}, "calls": {}}
        lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        token_lines = [self.lexer.tokenize(line) for line in lines]
        functions = self._functions(lines, token_lines, path)
        controls = self._control_context(lines, token_lines)
        preprocessor = self._preprocessor_context(lines)
        macros, constants = self._definitions(lines)
        calls = self._calls(token_lines)
        return {
            "supported": True, "functions": functions, "controls": controls,
            "preprocessor": preprocessor, "macros": macros, "constants": constants,
            "calls": calls, "lines": lines, "tokens": token_lines,
        }

    def function_for_line(self, analysis, line_number):
        candidates = [item for item in analysis.get("functions", [])
                      if item.start_line <= int(line_number) <= item.end_line]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _functions(self, lines, token_lines, path):
        functions = []
        brace_depth = 0
        brace_starts = {}
        brace_headers = {}
        brace_scope = {}
        scope_stack = []
        pending = []
        for index, tokens in enumerate(token_lines, 1):
            for token_index, token in enumerate(tokens):
                if token == "{":
                    header = list(pending)
                    if not header:
                        header = list(tokens[:token_index])
                    brace_depth += 1
                    brace_starts[brace_depth] = index
                    brace_headers[brace_depth] = header
                    scope_name = None
                    if "namespace" in header:
                        position = header.index("namespace")
                        if position + 1 < len(header):
                            scope_name = header[position + 1]
                    elif "class" in header or "struct" in header:
                        keyword = "class" if "class" in header else "struct"
                        position = header.index(keyword)
                        if position + 1 < len(header):
                            scope_name = header[position + 1]
                    brace_scope[brace_depth] = scope_name
                    if scope_name:
                        scope_stack.append(scope_name)
                    pending = []
                elif token == "}":
                    start = brace_starts.pop(brace_depth, None)
                    header = brace_headers.pop(brace_depth, [])
                    if start is not None and start <= index:
                        identity = self._identity_from_tokens(header, path, scope_stack)
                        if identity:
                            body = []
                            for line_tokens in token_lines[start:index]:
                                body.extend(line_tokens)
                            functions.append(FunctionRange(
                                identity, start, index, body_tokens=body,
                                uncertain=self._looks_uncertain(" ".join(header)),
                            ))
                    brace_depth = max(0, brace_depth - 1)
                    closing_scope = brace_scope.pop(brace_depth + 1, None)
                    if closing_scope and scope_stack and scope_stack[-1] == closing_scope:
                        scope_stack.pop()
                    pending = []
                else:
                    pending.append(token)
                    if token == ";":
                        pending = []
        # Nested class/namespace braces can produce a non-function range, and
        # declaration-only prototypes have no body.  Keep deterministic order.
        functions.sort(key=lambda item: (item.start_line, item.end_line, item.identity.canonical()))
        unique = []
        seen = set()
        for item in functions:
            key = (item.identity.canonical(), item.start_line, item.end_line)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    @staticmethod
    def _tokens_between(token_lines, start_index, end_index):
        tokens = []
        for row in token_lines[max(0, start_index):max(0, end_index)]:
            tokens.extend(row)
        return tokens

    def _identity_from_prefix(self, prefix, path):
        return self._identity_from_tokens(self.lexer.tokenize(prefix), path)

    def _identity_from_tokens(self, tokens, path, scope_prefix=()):
        if "(" not in tokens or ")" not in tokens:
            return None
        open_index = max(index for index, token in enumerate(tokens) if token == "(")
        close_index = None
        depth = 0
        for index in range(open_index, len(tokens)):
            if tokens[index] == "(":
                depth += 1
            elif tokens[index] == ")":
                depth -= 1
                if depth == 0:
                    close_index = index
                    break
        if close_index is None or open_index == 0:
            return None
        name_index = open_index - 1
        while name_index >= 0 and tokens[name_index] in ("*", "&", "&&", "::"):
            name_index -= 1
        if name_index < 0:
            return None
        name = tokens[name_index]
        if name in self.CONTROL_WORDS or name in ("function", "sizeof", "return"):
            return None
        # A function-like macro or lambda has no stable source identity.
        if "[" in tokens[:open_index] or "#" in tokens[:open_index]:
            return None
        scope = list(scope_prefix or ())
        for index in range(0, name_index - 1):
            if tokens[index] == "namespace" and index + 1 < len(tokens):
                scope.append(tokens[index + 1])
            if tokens[index] == "class" and index + 1 < len(tokens):
                scope.append(tokens[index + 1])
        parameters = tuple(tokens[open_index + 1:close_index])
        qualifiers = tuple(tokens[close_index + 1:])
        trailing = ()
        if "->" in qualifiers:
            arrow = qualifiers.index("->")
            trailing = qualifiers[arrow + 1:]
            qualifiers = qualifiers[:arrow]
        return FunctionIdentity(path, scope, name, parameters, qualifiers, trailing)

    @staticmethod
    def _looks_uncertain(prefix):
        return any(token in prefix for token in ("<", ">", "operator", "->*"))

    def _control_context(self, lines, token_lines):
        context = {}
        stack = []
        for index, tokens in enumerate(token_lines, 1):
            if tokens:
                words = [token for token in tokens if token in self.CONTROL_WORDS]
                if words:
                    control = " ".join(tokens)
                    stack.append(control)
            context[index] = tuple(stack)
            # Braces close the nearest control scope conservatively.  This is
            # deliberately fail-closed for malformed/nested macro constructs.
            closes = sum(1 for token in tokens if token == "}")
            for _ in range(min(closes, len(stack))):
                stack.pop()
        return context

    @staticmethod
    def _preprocessor_context(lines):
        stack = []
        context = {}
        for index, line in enumerate(lines, 1):
            stripped = line.strip()
            context[index] = tuple(stack)
            if not stripped.startswith("#"):
                continue
            directive = stripped[1:].strip()
            match = re.match(r"(if|ifdef|ifndef|elif|else|endif)\b(.*)", directive)
            if not match:
                continue
            kind, expression = match.group(1), match.group(2).strip()
            if kind == "endif":
                if stack:
                    stack.pop()
            elif kind in ("elif", "else"):
                if stack:
                    stack[-1] = "{} {}".format(kind, expression).strip()
            else:
                stack.append("{} {}".format(kind, expression).strip())
        return context

    def _definitions(self, lines):
        macros = {}
        constants = {}
        for index, line in enumerate(lines, 1):
            match = re.match(r"\s*#\s*define\s+([A-Za-z_]\w*)(.*)$", line)
            if match:
                macros[match.group(1)] = (index, tuple(normalize_cpp(match.group(2))))
            match = re.match(
                r"\s*(?:constexpr|const)\s+[A-Za-z_:][\w:<>]*\s+([A-Za-z_]\w*)\s*=\s*(.*?);?\s*$",
                line,
            )
            if match:
                constants[match.group(1)] = (index, tuple(normalize_cpp(match.group(2))))
        return macros, constants

    def _calls(self, token_lines):
        calls = {}
        for index, tokens in enumerate(token_lines, 1):
            names = []
            for offset, token in enumerate(tokens[:-1]):
                if re.match(r"^[A-Za-z_]\w*$", token) and tokens[offset + 1] == "(":
                    if token not in self.CONTROL_WORDS:
                        names.append(token)
            calls[index] = tuple(names)
        return calls
