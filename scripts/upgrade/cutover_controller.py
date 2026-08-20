"""Explicit-file cutover and recoverable rollback primitives."""

import hashlib
import os
import shutil
import tempfile


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class CutoverController:
    def __init__(self, repo_root, backup_root):
        self.repo_root = os.path.realpath(repo_root)
        self.backup_root = os.path.realpath(backup_root)
        os.makedirs(self.backup_root, exist_ok=True)

    def _inside(self, path, root=None):
        root = os.path.realpath(root or self.repo_root)
        target = os.path.realpath(path)
        return os.path.commonpath((root, target)) == root

    def apply(self, actions):
        applied = []
        for action in actions:
            if action.get("op") not in ("ADD", "MODIFY"):
                raise RuntimeError("only ADD/MODIFY are allowed by this safe cutover primitive")
            source = os.path.join(self.repo_root, action["source"])
            destination = os.path.join(self.repo_root, action.get("destination", action["source"]))
            if not self._inside(source) or not self._inside(destination) or not os.path.isfile(source):
                raise RuntimeError("cutover path is outside safe root or missing")
            if action.get("source_sha256") != _sha256(source):
                raise RuntimeError("cutover source hash mismatch: {}".format(action["source"]))
            same_file = os.path.exists(destination) and os.path.samefile(source, destination)
            if os.path.isfile(destination) and not same_file:
                backup = os.path.join(self.backup_root, action.get("destination", action["source"]))
                if not self._inside(backup, self.backup_root):
                    raise RuntimeError("backup path escapes safe root")
                os.makedirs(os.path.dirname(backup), exist_ok=True)
                shutil.copy2(destination, backup)
            if not same_file:
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                shutil.copy2(source, destination)
            applied.append(destination)
        return applied

    def rollback(self):
        for dirpath, _, filenames in os.walk(self.backup_root):
            for name in filenames:
                backup = os.path.join(dirpath, name)
                rel = os.path.relpath(backup, self.backup_root)
                destination = os.path.join(self.repo_root, rel)
                if not self._inside(destination):
                    raise RuntimeError("rollback destination escapes repository root")
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                shutil.copy2(backup, destination)
