"""Small C/C++ lexical normalizer that preserves real tokens."""

from __future__ import absolute_import

import re


class CppLexer(object):
    IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

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
            # C++ raw strings.  Keep the delimiter and body as one literal so
            # comment markers inside it can never be mistaken for comments.
            if text.startswith("R\"", index):
                close = text.find(")\"", index + 2)
                if close >= 0:
                    delimiter_start = text.find("(", index + 2, close)
                    if delimiter_start >= 0:
                        delimiter = text[delimiter_start + 1:close]
                        end = text.find(")" + delimiter + "\"", close + 2)
                        if end >= 0:
                            end += len(delimiter) + 2
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
        """Return physical-line token tuples for line-map evidence."""
        lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        return [(index + 1, self.tokenize(line)) for index, line in enumerate(lines)]


def normalize_cpp(text):
    return CppLexer().tokenize(text)


def tokens_equal(left, right):
    return list(left or []) == list(right or [])
