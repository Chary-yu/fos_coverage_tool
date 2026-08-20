"""Sidecar and report-registry inventory/integrity audit (Item 22)."""

import hashlib
import json
import os
import re
import sys
from typing import Dict, Any, List

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from code_detail_service import get_configured_registry_dir, is_valid_report_id


def _cache_path(directory, report_id):
    directory = os.path.realpath(directory)
    if os.path.basename(directory) == ".source_cache":
        return os.path.join(directory, report_id)
    return os.path.join(directory, ".source_cache", report_id)


def audit_sidecar_and_registry(search_roots: List[str]) -> Dict[str, Any]:
    """Audit registry reachability, sidecar inventory, and chunk integrity.

    Registry paths are never silently filtered.  A registry can explicitly set
    ``sidecar_required=false`` for immediate/legacy reports; otherwise a lazy
    report with no discoverable sidecar is an integrity failure rather than a
    false green zero-sidecar result.
    """
    reg_dir = os.path.realpath(get_configured_registry_dir())
    registered_reports: Dict[str, Dict[str, Any]] = {}
    corrupted_registries = []
    duplicate_identities = []

    if os.path.isdir(reg_dir):
        for fname in sorted(os.listdir(reg_dir)):
            if not fname.endswith(".json") or fname.startswith("."):
                continue
            fpath = os.path.join(reg_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as stream:
                    data = json.load(stream)
                if not isinstance(data, dict):
                    raise ValueError("registry must be an object")
                rid = data.get("report_id") or os.path.splitext(fname)[0]
                if not is_valid_report_id(rid):
                    corrupted_registries.append("Invalid report_id format in registry: {}".format(fname))
                if rid in registered_reports:
                    duplicate_identities.append(rid)
                dirs = data.get("directories", [])
                if isinstance(dirs, str):
                    dirs = [dirs]
                if not isinstance(dirs, list) or not dirs:
                    corrupted_registries.append("Registry has no directories: {}".format(fname))
                    dirs = []
                normalized_dirs = []
                for directory in dirs:
                    directory = os.path.realpath(str(directory))
                    normalized_dirs.append(directory)
                    if not os.path.isdir(directory):
                        corrupted_registries.append("Registry directory is missing: {}".format(directory))
                registered_reports[rid] = {
                    "directories": normalized_dirs,
                    "sidecar_required": bool(data.get("sidecar_required", False)),
                    "registry_file": fpath,
                }
            except Exception as exc:
                corrupted_registries.append("Corrupted registry JSON: {} ({})".format(fname, exc))

    total_sidecars = 0
    chunked_v2_count = 0
    legacy_v1_count = 0
    orphaned_caches = []
    corrupted_chunks = []
    discovered_by_report = {}

    for s_root in search_roots:
        cache_base = os.path.join(os.path.realpath(s_root), ".source_cache")
        if not os.path.isdir(cache_base):
            continue
        for r_entry in sorted(os.listdir(cache_base)):
            r_path = os.path.join(cache_base, r_entry)
            if not os.path.isdir(r_path) or os.path.islink(r_path):
                corrupted_chunks.append("Symlink or invalid report cache directory: {}".format(r_path))
                continue
            report_id = r_entry
            discovered_by_report[report_id] = discovered_by_report.get(report_id, 0) + 1
            if report_id not in registered_reports and not report_id.startswith("report_benchmark_"):
                orphaned_caches.append(r_path)

            for item in sorted(os.listdir(r_path)):
                item_path = os.path.join(r_path, item)
                if os.path.isdir(item_path):
                    if os.path.islink(item_path):
                        corrupted_chunks.append("Symlink sidecar directory: {}".format(item_path))
                        continue
                    meta_path = os.path.join(item_path, "meta.json")
                    if not os.path.isfile(meta_path):
                        corrupted_chunks.append("Sidecar directory has no meta.json: {}".format(item_path))
                        continue
                    total_sidecars += 1
                    chunked_v2_count += 1
                    try:
                        with open(meta_path, "r", encoding="utf-8") as stream:
                            mdata = json.load(stream)
                        if mdata.get("schema_version") != 2 or "total_lines" not in mdata or "total_chunks" not in mdata:
                            corrupted_chunks.append("Missing v2 fields in {}".format(meta_path))
                        if mdata.get("report_id") and mdata.get("report_id") != report_id:
                            corrupted_chunks.append("Report identity mismatch in {}".format(meta_path))
                        chunks = mdata.get("chunks") or []
                        total_lines = int(mdata.get("total_lines") or 0)
                        chunk_size = int(mdata.get("chunk_size") or 0)
                        total_chunks = int(mdata.get("total_chunks") or 0)
                        expected_chunks = ((total_lines + chunk_size - 1) // chunk_size) if total_lines and chunk_size > 0 else 0
                        if (not isinstance(chunks, list) or len(chunks) != total_chunks
                                or total_lines < 0 or chunk_size <= 0 or total_chunks != expected_chunks):
                            corrupted_chunks.append("Chunk inventory mismatch in {}".format(meta_path))
                        ranges = []
                        for chunk_name in chunks:
                            if not isinstance(chunk_name, str) or os.path.basename(chunk_name) != chunk_name:
                                corrupted_chunks.append("Chunk path escapes sidecar root: {}".format(meta_path))
                                continue
                            chunk_path = os.path.realpath(os.path.join(item_path, chunk_name))
                            if os.path.commonpath((os.path.realpath(item_path), chunk_path)) != os.path.realpath(item_path):
                                corrupted_chunks.append("Chunk path escapes sidecar root: {}".format(meta_path))
                                continue
                            if not os.path.isfile(chunk_path):
                                corrupted_chunks.append("Missing sidecar chunk: {}".format(chunk_path))
                                continue
                            with open(chunk_path, "r", encoding="utf-8") as stream:
                                chunk_lines = json.load(stream)
                            if not isinstance(chunk_lines, list):
                                corrupted_chunks.append("Chunk is not an array: {}".format(chunk_path))
                            match = re.match(r"^lines-(\d+)-(\d+)\.json$", chunk_name)
                            if match:
                                ranges.append((int(match.group(1)), int(match.group(2))))
                        ranges.sort()
                        previous_end = None
                        for range_index, (start, end) in enumerate(ranges):
                            expected_start = range_index * chunk_size
                            expected_end = expected_start + chunk_size - 1
                            if start != expected_start or end != expected_end:
                                corrupted_chunks.append("Non-contiguous chunk range in {}".format(meta_path))
                            if previous_end is not None and start <= previous_end:
                                corrupted_chunks.append("Overlapping chunk ranges in {}".format(meta_path))
                            previous_end = end
                        content_hash = mdata.get("content_hash")
                        if content_hash:
                            lines = []
                            for chunk_name in chunks:
                                with open(os.path.join(item_path, chunk_name), "r", encoding="utf-8") as stream:
                                    lines.extend(json.load(stream))
                            actual_hash = hashlib.sha256(json.dumps(
                                lines, ensure_ascii=False, sort_keys=True
                            ).encode("utf-8")).hexdigest()
                            if actual_hash != content_hash:
                                corrupted_chunks.append("Content hash mismatch in {}".format(meta_path))
                    except Exception as exc:
                        corrupted_chunks.append("Corrupted meta/chunk data in {} ({})".format(item_path, exc))
                elif item.endswith(".source.json"):
                    if os.path.islink(item_path):
                        corrupted_chunks.append("Symlink legacy sidecar: {}".format(item_path))
                        continue
                    total_sidecars += 1
                    legacy_v1_count += 1
                    try:
                        with open(item_path, "r", encoding="utf-8") as stream:
                            legacy = json.load(stream)
                        if not isinstance(legacy, dict) or not isinstance(legacy.get("lines", []), list):
                            corrupted_chunks.append("Invalid legacy sidecar: {}".format(item_path))
                    except Exception as exc:
                        corrupted_chunks.append("Corrupted legacy sidecar: {} ({})".format(item_path, exc))

    for report_id, info in registered_reports.items():
        if info.get("sidecar_required"):
            expected = [_cache_path(directory, report_id) for directory in info["directories"]]
            if not any(os.path.isdir(path) for path in expected):
                corrupted_registries.append("Registered lazy report has no sidecar cache: {}".format(report_id))
        elif discovered_by_report.get(report_id, 0) == 0:
            info["sidecar_inventory"] = "NOT_REQUIRED"

    is_safe = not (corrupted_registries or duplicate_identities or corrupted_chunks or orphaned_caches)
    return {
        "status": "AUDIT_PASSED" if is_safe else "VIOLATIONS_FOUND",
        "registered_report_count": len(registered_reports),
        "corrupted_registries": corrupted_registries,
        "duplicate_report_identities": duplicate_identities,
        "corrupted_chunks": corrupted_chunks,
        "orphaned_cache_count": len(orphaned_caches),
        "orphaned_caches": orphaned_caches,
        "total_sidecars": total_sidecars,
        "chunked_v2_count": chunked_v2_count,
        "legacy_v1_count": legacy_v1_count,
        "discovered_sidecar_reports": discovered_by_report,
        "is_safe": is_safe,
    }


if __name__ == "__main__":
    roots = [_REPO_ROOT, "/opt/coverage_tool", "/opt/coverage_reports"]
    result = audit_sidecar_and_registry(roots)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result["is_safe"] else 1)
