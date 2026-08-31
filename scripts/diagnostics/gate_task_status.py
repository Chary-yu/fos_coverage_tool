"""Build a conservative status report for every frozen Gate A--F task.

The task manifest is an inventory contract, not evidence that work passed.  A
task is marked ``PASSED`` only when its gate and every upstream task are
``PASSED`` in the same exact-SHA gate matrix.  Missing external evidence is
therefore visible on each affected task instead of being hidden behind one
coarse gate-level result.
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
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.release_identity import generate_release_identity
from app.time_utils import utc_iso
from scripts.diagnostics.contract import with_contract
from scripts.diagnostics.task_manifest_audit import audit as audit_task_manifest


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _revision(repo_root):
    try:
        return subprocess.check_output(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode("ascii").strip()
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return ""


def _load_json(path):
    with open(path, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def _gate_blockers(gate_payload):
    blockers = []
    gate_payload = gate_payload if isinstance(gate_payload, dict) else {}
    for collection_name in ("local_checks", "external_evidence"):
        for check in gate_payload.get(collection_name) or []:
            if not isinstance(check, dict):
                blockers.append({
                    "name": "<invalid-check>",
                    "status": "FAILED",
                    "violations": ["gate check must be an object"],
                })
                continue
            status = str(check.get("status") or "INCOMPLETE").upper()
            if status == "PASSED":
                continue
            blockers.append({
                "name": check.get("name") or "<unnamed-check>",
                "status": status,
                "evidence_class": check.get("evidence_class", ""),
                "requirement": check.get("requirement", ""),
                "violations": check.get("violations") or [],
            })
    return blockers


def _status(*values):
    normalized = [str(value or "INCOMPLETE").upper() for value in values]
    if "FAILED" in normalized:
        return "FAILED"
    if "BLOCKED" in normalized:
        return "BLOCKED"
    if any(value != "PASSED" for value in normalized):
        return "INCOMPLETE"
    return "PASSED"


def _resolve_task_status(task_id, task_by_id, gate_by_id, cache, visiting):
    if task_id in cache:
        return cache[task_id]
    if task_id in visiting:
        result = {
            "status": "FAILED",
            "gate_status": "FAILED",
            "dependency_statuses": {},
            "blockers": [{
                "name": "task_manifest_dependencies",
                "status": "FAILED",
                "violations": ["dependency cycle detected at {}".format(task_id)],
            }],
        }
        cache[task_id] = result
        return result

    task = task_by_id.get(task_id) or {}
    gate = str(task_id).split("-", 1)[0]
    gate_payload = gate_by_id.get(gate) or {}
    gate_status = str(gate_payload.get("status") or "INCOMPLETE").upper()
    visiting = set(visiting)
    visiting.add(task_id)
    dependency_statuses = {}
    dependency_results = []
    for dependency in task.get("upstream_gate_dependencies") or []:
        dependency_result = _resolve_task_status(
            dependency, task_by_id, gate_by_id, cache, visiting
        )
        dependency_statuses[dependency] = dependency_result.get("status")
        dependency_results.append(dependency_result.get("status"))

    blockers = _gate_blockers(gate_payload)
    if gate_status in ("FAILED", "BLOCKED"):
        task_status = gate_status
    elif gate_status != "PASSED":
        task_status = "INCOMPLETE"
    elif blockers:
        task_status = "FAILED" if any(
            str(item.get("status") or "").upper() == "FAILED"
            for item in blockers
        ) else "INCOMPLETE"
    elif any(value != "PASSED" for value in dependency_results):
        task_status = "BLOCKED"
        blockers.append({
            "name": "upstream_gate_dependencies",
            "status": "BLOCKED",
            "violations": [
                "upstream task {} is not PASSED".format(dependency)
                for dependency, value in dependency_statuses.items()
                if value != "PASSED"
            ],
        })
    else:
        task_status = "PASSED"

    result = {
        "status": task_status,
        "gate_status": gate_status,
        "dependency_statuses": dependency_statuses,
        "blockers": blockers,
    }
    cache[task_id] = result
    return result


def build_from_matrix(repo_root, matrix, matrix_path=""):
    repo_root = os.path.abspath(repo_root)
    manifest_result = audit_task_manifest(repo_root)
    violations = list(manifest_result.get("violations") or [])
    manifest_path = os.path.join(repo_root, "docs", "gate_task_manifest.json")
    try:
        manifest = _load_json(manifest_path)
    except (OSError, ValueError, TypeError) as exc:
        manifest = {"tasks": []}
        violations.append("cannot load task manifest: {}".format(exc))

    matrix = matrix if isinstance(matrix, dict) else {}
    candidate_revision = str(matrix.get("candidate_revision") or "")
    current_revision = _revision(repo_root)
    if not candidate_revision:
        violations.append("gate matrix candidate_revision is missing")
    elif current_revision and candidate_revision != current_revision:
        violations.append(
            "gate matrix candidate_revision does not match HEAD: {} != {}".format(
                candidate_revision, current_revision
            )
        )

    tasks = [item for item in (manifest.get("tasks") or []) if isinstance(item, dict)]
    task_by_id = {item.get("task_id"): item for item in tasks if item.get("task_id")}
    gate_by_id = matrix.get("gates") or {}
    cache = {}
    task_results = []
    for task in tasks:
        task_id = task.get("task_id")
        resolved = _resolve_task_status(
            task_id, task_by_id, gate_by_id, cache, set()
        )
        task_results.append({
            "task_id": task_id,
            "gate": str(task_id).split("-", 1)[0],
            "status": resolved["status"],
            "gate_status": resolved["gate_status"],
            "root_owner_skill": task.get("root_owner_skill"),
            "secondary_owner_skill": task.get("secondary_owner_skill"),
            "required_evidence_class": task.get("required_evidence_class"),
            "upstream_gate_dependencies": task.get("upstream_gate_dependencies") or [],
            "dependency_statuses": resolved["dependency_statuses"],
            "blockers": resolved["blockers"],
        })

    by_gate = {}
    for item in task_results:
        gate = item["gate"]
        summary = by_gate.setdefault(gate, {
            "PASSED": 0, "INCOMPLETE": 0, "BLOCKED": 0, "FAILED": 0,
        })
        summary[item["status"]] = summary.get(item["status"], 0) + 1
    task_status = _status(*(item["status"] for item in task_results))
    if violations and task_status == "PASSED":
        task_status = "FAILED"

    result = with_contract({
        "status": task_status,
        "evidence_class": "gate_task_status",
        "synthetic": False,
        "candidate_revision": candidate_revision,
        "current_revision": current_revision,
        "release_identity": matrix.get("release_identity") or
            generate_release_identity(repo_root=repo_root),
        "host_identity": {
            "hostname": platform.node(),
            "platform": platform.platform(),
        },
        "generated_at": utc_iso(),
        "manifest_path": os.path.relpath(manifest_path, repo_root),
        "manifest_sha256": _sha256(manifest_path) if os.path.isfile(manifest_path) else "",
        "matrix_path": os.path.abspath(matrix_path) if matrix_path else "",
        "matrix_sha256": _sha256(matrix_path) if matrix_path and os.path.isfile(matrix_path) else "",
        "task_count": len(task_results),
        "summary_by_gate": by_gate,
        "tasks": task_results,
        "violations": violations,
    })
    return result


def build(repo_root=ROOT, matrix=None, matrix_path=""):
    if matrix is None:
        from scripts.diagnostics.gate_matrix import build as build_gate_matrix
        matrix = build_gate_matrix(repo_root)
    return build_from_matrix(repo_root, matrix, matrix_path=matrix_path)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=ROOT)
    parser.add_argument("--matrix", help="existing exact-SHA gate-matrix.json")
    parser.add_argument("--output", default=".artifacts/vnext/gate-task-status.json")
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args(argv)
    repo_root = os.path.abspath(args.repo_root)
    matrix_path = os.path.abspath(args.matrix) if args.matrix else ""
    try:
        matrix = _load_json(matrix_path) if matrix_path else None
        result = build(repo_root, matrix=matrix, matrix_path=matrix_path)
    except (OSError, ValueError, TypeError) as exc:
        result = with_contract({
            "status": "FAILED",
            "evidence_class": "gate_task_status",
            "synthetic": False,
            "candidate_revision": _revision(repo_root),
            "violations": ["cannot load gate matrix: {}".format(exc)],
        })
    output = args.output if os.path.isabs(args.output) else os.path.join(repo_root, args.output)
    directory = os.path.dirname(output)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(output, "w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASSED" or (
        result["status"] == "INCOMPLETE" and args.allow_incomplete
    ) else 1


if __name__ == "__main__":
    sys.exit(main())
