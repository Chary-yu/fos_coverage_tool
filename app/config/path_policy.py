"""Fail-closed path validation for configured VNext input/output roots."""

import os


def realpath_within(path, roots):
    candidate = os.path.realpath(path)
    for root in roots or []:
        root = os.path.realpath(root)
        try:
            if os.path.commonpath((root, candidate)) == root:
                return candidate
        except ValueError:
            continue
    return None


def reject_relative_traversal(path):
    value = str(path or "").replace("\\", "/")
    if any(part == ".." for part in value.split("/")):
        raise ValueError("path traversal is not allowed")
    return value
