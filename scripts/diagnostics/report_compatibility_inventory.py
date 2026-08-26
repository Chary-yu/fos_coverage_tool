"""Read-only inventory of historical HTML report compatibility contracts.

The production/backup report root is intentionally supplied by the operator.
This tool never rewrites reports or assets; it only records the HTML metadata,
API strings, local asset hashes, and missing identity fields needed by the
COMPAT-005 release gate.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
_CURRENT_API_VERSION = "vnext-api-20260826.1"
_DEFAULT_MAX_FILES = 100000
_MAX_HTML_BYTES = 32 * 1024 * 1024
_ASSET_RE = re.compile(
    r"(?:src|href)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)
_ENDPOINT_RE = re.compile(
    r"[\"'`]((?:https?://[^\"'`\s]+)?/api/[A-Za-z0-9_./{}?=&$:-]+)[\"'`]"
)
_KEY_VALUE_RE = re.compile(
    r"[\"'](?P<key>api_contract_version|asset_identity|report_id|scan_id|"
    r"commit_sha|release_commit_sha)[\"']\s*:\s*[\"'](?P<value>[^\"']*)[\"']",
    re.IGNORECASE,
)
_META_RE = re.compile(
    r"<meta\s+[^>]*?(?:name|property)\s*=\s*[\"']([^\"']+)[\"']"
    r"[^>]*?content\s*=\s*[\"']([^\"']*)[\"'][^>]*>",
    re.IGNORECASE,
)


def _revision(repo_root):
    try:
        return subprocess.check_output(
            ["git", "-C", os.path.abspath(repo_root), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode("ascii").strip()
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return ""


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_local_asset(root, reference):
    parsed = urlsplit(str(reference or ""))
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    candidate = os.path.realpath(os.path.join(root, parsed.path.lstrip("/")))
    root_real = os.path.realpath(root)
    if candidate != root_real and not candidate.startswith(root_real + os.sep):
        return None
    return candidate


def _metadata(text):
    values = {}
    for key, value in _KEY_VALUE_RE.findall(text):
        values.setdefault(key.lower(), []).append(value)
    for key, value in _META_RE.findall(text):
        normalized = key.strip().lower().replace("-", "_")
        if normalized in {
            "api_contract_version", "asset_identity", "report_id",
            "scan_id", "commit_sha", "release_commit_sha",
        }:
            values.setdefault(normalized, []).append(value)
    return {
        key: sorted(set(value)) for key, value in sorted(values.items())
    }


def _inventory_file(path, root):
    relative = os.path.relpath(path, root)
    with open(path, "rb") as stream:
        raw = stream.read(_MAX_HTML_BYTES + 1)
    truncated = len(raw) > _MAX_HTML_BYTES
    text = raw[:_MAX_HTML_BYTES].decode("utf-8", errors="replace")
    metadata = _metadata(text)
    references = sorted(set(_ASSET_RE.findall(text)))
    assets = []
    for reference in references:
        local_path = _safe_local_asset(root, reference)
        item = {"reference": reference, "local": bool(local_path)}
        if local_path and os.path.isfile(local_path):
            item["path"] = os.path.relpath(local_path, root)
            item["sha256"] = _sha256(local_path)
        elif local_path:
            item["missing"] = True
        assets.append(item)
    endpoints = sorted(set(_ENDPOINT_RE.findall(text)))
    api_version = (metadata.get("api_contract_version") or [""])[0]
    has_release = bool(
        (metadata.get("commit_sha") or []) or
        (metadata.get("release_commit_sha") or [])
    )
    if api_version == _CURRENT_API_VERSION and has_release:
        classification = "CANONICAL_VNEXT"
    elif api_version or has_release:
        classification = "VERSIONED_NONCURRENT"
    else:
        classification = "UNVERSIONED_HISTORICAL"
    return {
        "path": relative,
        "size_bytes": len(raw),
        "truncated": truncated,
        "metadata": metadata,
        "asset_references": assets,
        "api_endpoints": endpoints,
        "classification": classification,
        "missing_identity": not (api_version and has_release),
    }


def inventory(roots, repo_root=ROOT, max_files=_DEFAULT_MAX_FILES):
    roots = [os.path.realpath(path) for path in roots]
    files = []
    skipped = []
    for root in roots:
        if not os.path.isdir(root):
            skipped.append({"root": root, "reason": "missing_or_not_directory"})
            continue
        for directory, _, names in os.walk(root, followlinks=False):
            for name in sorted(names):
                if not name.lower().endswith((".html", ".htm")):
                    continue
                if len(files) >= int(max_files):
                    skipped.append({"root": root, "reason": "max_files_reached"})
                    break
                path = os.path.join(directory, name)
                try:
                    files.append(_inventory_file(path, root))
                except (OSError, ValueError) as exc:
                    skipped.append({
                        "root": root, "path": os.path.relpath(path, root),
                        "reason": "read_failed", "error": str(exc),
                    })
            if len(files) >= int(max_files):
                break
    counts = {}
    for item in files:
        counts[item["classification"]] = counts.get(item["classification"], 0) + 1
    missing = sum(1 for item in files if item["missing_identity"])
    missing_assets = sum(
        1 for item in files
        for asset in item["asset_references"]
        if asset.get("missing")
    )
    truncated = sum(1 for item in files if item["truncated"])
    return {
        "status": "PASSED" if not skipped and not missing_assets and not truncated
        else "INCOMPLETE",
        "evidence_class": "report_compatibility_inventory",
        "synthetic": False,
        "read_only": True,
        "candidate_revision": _revision(repo_root),
        "host_identity": {
            "hostname": platform.node(), "platform": platform.platform(),
        },
        "roots": roots,
        "current_api_contract_version": _CURRENT_API_VERSION,
        "files_scanned": len(files),
        "files_missing_identity": missing,
        "local_assets_missing": missing_assets,
        "html_files_truncated": truncated,
        "classification_counts": counts,
        "reports": files,
        "skipped": skipped,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", action="append", required=True,
                        help="report directory; may be supplied more than once")
    parser.add_argument("--repo-root", default=ROOT)
    parser.add_argument("--max-files", type=int, default=_DEFAULT_MAX_FILES)
    parser.add_argument("--output")
    parser.add_argument("--strict", action="store_true",
                        help="fail when a root/read error is observed")
    args = parser.parse_args(argv)
    if args.max_files < 1:
        parser.error("--max-files must be positive")
    result = inventory(args.root, repo_root=args.repo_root, max_files=args.max_files)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = args.output if os.path.isabs(args.output) else os.path.join(os.getcwd(), args.output)
        directory = os.path.dirname(os.path.abspath(output))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(output, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.write("\n")
    print(encoded)
    return 1 if args.strict and result["status"] != "PASSED" else 0


if __name__ == "__main__":
    sys.exit(main())
