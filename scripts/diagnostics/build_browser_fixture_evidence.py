"""Create exact-SHA CI evidence for the non-production browser fixture lane.

The production upgrade controller consumes this small envelope instead of
trying to install or execute Node/npm on the production host.  The command
that produced the envelope must already have completed successfully; this
helper only records the immutable CI identity and does not claim real
Candidate browser evidence.
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import platform
import socket
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.release_identity import is_valid_commit_sha
from app.time_utils import utc_iso


def build_evidence(revision, source_tree_sha, repository, workflow_run_id,
                   workflow_run_attempt, workflow_sha=None, command=None,
                   workflow_path=".github/workflows/ci.yml",
                   workflow_identity="browser-fixture-regression",
                   artifact_name=None):
    """Return a truthful, exact-revision CI fixture evidence envelope."""
    revision = str(revision or "").strip().lower()
    source_tree_sha = str(source_tree_sha or "").strip().lower()
    repository = str(repository or "").strip()
    workflow_run_id = str(workflow_run_id or "").strip()
    workflow_run_attempt = str(workflow_run_attempt or "").strip()
    workflow_sha = str(workflow_sha or revision).strip().lower()
    workflow_path = str(workflow_path or "").strip()
    workflow_identity = str(workflow_identity or "").strip()
    artifact_name = str(
        artifact_name or "browser-fixture-regression-{}".format(revision)
    ).strip()
    if not is_valid_commit_sha(revision):
        raise ValueError("revision must be an exact commit SHA")
    if not is_valid_commit_sha(source_tree_sha):
        raise ValueError("source_tree_sha must be an exact Git tree SHA")
    if not repository:
        raise ValueError("repository is required")
    if not workflow_run_id.isdigit() or int(workflow_run_id) <= 0:
        raise ValueError("workflow_run_id must be a positive numeric ID")
    if not workflow_run_attempt.isdigit() or int(workflow_run_attempt) <= 0:
        raise ValueError("workflow_run_attempt must be a positive numeric ID")
    if workflow_sha != revision:
        raise ValueError("workflow_sha must match revision")
    if not workflow_path:
        raise ValueError("workflow_path is required")
    if not workflow_identity:
        raise ValueError("workflow_identity is required")
    if not artifact_name:
        raise ValueError("artifact_name is required")
    now = utc_iso()
    return {
        "status": "PASSED",
        "evidence_class": "browser_fixture_regression",
        "synthetic": True,
        "local_execution": False,
        "evidence_origin": "github_actions",
        "ci_provider": "github_actions",
        "revision": revision,
        "candidate_revision": revision,
        "workflow_sha": workflow_sha,
        "workflow_path": workflow_path,
        "workflow_identity": workflow_identity,
        "source_tree_sha": source_tree_sha,
        "repository": repository,
        "artifact_name": artifact_name,
        "workflow_run_id": workflow_run_id,
        "workflow_run_attempt": workflow_run_attempt,
        "suite": "node_and_playwright_fixture",
        "command": command or (
            "node test_lazy_collapse_browser_smoke.js && "
            "npm run test:browser -- --reporter=line"
        ),
        "exit_code": 0,
        "name": "browser-fixture-regression",
        "host": socket.gethostname(),
        "environment": "github_actions",
        "runtime": platform.platform(),
        "started_at": now,
        "finished_at": now,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="write exact-SHA CI browser fixture evidence"
    )
    parser.add_argument("--revision", required=True)
    parser.add_argument("--source-tree-sha", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow-run-id", required=True)
    parser.add_argument("--workflow-run-attempt", required=True)
    parser.add_argument("--workflow-sha", default="")
    parser.add_argument(
        "--workflow-path", default=".github/workflows/ci.yml"
    )
    parser.add_argument(
        "--workflow-identity", default="browser-fixture-regression"
    )
    parser.add_argument("--artifact-name", default="")
    parser.add_argument("--command", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--digest-output", default="",
        help="write a detached SHA256 digest for the evidence artifact",
    )
    args = parser.parse_args(argv)
    evidence = build_evidence(
        args.revision,
        args.source_tree_sha,
        args.repository,
        args.workflow_run_id,
        args.workflow_run_attempt,
        workflow_sha=args.workflow_sha,
        command=args.command,
        workflow_path=args.workflow_path,
        workflow_identity=args.workflow_identity,
        artifact_name=args.artifact_name or None,
    )
    output = os.path.abspath(args.output)
    parent = os.path.dirname(output)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(output, "w", encoding="utf-8") as stream:
        json.dump(evidence, stream, indent=2, sort_keys=True)
        stream.write("\n")
    digest_output = os.path.abspath(args.digest_output) if args.digest_output else ""
    if digest_output:
        digest_parent = os.path.dirname(digest_output)
        if digest_parent and not os.path.isdir(digest_parent):
            os.makedirs(digest_parent)
        digest = hashlib.sha256()
        with open(output, "rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        with open(digest_output, "w", encoding="utf-8") as stream:
            stream.write("{}  {}\n".format(digest.hexdigest(), os.path.basename(output)))
    print(json.dumps({
        "status": evidence["status"],
        "revision": evidence["revision"],
        "source_tree_sha": evidence["source_tree_sha"],
        "output": output,
        "digest_output": digest_output,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    main()
