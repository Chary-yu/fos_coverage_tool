"""Build the repository-owned trusted Candidate artifact.

This lane intentionally does not accept a pre-populated Candidate directory.
It creates the Candidate from the exact clean checkout supplied by CI, then
normalizes and hashes the result.  The external GitHub artifact attestation
and protected receipt are added by the following signing step.
"""

from __future__ import print_function

import argparse
import json
import os
import shutil
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.candidate_artifact import (
    CandidateArtifactManifest, build_git_source_provenance,
)
from app.code_detail.code_region import FunctionRange
from app.code_detail.sidecar_store import SidecarStore
from app.code_detail.source_reader import (
    SourceContext, SourceLineDTO, calc_sidecar_file_key,
)
from app.release_identity import generate_release_identity, save_release_manifest
from app.release_publication import normalize_candidate_artifact


_SOURCE_ASSETS = (
    "web/assets/js/coverage_enhance.js",
    "web/assets/js/coverage_progress.js",
    "web/assets/js/incremental_coverage.js",
    "web/assets/js/incremental_developer_tasks.js",
    "web/assets/js/pending_snapshot.js",
    "web/assets/css/coverage_enhance.css",
)

# The trusted lane must produce the same kind of artifact that the real HTTP
# browser gate consumes.  A tiny HTML marker plus an empty Sidecar can prove
# hashes, but it cannot prove the VNext code-detail/report contract.  Keep the
# workload deterministic and large enough to exercise the production lazy
# code-region path.
CANDIDATE_PROJECT = "Coverage Candidate"
CANDIDATE_REPOSITORY = "coverage-candidate"
CANDIDATE_FILE_PATH = "src/coverage_candidate.c"
CANDIDATE_LINE_COUNT = 100000
CANDIDATE_SIDECAR_SCHEMA = 2
CANDIDATE_SIDECAR_CHUNK_SIZE = 2000


def _write(path, value):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(value)


def _write_json(path, value):
    _write(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _prepare_empty_root(path):
    path = os.path.abspath(path)
    if os.path.lexists(path):
        if os.path.islink(path) or not os.path.isdir(path):
            raise ValueError("candidate-root must be a directory created by the build lane")
        if os.listdir(path):
            raise ValueError(
                "candidate-root must be empty; pre-populated artifacts are not accepted"
            )
    else:
        os.makedirs(path)
    for directory in ("reports", "assets", "registry"):
        os.makedirs(os.path.join(path, directory))
    return path


def _ensure_parent(path):
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)


def _build_candidate_tree(source_root, candidate_root, identity,
                          line_count=CANDIDATE_LINE_COUNT):
    """Build a complete, publishable VNext report artifact.

    The builder owns the report, HTML shell, canonical browser assets and
    chunked Sidecar together.  It deliberately does not invent a DB snapshot:
    production Scan facts remain the responsibility of the deployment/import
    stage, while the immutable report bytes and their exact identity are
    produced here in one deterministic build.
    """
    line_count = int(line_count)
    if line_count < 1 or line_count > 1000000:
        raise ValueError("candidate line_count must be between 1 and 1000000")
    report_id = "coverage_candidate_{}".format(identity["commit_sha"][:12])
    asset_identity = "source-assets-{}".format(identity["asset_hash"][:16])
    assets_root = os.path.join(candidate_root, "assets")
    reports_root = os.path.join(candidate_root, "reports")
    for relative in _SOURCE_ASSETS:
        source_path = os.path.join(source_root, *relative.split("/"))
        if not os.path.isfile(source_path):
            raise ValueError("trusted Candidate build input is missing: {}".format(relative))
        asset_name = os.path.basename(relative)
        shutil.copyfile(
            source_path,
            os.path.join(assets_root, asset_name),
        )
    # Report pages are served below the report root in the standard staging
    # configuration.  Keep the two page assets beside the HTML as well as in
    # the release asset inventory so relative report URLs work when the
    # report-root directory is mounted directly by the web server.
    for relative in (
            "web/assets/js/coverage_enhance.js",
            "web/assets/css/coverage_enhance.css"):
        source_path = os.path.join(source_root, *relative.split("/"))
        shutil.copyfile(source_path, os.path.join(reports_root, os.path.basename(relative)))

    html = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LCOV - Coverage Candidate - {file_path}</title>
<meta name="coverage-project" content="{project}">
<meta name="coverage-report-mode" content="VNEXT_ARTIFACT_READY">
<meta name="coverage-report-id" content="{report_id}">
<meta name="coverage-scan-id" content="1">
<meta name="coverage-repository-name" content="{repository}">
<meta name="coverage-file-path" content="{file_path}">
<meta name="coverage-asset-identity" content="{asset_identity}">
<meta name="coverage-sidecar-schema" content="{sidecar_schema}">
<meta name="coverage-render-mode" content="lazy_collapse">
<meta name="coverage-review-scope" content="full">
<link rel="stylesheet" href="coverage_enhance.css">
</head><body>
<header><h1>Coverage Candidate</h1></header>
<pre class="source"></pre>
<script src="coverage_enhance.js"></script>
</body></html>
""".format(
        report_id=report_id,
        asset_identity=asset_identity,
        project=CANDIDATE_PROJECT,
        repository=CANDIDATE_REPOSITORY,
        file_path=CANDIDATE_FILE_PATH,
        sidecar_schema=CANDIDATE_SIDECAR_SCHEMA,
    )
    _write(os.path.join(reports_root, "coverage_candidate.gcov.html"), html)

    lines = [
        SourceLineDTO(
            line_number,
            source="int coverage_candidate_line_{:06d} = {};".format(
                line_number, line_number
            ),
            coverage_state="uncovered",
            analysis_state="未确认",
            is_pending_analysis=True,
            block_start_line=1,
            block_end_line=line_count,
            block_type="function",
            function_name="coverage_candidate_workload",
            is_block_entry=(line_number == 1),
        )
        for line_number in range(1, line_count + 1)
    ]
    context = SourceContext(
        CANDIDATE_PROJECT,
        CANDIDATE_FILE_PATH,
        lines,
        function_ranges=[FunctionRange(
            1, line_count, "coverage_candidate_workload"
        )],
        report_id=report_id,
    )
    file_key = calc_sidecar_file_key(CANDIDATE_FILE_PATH, CANDIDATE_REPOSITORY)
    SidecarStore(
        [reports_root],
        chunk_size=CANDIDATE_SIDECAR_CHUNK_SIZE,
        asset_identity=asset_identity,
    ).save_chunked_sidecar(reports_root, report_id, file_key, context)

    _write_json(
        os.path.join(candidate_root, "registry", report_id + ".json"),
        {
            "report_id": report_id,
            "report_mode": "VNEXT_ARTIFACT_READY",
            "scan_id": 1,
            "report_root": "reports",
            "sidecar_schema": CANDIDATE_SIDECAR_SCHEMA,
            "asset_identity": asset_identity,
            "repository_name": CANDIDATE_REPOSITORY,
            "repository_names": [CANDIDATE_REPOSITORY],
            "project_name": CANDIDATE_PROJECT,
        },
    )
    return {
        "report_id": report_id,
        "project_name": CANDIDATE_PROJECT,
        "repository_name": CANDIDATE_REPOSITORY,
        "file_path": CANDIDATE_FILE_PATH,
        "line_count": line_count,
        "sidecar_schema": CANDIDATE_SIDECAR_SCHEMA,
    }


def _load_args(argv=None):
    parser = argparse.ArgumentParser(prog="build_candidate_artifact.py")
    parser.add_argument("--source-repo-root", required=True)
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--release-identity-output", required=True)
    parser.add_argument("--build-workflow-identity", required=True)
    parser.add_argument("--build-workflow-run-id", required=True)
    parser.add_argument("--build-workflow-run-attempt", required=True)
    parser.add_argument("--build-workflow-sha", required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = _load_args(argv)
    source_root = os.path.realpath(os.path.abspath(args.source_repo_root))
    candidate_root = _prepare_empty_root(args.candidate_root)
    identity_output = os.path.abspath(args.release_identity_output)
    try:
        identity = generate_release_identity(
            source_root, build_provenance="release-build"
        )
        report = _build_candidate_tree(source_root, candidate_root, identity)
        normalize_candidate_artifact(candidate_root)
        provenance = build_git_source_provenance(
            source_root, identity, args.build_workflow_identity,
            build_workflow_run_id=args.build_workflow_run_id,
            build_workflow_run_attempt=args.build_workflow_run_attempt,
            build_workflow_sha=args.build_workflow_sha,
        )
        manifest = CandidateArtifactManifest.build(
            candidate_root, identity, source_provenance=provenance
        )
        _ensure_parent(identity_output)
        save_release_manifest(identity_output, identity)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise SystemExit("trusted Candidate build failed: {}".format(exc))
    print(json.dumps({
        "status": "PASSED",
        "candidate_root": candidate_root,
        "release_identity": identity_output,
        "candidate_artifact_manifest": os.path.join(
            candidate_root, "candidate_artifact_manifest.json"
        ),
        "candidate_build_attestation": os.path.join(
            candidate_root, "candidate_build_attestation.json"
        ),
        "receipt_required": True,
        "commit_sha": manifest["commit_sha"],
        "build_id": manifest["build_id"],
        "artifact_sha256": manifest["artifact_sha256"],
        "source_commit_sha": manifest["source_commit_sha"],
        "source_tree_sha": manifest["source_tree_sha"],
        "build_workflow_identity": manifest["build_workflow_identity"],
        "build_workflow_run_id": manifest["build_workflow_run_id"],
        "build_workflow_run_attempt": manifest["build_workflow_run_attempt"],
        "build_workflow_sha": manifest["build_workflow_sha"],
        "report_id": report["report_id"],
        "report_file_path": report["file_path"],
        "report_line_count": report["line_count"],
        "report_sidecar_schema": report["sidecar_schema"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
