"""Build-time release identity generator.

Runtime verification intentionally does not call this module or rewrite a
manifest.  CI/release tooling must invoke it and publish the resulting file.
"""

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.release_identity import (
    generate_release_identity, is_valid_commit_sha, save_release_manifest,
)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="build_release.py")
    parser.add_argument("--output", required=True)
    parser.add_argument("--repo-root", default=ROOT)
    parser.add_argument(
        "--commit-sha", default="",
        help="build provenance SHA when packaging from a tree without .git",
    )
    args = parser.parse_args(argv)
    repo_root = os.path.abspath(args.repo_root)
    if not os.path.exists(os.path.join(repo_root, ".git")) and not args.commit_sha:
        parser.error("--commit-sha is required when --repo-root has no .git metadata")
    if args.commit_sha and not is_valid_commit_sha(args.commit_sha):
        parser.error("--commit-sha must be a concrete 40-character Git SHA")
    identity = generate_release_identity(
        repo_root,
        commit_sha=args.commit_sha or None,
        build_provenance="release-build",
    )
    save_release_manifest(os.path.abspath(args.output), identity)
    print(identity["build_id"])


if __name__ == "__main__":
    main()
