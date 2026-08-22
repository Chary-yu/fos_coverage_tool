#!/usr/bin/env python3
"""Preflight and upgrade legacy runtime configuration without overwriting it."""

from __future__ import print_function

import argparse
import copy
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.config.runtime_config import (
    CONFIG_SCHEMA_VERSION, _apply_compatibility_aliases,
    default_runtime_config, normalize_candidate_paths,
)


AUTO_PATHS = (
    ("mysql", "host"), ("mysql", "port"), ("mysql", "user"),
    ("mysql", "password"), ("mysql", "database"), ("server", "host"),
    ("server", "port"), ("runtime_state", "root"), ("runtime_state", "jobs_dir"),
    ("runtime_state", "registry_dir"), ("runtime_state", "exports_dir"),
    ("input_roots",), ("report_roots",),
)
MANUAL_PATHS = (
    ("auth", "mode"), ("auth", "trusted_proxy_addresses"),
    ("upgrade", "release_endpoint"), ("upgrade", "previous_release_endpoint"),
    ("upgrade", "previous_release"), ("upgrade", "commands"),
)


def _get(mapping, path):
    value = mapping
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _set(mapping, path, value):
    target = mapping
    for key in path[:-1]:
        target = target.setdefault(key, {})
    target[path[-1]] = value


def preflight_config(path, base_dir=None, output_path=None, write_candidate=False):
    path = os.path.realpath(path)
    base_dir = os.path.realpath(base_dir or os.path.dirname(path) or os.getcwd())
    with open(path, "r", encoding="utf-8-sig") as stream:
        original = json.load(stream)
    if not isinstance(original, dict):
        raise ValueError("config root must be an object")
    normalized_original = _apply_compatibility_aliases(original)
    candidate = copy.deepcopy(default_runtime_config(base_dir))
    diff = []
    provenance = {}
    for key, value in normalized_original.items():
        candidate[key] = copy.deepcopy(value)
    for path_item in AUTO_PATHS:
        value = _get(normalized_original, path_item)
        if value is not None:
            _set(candidate, path_item, copy.deepcopy(value))
            provenance[".".join(path_item)] = {
                "source": "legacy_config", "value": copy.deepcopy(value),
            }
        elif _get(candidate, path_item) is not None:
            diff.append({
                "path": ".".join(path_item), "classification": "auto_default",
                "suggested_value": copy.deepcopy(_get(candidate, path_item)),
            })
    for path_item in MANUAL_PATHS:
        value = _get(normalized_original, path_item)
        if value is None:
            diff.append({
                "path": ".".join(path_item), "classification": "manual_required",
                "suggested_value": None,
            })
        else:
            _set(candidate, path_item, copy.deepcopy(value))
            provenance[".".join(path_item)] = {
                "source": "legacy_config_requires_review", "value": copy.deepcopy(value),
            }
    candidate["config_schema_version"] = CONFIG_SCHEMA_VERSION
    candidate["_config_upgrade_provenance"] = {
        "source_path": path, "source_schema_version": int(
            original.get("config_schema_version") or 0
        ), "tool": "config-preflight-v1", "fields": provenance,
    }
    candidate = normalize_candidate_paths(candidate, base_dir)
    result = {
        "status": "PASSED" if not any(
            item["classification"] == "manual_required" for item in diff
        ) else "REVIEW_REQUIRED",
        "source_path": path,
        "source_sha256": _sha256(path),
        "source_config_schema_version": int(
            original.get("config_schema_version") or 0
        ),
        "target_config_schema_version": CONFIG_SCHEMA_VERSION,
        "diff": diff,
        "candidate": candidate,
        "candidate_path": os.path.realpath(output_path) if output_path else "",
        "source_unchanged": True,
    }
    if write_candidate:
        if not output_path:
            raise ValueError("output_path is required when writing a candidate")
        output_path = os.path.realpath(output_path)
        if output_path == path:
            raise ValueError("candidate config must not overwrite source config")
        parent = os.path.dirname(output_path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(output_path, "w", encoding="utf-8") as stream:
            json.dump(candidate, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        result["candidate_path"] = output_path
    return result


def _sha256(path):
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "upgrade"))
    parser.add_argument("config")
    parser.add_argument("--output", help="write candidate config to this new path")
    parser.add_argument("--base-dir", default=None)
    args = parser.parse_args(argv)
    result = preflight_config(
        args.config, base_dir=args.base_dir, output_path=args.output,
        write_candidate=args.command == "upgrade",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] in ("PASSED", "REVIEW_REQUIRED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
