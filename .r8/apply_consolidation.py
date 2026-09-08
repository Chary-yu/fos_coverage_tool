#!/usr/bin/env python3
from __future__ import print_function

import argparse
import ast
import base64
import glob
import hashlib
import json
import os
import subprocess
import sys
import zlib

BASE_SHA = "c0b88ceb05bbe3047812b0b1b35320ac6a6d1be0"


def blob_sha(data):
    return hashlib.sha1(("blob %d\0" % len(data)).encode("ascii") + data).hexdigest()


def load_payload():
    paths = sorted(glob.glob(".r8/payload_*.txt"))
    if len(paths) != 6:
        raise RuntimeError("expected 6 R8 payload chunks, found %d" % len(paths))
    encoded = "".join(open(path, "r", encoding="ascii").read().strip() for path in paths)
    raw = zlib.decompress(base64.b64decode(encoded)).decode("utf-8")
    return json.loads(raw)


def apply_transform(path, spec):
    data = open(path, "rb").read()
    actual = blob_sha(data)
    if actual != spec["expected_blob"]:
        raise RuntimeError("%s base blob mismatch: %s != %s" % (
            path, actual, spec["expected_blob"]))
    lines = data.decode("utf-8").splitlines(True)
    for op in sorted(spec["ops"], key=lambda item: item["i1"], reverse=True):
        lines[op["i1"]:op["i2"]] = op["new"].splitlines(True)
    with open(path, "wb") as stream:
        stream.write("".join(lines).encode("utf-8"))


def write_new(path, content):
    if os.path.lexists(path):
        raise RuntimeError("new R8 file already exists: %s" % path)
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w", encoding="utf-8", newline="") as stream:
        stream.write(content)


def py36_grammar(paths):
    failed = []
    for path in paths:
        if not path.endswith(".py"):
            continue
        try:
            with open(path, "r", encoding="utf-8") as stream:
                source = stream.read()
            ast.parse(source, filename=path, feature_version=(3, 6))
        except Exception as exc:
            failed.append((path, repr(exc)))
    if failed:
        raise RuntimeError("Python 3.6 grammar failures: %r" % (failed,))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply:
        parser.error("--apply is required")
    root = subprocess.check_output(["git", "rev-parse", "--show-toplevel"]).decode().strip()
    os.chdir(root)
    merge_base = subprocess.check_output(["git", "merge-base", "HEAD", BASE_SHA]).decode().strip()
    if merge_base != BASE_SHA:
        raise RuntimeError("R8 branch does not descend from exact c0b88ceb base")
    payload = load_payload()
    transforms = payload["transforms"]
    new_files = payload["new_files"]
    for path in sorted(transforms):
        apply_transform(path, transforms[path])
    for path in sorted(new_files):
        write_new(path, new_files[path])
    changed = list(transforms) + list(new_files)
    py36_grammar(changed)
    print(json.dumps({
        "status": "PASSED",
        "base_sha": BASE_SHA,
        "transformed_files": len(transforms),
        "new_files": len(new_files),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
