"""Deterministic, fail-closed Analysis Inheritance engine."""

from app.inheritance.normalizer import CppLexer, normalize_cpp, tokens_equal
from app.inheritance.line_map import GitLineMapEngine, LineMapping
from app.inheritance.cpp_parser import CppSourceAnalyzer, FunctionIdentity
from app.inheritance.engine import InheritanceEngine

__all__ = [
    "CppLexer", "normalize_cpp", "tokens_equal", "GitLineMapEngine",
    "LineMapping", "CppSourceAnalyzer", "FunctionIdentity", "InheritanceEngine",
]
