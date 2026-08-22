"""Small exact-release fixture for runtime composition tests."""

import os

from app.release_identity import generate_release_identity, save_release_manifest


def prepare_release_root(root):
    # The temporary runtime root intentionally has no .git directory.  Pin it
    # to the exact checkout revision while hashing only the assets present in
    # that artifact root, just like a stripped release bundle.
    source_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    source_identity = generate_release_identity(source_root)
    identity = generate_release_identity(
        root, asset_files=[], commit_sha=source_identity["commit_sha"],
        build_provenance="test-artifact",
    )
    save_release_manifest(os.path.join(root, "release_manifest.json"), identity)
    return identity
