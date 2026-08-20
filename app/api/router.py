"""Exact method/path router with no browser-side fallback guessing."""

import re


class Router(object):
    def __init__(self):
        self._routes = []

    def add(self, method, pattern, handler):
        self._routes.append((str(method).upper(), re.compile(pattern), handler))
        return self

    def dispatch(self, method, path, *args, **kwargs):
        for route_method, pattern, handler in self._routes:
            if route_method != str(method).upper():
                continue
            match = pattern.match(path)
            if match and match.end() == len(path):
                return handler(*match.groups(), *args, **kwargs)
        raise KeyError("route not found")

    def describe(self):
        return [{"method": method, "pattern": pattern.pattern}
                for method, pattern, _ in self._routes]
