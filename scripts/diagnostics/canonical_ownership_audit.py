"""Audit canonical source ownership and generated compatibility copies."""

import hashlib
import os
from typing import Dict, Any


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def audit_canonical_ownership(repo_root: str) -> Dict[str, Any]:
    mappings = {
        "coverage_enhance.js": "web/assets/js/coverage_enhance.js",
        "coverage_progress.js": "web/assets/js/coverage_progress.js",
        "incremental_coverage.js": "web/assets/js/incremental_coverage.js",
        "incremental_developer_tasks.js": "web/assets/js/incremental_developer_tasks.js",
        "coverage_enhance.css": "web/assets/css/coverage_enhance.css",
        "coverage_progress.html": "web/templates/coverage_progress.html",
    }
    violations = []
    copies = []
    for compatibility, canonical in mappings.items():
        compat_path = os.path.join(repo_root, compatibility)
        canonical_path = os.path.join(repo_root, canonical)
        if not os.path.isfile(canonical_path):
            violations.append("missing canonical source: {}".format(canonical))
            continue
        if os.path.isfile(compat_path):
            if _sha(compat_path) != _sha(canonical_path):
                violations.append("generated compatibility copy drift: {}".format(compatibility))
            else:
                copies.append(compatibility)
    return {"status": "PASSED" if not violations else "FAILED",
            "canonical_sources": sorted(mappings.values()),
            "compatibility_copies": copies, "violations": violations,
            "is_valid": not violations}
