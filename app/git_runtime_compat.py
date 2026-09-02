"""Install a process-local Git CLI adapter for legacy production clients.

The vfoswind production Git rejects the global ``git -C <repo>`` option.
Rather than duplicating compatibility branches throughout incremental,
inheritance, and release diagnostics, put a tiny executable ``git`` adapter at
the front of PATH.  The adapter translates only a leading ``-C <repo>`` into
an actual working-directory change, then execs the real Git captured before
PATH is changed.  All other Git arguments are passed through byte-for-byte.

This module changes only child-process command resolution.  It does not mutate
Git configuration and does not change any authoritative application state.
"""
from __future__ import absolute_import

import os
import shutil


_REAL_GIT_ENV = "FOS_REAL_GIT"
_INSTALLED_ENV = "FOS_GIT_COMPAT_INSTALLED"


def _real_git_outside(shim_dir):
    """Resolve Git without accidentally selecting an already-prepended shim."""
    current = os.environ.get("PATH", "")
    real_shim_dir = os.path.realpath(shim_dir)
    entries = [
        item for item in current.split(os.pathsep)
        if item and os.path.realpath(item) != real_shim_dir
    ]
    return shutil.which("git", path=os.pathsep.join(entries)), entries


def install():
    """Prepend the repository Git adapter exactly once for this process."""
    if os.environ.get(_INSTALLED_ENV) == "1":
        return False
    package_root = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
    shim_dir = os.path.join(package_root, "scripts", "compat")
    shim = os.path.join(shim_dir, "git")
    if not os.path.isfile(shim):
        return False
    real_git, entries = _real_git_outside(shim_dir)
    if not real_git:
        return False
    os.environ[_REAL_GIT_ENV] = os.path.realpath(real_git)
    os.environ["PATH"] = os.pathsep.join([shim_dir] + entries)
    os.environ[_INSTALLED_ENV] = "1"
    return True
