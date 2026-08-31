"""Diagnostic for project-name normalization collisions.

Project lookup and persistence remain exact-name/project-id operations.  The
normalization below is intentionally diagnostic-only: it must never be used
to resolve a request or merge two projects.
"""

from __future__ import print_function

import argparse
import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.time_utils import utc_iso


def diagnostic_key(project_name):
    """Return a human-review key; never use this value for project lookup."""
    value = str(project_name or "").strip().casefold()
    return re.sub(r"[-_]", "", value)


def find_collisions(project_names):
    groups = {}
    for name in project_names or ():
        exact = str(name)
        if not exact.strip():
            continue
        groups.setdefault(diagnostic_key(exact), set()).add(exact)
    collisions = []
    for key, exact_names in sorted(groups.items()):
        if len(exact_names) > 1:
            collisions.append({
                "diagnostic_key": key,
                "exact_project_names": sorted(exact_names),
                "matching_policy": "project_id_or_exact_name_only",
                "action": "manual_review_only_no_auto_merge",
            })
    return collisions


def audit(project_names):
    collisions = find_collisions(project_names)
    return {
        "status": "PASSED" if not collisions else "INCOMPLETE",
        "evidence_class": "project_identity_collision_diagnostic",
        "checked_at": utc_iso(),
        "project_count": len(set(str(name) for name in (project_names or ())
                                  if str(name).strip())),
        "matching_policy": "project_id_or_exact_name_only",
        "normalization_is_diagnostic_only": True,
        "collisions": collisions,
        "violations": [
            "normalization collision requires manual review: {}".format(
                ", ".join(item["exact_project_names"])
            ) for item in collisions
        ],
    }


def _read_names(path):
    with open(path, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    if isinstance(value, dict):
        value = value.get("projects") or value.get("project_names") or []
    if not isinstance(value, list):
        raise ValueError("identity input must be a JSON list or projects object")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", action="append", default=[])
    parser.add_argument("--input", default="")
    args = parser.parse_args(argv)
    names = list(args.name)
    if args.input:
        names.extend(_read_names(args.input))
    result = audit(names)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())

