"""Generate an explicit deployment manifest from a release file list."""

import argparse
import hashlib
import json
import os


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build(repo_root, files, output):
    actions = []
    for rel in files:
        rel = os.path.normpath(rel)
        source = os.path.abspath(os.path.join(repo_root, rel))
        root = os.path.abspath(repo_root)
        if os.path.commonpath((root, source)) != root or not os.path.isfile(source):
            raise ValueError("manifest source is outside repository or missing: {}".format(rel))
        actions.append({"op": "ADD", "source": rel.replace(os.sep, "/"),
                       "source_sha256": sha256(source), "destination": rel.replace(os.sep, "/"),
                       "backup_required": True})
    with open(output, "w", encoding="utf-8") as stream:
        json.dump({"schema_version": 1, "actions": actions}, stream, indent=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--files", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    build(args.repo_root, args.files, args.output)
