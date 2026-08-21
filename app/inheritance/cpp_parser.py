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
        token_lines = [tokens for _, tokens in self.lexer.tokenize_lines(text)]
        lexer_uncertain = bool(getattr(self.lexer, "last_lines_uncertain", False))
        functions = self._functions(lines, token_lines, path, lexer_uncertain)
        controls = self._control_context(
            lines, token_lines, getattr(self.lexer, "last_line_splices", set())
        )
        preprocessor = self._preprocessor_context(lines)
        macros, constants = self._definitions(lines)
        calls = self._calls(token_lines)
        return {
            "supported": True, "functions": functions, "controls": controls,
            "preprocessor": preprocessor, "macros": macros, "constants": constants,
            "calls": calls, "lines": lines, "tokens": token_lines,
            "path": path,
            "uncertain": lexer_uncertain,
        }

    def function_for_line(self, analysis, line_number):
        candidates = [item for item in analysis.get("functions", [])
                      if item.start_line <= int(line_number) <= item.end_line]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _functions(self, lines, token_lines, path, lexer_uncertain=False):
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
                                uncertain=(lexer_uncertain or
                                           self._looks_uncertain(" ".join(header))),
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

    def _control_context(self, lines, token_lines, line_splices=None):
        context = {}
        active = []
        brace_depth = 0
        groups = []
        index = 0
        line_splices = set(line_splices or ())
        while index < len(token_lines):
            start = index + 1
            end = start
            tokens = list(token_lines[index])
            while end in line_splices and end < len(token_lines):
                tokens.extend(token_lines[end])
                end += 1
            groups.append((start, end, tuple(tokens)))
            index = end
        for start_line, end_line, tokens in groups:
            line_context = [item[1] for item in active]
            pending = []
            token_index = 0
            while token_index < len(tokens):
                token = tokens[token_index]
                if token in self.CONTROL_WORDS and self._control_start(tokens, token_index):
                    descriptor, end = self._control_descriptor(tokens, token_index)
                    if descriptor:
                        # A control header and its opening brace frequently
                        # share one physical line.  Record it for that line
                        # immediately; assigning context only at line start
                        # would make ``if (x) { return ...; }`` look ordinary.
                        if descriptor not in line_context:
                            line_context.append(descriptor)
                        pending.append(descriptor)
                    token_index = max(token_index + 1, end)
                    continue
                if token == "{":
                    brace_depth += 1
                    active.extend((brace_depth, descriptor) for descriptor in pending)
                    pending = []
                elif token == "}":
                    active = [item for item in active if item[0] < brace_depth]
                    brace_depth = max(0, brace_depth - 1)
                token_index += 1
            # A control header without a brace has no safely knowable extent.
            # Keep it only for the current physical line; the following line
            # remains ordinary pending instead of inheriting through a guessed
            # statement boundary.
            for physical_line in range(start_line, end_line + 1):
                context[physical_line] = tuple(line_context)
        return context

    @staticmethod
    def _control_start(tokens, index):
        if index == 0:
            return True
        return tokens[index - 1] in ("{", "}", ";", ":", "else", "do")

    @staticmethod
    def _control_descriptor(tokens, index):
        token = tokens[index]
        if token in ("else", "do"):
            return token, index + 1
        if token == "case":
            end = index + 1
            while end < len(tokens) and tokens[end] != ":":
                end += 1
            return " ".join(tokens[index:end + 1]), min(len(tokens), end + 1)
        if index + 1 >= len(tokens) or tokens[index + 1] != "(":
            return token, index + 1
        depth = 0
        end = index + 1
        while end < len(tokens):
            if tokens[end] == "(":
                depth += 1
            elif tokens[end] == ")":
                depth -= 1
                if depth == 0:
                    end += 1
                    break
            end += 1
        return " ".join(tokens[index:end]), end

    @staticmethod
    def _preprocessor_context(lines):
        stack = []
        context = {}
        logical_lines = CppLexer._logical_lines(lines)
        for start_line, end_line, line in logical_lines:
            for physical_line in range(start_line, end_line + 1):
                context[physical_line] = tuple(stack)
            stripped = line.strip()
            if not stripped.startswith("#"):
                continue
            directive = stripped[1:].strip()
            match = re.match(r"(if|ifdef|ifndef|elif|else|endif)\b(.*)", directive)
            if not match:
                continue
            kind, expression = match.group(1), match.group(2).strip()
            expression = " ".join(normalize_cpp(expression))
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
        for index, _, line in CppLexer._logical_lines(lines):
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
