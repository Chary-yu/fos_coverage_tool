"""Small C/C++ lexical normalizer that preserves real tokens."""

from __future__ import absolute_import

import re


class CppLexer(object):
    IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
    RAW_PREFIXES = ("u8R\"", "uR\"", "UR\"", "LR\"", "R\"")

    @classmethod
    def _raw_prefix_at(cls, text, index):
        for prefix in cls.RAW_PREFIXES:
            if text.startswith(prefix, index):
                return prefix
        return ""

    @staticmethod
    def _has_line_splice(line):
        trailing = 0
        for char in reversed(line):
            if char != "\\":
                break
            trailing += 1
        return bool(trailing % 2)

    @classmethod
    def _logical_lines(cls, lines):
        """Join translation-phase line splices while retaining line bounds."""
        result = []
        index = 0
        while index < len(lines):
            start = index + 1
            end = start
            value = lines[index]
            while cls._has_line_splice(value) and index + 1 < len(lines):
                value = value[:-1] + lines[index + 1]
                index += 1
                end = index + 1
            result.append((start, end, value))
            index += 1
        return result

    def tokenize(self, text):
        text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        # Translation phase line splicing is removed before comment handling,
        # but physical-line callers should use tokenize_lines for provenance.
        text = text.replace("\\\n", "")
        tokens = []
        index = 0
        length = len(text)
        while index < length:
            char = text[index]
            if char.isspace():
                index += 1
                continue
            if text.startswith("//", index):
                end = text.find("\n", index + 2)
                index = length if end < 0 else end + 1
                continue
            if text.startswith("/*", index):
                end = text.find("*/", index + 2)
                index = length if end < 0 else end + 2
                continue
            if char in ("\"", "'"):
                quote = char
                start = index
                index += 1
                while index < length:
                    if text[index] == "\\":
                        index += 2
                        continue
                    if text[index] == quote:
                        index += 1
                        break
                    index += 1
                tokens.append(text[start:index])
                continue
            # C++ raw strings. Keep all standard encoding prefixes and the
            # delimiter/body as one literal so comment markers inside it can
            # never be mistaken for comments.
            raw_prefix = self._raw_prefix_at(text, index)
            if raw_prefix:
                open_paren = text.find("(", index + len(raw_prefix))
                if open_paren >= 0:
                    delimiter = text[index + len(raw_prefix):open_paren]
                    if len(delimiter) <= 16 and not any(
                            item.isspace() or item in ("(", ")", "\\", '"')
                            for item in delimiter):
                        closing = ")" + delimiter + '"'
                        end = text.find(closing, open_paren + 1)
                        if end >= 0:
                            end += len(closing)
                            tokens.append(text[index:end])
                            index = end
                            continue
            match = self.IDENTIFIER.match(text, index)
            if match:
                tokens.append(match.group(0))
                index = match.end()
                continue
            number = re.match(r"(?:0[xX][0-9A-Fa-f]+|[0-9]+(?:\.[0-9]*)?)", text[index:])
            if number:
                tokens.append(number.group(0))
                index += len(number.group(0))
                continue
            matched = False
            for operator in ("::", "->", "++", "--", "&&", "||", "==", "!=",
                             "<=", ">=", "+=", "-=", "*=", "/=", "<<", ">>",
                             "##", "...", "?."):
                if text.startswith(operator, index):
                    tokens.append(operator)
                    index += len(operator)
                    matched = True
                    break
            if matched:
                continue
            tokens.append(char)
            index += 1
        return tokens

    def tokenize_lines(self, text):
        """Return physical-line token tuples while retaining lexical state.

        A line-by-line call to :meth:`tokenize` loses whether a ``/*`` block
        comment, escaped literal, or C++ raw string started on the previous
        physical line.  That can turn comment text into fake identifiers and,
        more seriously, make the inheritance parser appear certain when the
        translation unit was not tokenized faithfully.  Keep the public return
        shape stable, but expose ``last_lines_uncertain`` for callers that need
        to fail closed on an unterminated construct.
        """
        lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        result = []
        line_splices = set()
        state = {
            "block_comment": False,
            "quote": None,
            "literal": [],
            "raw_delimiter": None,
            "raw_literal": [],
        }
        for line_number, line in enumerate(lines, 1):
            tokens = []
            index = 0
            length = len(line)
            while index < length:
                if state["block_comment"]:
                    end = line.find("*/", index)
                    if end < 0:
                        index = length
                        continue
                    state["block_comment"] = False
                    index = end + 2
                    continue

                if state["raw_delimiter"] is not None:
                    closing = "){}\"".format(state["raw_delimiter"])
                    end = line.find(closing, index)
                    if end < 0:
                        state["raw_literal"].append(line[index:])
                        index = length
                        continue
                    state["raw_literal"].append(line[index:end + len(closing)])
                    tokens.append("".join(state["raw_literal"]))
                    state["raw_delimiter"] = None
                    state["raw_literal"] = []
                    index = end + len(closing)
                    continue

                if state["quote"] is not None:
                    char = line[index]
                    state["literal"].append(char)
                    index += 1
                    if char == "\\":
                        if index < length:
                            state["literal"].append(line[index])
                            index += 1
                        elif self._has_line_splice(line):
                            # The escaped physical newline is removed during
                            # translation phase 2; retain the open literal
                            # state but do not include the backslash token.
                            state["literal"].pop()
                            line_splices.add(line_number)
                        # A trailing backslash continues the literal onto the
                        # next physical line; keep the quote state alive.
                        continue
                    if char == state["quote"]:
                        tokens.append("".join(state["literal"]))
                        state["quote"] = None
                        state["literal"] = []
                    continue

                char = line[index]
                if char.isspace():
                    index += 1
                    continue
                if line.startswith("//", index):
                    break
                if line.startswith("/*", index):
                    state["block_comment"] = True
                    index += 2
                    continue
                raw_prefix = self._raw_prefix_at(line, index)
                if raw_prefix:
                    open_paren = line.find("(", index + len(raw_prefix))
                    if open_paren >= 0 and open_paren - (index + len(raw_prefix)) <= 16:
                        delimiter = line[index + len(raw_prefix):open_paren]
                        if not any(item.isspace() or item in ("(", ")", "\\", '"')
                                   for item in delimiter):
                            state["raw_delimiter"] = delimiter
                            state["raw_literal"] = [line[index:open_paren + 1]]
                            index = open_paren + 1
                            continue
                if char in ('"', "'"):
                    state["quote"] = char
                    state["literal"] = [char]
                    index += 1
                    continue

                match = self.IDENTIFIER.match(line, index)
                if match:
                    tokens.append(match.group(0))
                    index = match.end()
                    continue
                number = re.match(
                    r"(?:0[xX][0-9A-Fa-f]+|[0-9]+(?:\.[0-9]*)?)",
                    line[index:],
                )
                if number:
                    tokens.append(number.group(0))
                    index += len(number.group(0))
                    continue
                matched = False
                for operator in ("::", "->", "++", "--", "&&", "||", "==", "!=",
                                 "<=", ">=", "+=", "-=", "*=", "/=", "<<", ">>",
                                 "##", "...", "?."):
                    if line.startswith(operator, index):
                        tokens.append(operator)
                        index += len(operator)
                        matched = True
                        break
                if matched:
                    continue
                if char == "\\" and index == length - 1 and self._has_line_splice(line):
                    # Translation phase 2 removes backslash-newline. Keep
                    # physical line numbering in the result, but do not let
                    # the splice become a real token or an uncertainty.
                    line_splices.add(line_number)
                    index += 1
                    continue
                tokens.append(char)
                index += 1
            result.append((line_number, tuple(tokens)))
        self.last_lines_uncertain = bool(
            state["block_comment"] or state["quote"] is not None or
            state["raw_delimiter"] is not None
        )
        self.last_line_splices = line_splices
        return result


def normalize_cpp(text):
    return CppLexer().tokenize(text)


def tokens_equal(left, right):
    return list(left or []) == list(right or [])
