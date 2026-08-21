"""Evaluate every Definition-of-Done item against exact-SHA task evidence.

The Gate A-F task manifest describes implementation work.  This manifest is
the separate completion contract from section 19 of the plan.  A DoD item is
never inferred from a source search: every required task must be PASSED in the
same candidate revision, otherwise the item remains INCOMPLETE/BLOCKED.
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
MANIFEST_RELATIVE_PATH = os.path.join("docs", "gate_dod_manifest.json")
EXPECTED_DOD_IDS = ["DOD-{:02d}".format(index) for index in range(1, 25)]

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.diagnostics.contract import with_contract


def _revision(repo_root):
    try:
        return subprocess.check_output(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
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


def _load(path):
    with open(path, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def _status(values):
    values = [str(value or "INCOMPLETE").upper() for value in values]
    if "FAILED" in values:
        return "FAILED"
    if "BLOCKED" in values:
        return "BLOCKED"
    if any(value != "PASSED" for value in values):
        return "INCOMPLETE"
    return "PASSED"


def build(repo_root=ROOT, matrix=None, task_status=None,
          manifest_path="", task_status_path="", matrix_path=""):
    repo_root = os.path.abspath(repo_root)
    manifest_path = manifest_path or os.path.join(repo_root, MANIFEST_RELATIVE_PATH)
    violations = []
    try:
        manifest = _load(manifest_path)
    except (OSError, ValueError, TypeError) as exc:
        manifest = {"items": []}
        violations.append("cannot load DoD manifest: {}".format(exc))

    items = manifest.get("items") if isinstance(manifest.get("items"), list) else []
    expected_ids = EXPECTED_DOD_IDS
    observed_ids = [item.get("dod_id") for item in items if isinstance(item, dict)]
    if observed_ids != expected_ids:
        violations.append("DoD manifest must contain DOD-01..DOD-24 in order")
    if not isinstance(matrix, dict):
        violations.append("Gate Matrix is missing")
        matrix = {}
    if not isinstance(task_status, dict):
        violations.append("Gate task status is missing")
        task_status = {}

    candidate_revision = str(matrix.get("candidate_revision") or "")
    current_revision = _revision(repo_root)
    if not candidate_revision:
        violations.append("Gate Matrix candidate_revision is missing")
    if current_revision and candidate_revision and current_revision != candidate_revision:
        violations.append("Gate Matrix candidate_revision does not match HEAD")
    task_revision = str(task_status.get("candidate_revision") or "")
    if task_revision != candidate_revision:
        violations.append("task status candidate_revision does not match Gate Matrix")

    tasks = {
        item.get("task_id"): item
        for item in (task_status.get("tasks") or [])
        if isinstance(item, dict) and item.get("task_id")
    }
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        dod_id = str(item.get("dod_id") or "")
        required_tasks = [str(task_id) for task_id in item.get("required_tasks") or []]
        missing_tasks = [task_id for task_id in required_tasks if task_id not in tasks]
        task_results = []
        blockers = []
        if missing_tasks:
            blockers.append({
                "name": "required_tasks",
                "status": "INCOMPLETE",
                "violations": ["missing task evidence: {}".format(
                    ", ".join(missing_tasks)
                )],
            })
        for task_id in required_tasks:
            task = tasks.get(task_id)
            status = str((task or {}).get("status") or "INCOMPLETE").upper()
            task_results.append({"task_id": task_id, "status": status})
            if status != "PASSED":
                blockers.append({
                    "name": "task:{}".format(task_id),
                    "status": status,
                    "violations": (task or {}).get("blockers") or [],
                })
        result_status = _status([item.get("status") for item in task_results])
        if missing_tasks:
            result_status = "INCOMPLETE"
        results.append({
            "dod_id": dod_id,
            "title": item.get("title") or "",
            "required_tasks": required_tasks,
            "required_evidence_class": item.get("evidence_class") or "",
            "status": result_status,
            "task_results": task_results,
            "blockers": blockers,
        })

    if len(results) != 24:
        violations.append("DoD result count must be 24")
    overall = _status([item.get("status") for item in results])
    if violations and overall == "PASSED":
        overall = "FAILED"
    return with_contract({
        "schema_version": 1,
        "status": overall,
        "evidence_class": "gate_dod_status",
        "synthetic": False,
        "candidate_revision": candidate_revision,
        "current_revision": current_revision,
        "host_identity": {
            "hostname": platform.node(), "platform": platform.platform(),
        },
        "manifest_path": os.path.relpath(manifest_path, repo_root),
        "manifest_sha256": _sha256(manifest_path) if os.path.isfile(manifest_path) else "",
        "matrix_path": os.path.abspath(matrix_path) if matrix_path else "",
        "task_status_path": os.path.abspath(task_status_path) if task_status_path else "",
        "dod_count": len(results),
        "items": results,
        "violations": violations,
    })


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=ROOT)
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--task-status", required=True)
    parser.add_argument("--manifest", default=os.path.join(ROOT, MANIFEST_RELATIVE_PATH))
    parser.add_argument("--output", default=".artifacts/vnext/gate-dod-status.json")
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args(argv)
    repo_root = os.path.abspath(args.repo_root)
    try:
        result = build(
            repo_root, matrix=_load(args.matrix), task_status=_load(args.task_status),
            manifest_path=args.manifest, task_status_path=args.task_status,
            matrix_path=args.matrix,
        )
    except (OSError, ValueError, TypeError) as exc:
        result = with_contract({
            "schema_version": 1, "status": "FAILED",
            "evidence_class": "gate_dod_status", "synthetic": False,
            "candidate_revision": _revision(repo_root),
            "items": [], "dod_count": 0,
            "violations": ["cannot load DoD inputs: {}".format(exc)],
        })
    output = args.output if os.path.isabs(args.output) else os.path.join(repo_root, args.output)
    directory = os.path.dirname(os.path.abspath(output))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(output, "w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result["status"] == "PASSED":
        return 0
    return 0 if args.allow_incomplete and result["status"] == "INCOMPLETE" else 1


if __name__ == "__main__":
    sys.exit(main())
