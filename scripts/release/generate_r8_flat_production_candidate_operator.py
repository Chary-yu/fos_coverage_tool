#!/usr/bin/env python3
"""Compatibility wrapper for the retired version-specific operator name.

New release preparation must use
``generate_flat_production_candidate_operator.py`` and an immutable release
input manifest.  This wrapper intentionally contains no version-specific
source, baseline, host, or tooling identities.
"""

from __future__ import print_function

from scripts.release.generate_flat_production_candidate_operator import generate


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="generate_r8_flat_production_candidate_operator.py",
        description="Compatibility entry point; inputs are manifest-bound.",
    )
    parser.add_argument("--release-input-manifest", required=True)
    parser.add_argument("--source-bundle", required=True)
    parser.add_argument("--tooling-bundle", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = generate(
            args.release_input_manifest, args.source_bundle,
            args.tooling_bundle, args.output,
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print("OPERATOR_GENERATION=PASS")
    for key in sorted(result):
        print("{}={}".format(key.upper(), result[key]))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
