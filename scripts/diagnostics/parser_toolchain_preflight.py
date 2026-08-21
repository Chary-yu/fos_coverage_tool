"""Gate D/F parser toolchain preflight; never treats the builtin parser as production proof."""

from __future__ import absolute_import

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.config.runtime_config import load_application_config
from app.inheritance.toolchain import (
    parser_toolchain_preflight, parser_toolchain_preflight_from_config,
)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", default="")
    parser.add_argument("--adapter", default="builtin-conservative")
    parser.add_argument(
        "--config", default="",
        help="runtime config whose inheritance_parser selection must be checked",
    )
    parser.add_argument("--output", default="")
    parser.add_argument("--require-external", action="store_true")
    args = parser.parse_args(argv)
    if args.config:
        config_path = args.config
        if not os.path.isabs(config_path):
            config_path = os.path.join(ROOT, config_path)
        config = load_application_config(config_path, base_dir=ROOT)
        result = parser_toolchain_preflight_from_config(
            config, require_external=args.require_external
        )
        result["runtime_config_path"] = os.path.realpath(config_path)
    else:
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
