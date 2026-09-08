#!/usr/bin/env python3
"""Classify Path Mapping/VNext report gate applicability without fabricating identity."""
from __future__ import print_function

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.upgrade.release_gate_mode import classify_release_gate_mode


def _load(path):
    with open(path, "r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("input JSON must be an object")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    try:
        result = classify_release_gate_mode(_load(os.path.abspath(args.input)))
    except Exception as exc:
        print("ERROR {}".format(exc), file=sys.stderr)
        return 2
    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        with open(os.path.abspath(args.output), "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.write("\n")
    print(text)
    return 2 if result.get("blocked") else 0


if __name__ == "__main__":
    raise SystemExit(main())
