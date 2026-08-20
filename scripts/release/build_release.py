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

from app.release_identity import generate_release_identity, save_release_manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    identity = generate_release_identity(ROOT)
    save_release_manifest(os.path.abspath(args.output), identity)
    print(identity["build_id"])


if __name__ == "__main__":
    main()
