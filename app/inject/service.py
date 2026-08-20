"""Canonical inject parsing service facade."""

from app.inject.parse_once import parse_gcov_source_once


class InjectService:
    parse_once = staticmethod(parse_gcov_source_once)
