"""Gate D/F parser toolchain preflight; never treats the builtin parser as production proof."""

from __future__ import absolute_import

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.inheritance.toolchain import parser_toolchain_preflight


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", default="")
    parser.add_argument("--adapter", default="builtin-conservative")
    parser.add_argument("--output", default="")
    parser.add_argument("--require-external", action="store_true")
    args = parser.parse_args(argv)
    result = parser_toolchain_preflight(
        command=args.command or None,
        adapter_name=args.adapter,
        require_external=args.require_external,
    )
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = os.path.abspath(args.output)
        directory = os.path.dirname(output)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(output, "w", encoding="utf-8") as stream:
            stream.write(encoded)
    print(encoded)
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
