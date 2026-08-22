"""Small exact-release fixture for runtime composition tests."""

import os

from app.release_identity import generate_release_identity, save_release_manifest


def prepare_release_root(root):
    identity = generate_release_identity(root)
    save_release_manifest(os.path.join(root, "release_manifest.json"), identity)
    return identity
