"""Capture the authoritative immutable production ``CURRENT`` binding.

This observation-only command is intended to run on the protected production
builder before a Candidate is assembled.  It resolves ``publish_root/CURRENT``
itself and emits the exact values that the Candidate builder must bind to.
"""

from __future__ import print_function

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.release_publication import current_served_root_binding


def capture(publish_root):
    binding = current_served_root_binding(publish_root)
    return {
        "status": "PASSED",
        "evidence_class": "production-current-served-root-binding",
        "publish_root": binding["publish_root"],
        "current_path": binding["requested_path"],
        "current_realpath": binding["realpath"],
        "previous_release_commit_sha": binding["previous_release_commit_sha"],
        "served_root_tree_sha256": binding["served_root_tree_sha256"],
        "served_root_identity_sha256": binding["served_root_identity_sha256"],
        "served_root_identity_file_sha256": binding[
            "served_root_identity_file_sha256"
        ],
        "release_validation_session_id": binding[
            "release_validation_session_id"
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="production_current_binding.py")
    parser.add_argument("--publish-root", required=True)
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    try:
        result = capture(args.publish_root)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        parser.error(str(exc))
        return 2
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = os.path.abspath(args.output)
        directory = os.path.dirname(output)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(output, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.write("\n")
    print(encoded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
