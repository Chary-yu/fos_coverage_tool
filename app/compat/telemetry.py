"""Opt-in usage counter for compatibility surfaces.

The default is deliberately side-effect free.  Deployments that want to
measure the deprecation window set ``COVERAGE_LEGACY_USAGE_FILE`` to a path
owned by the service account; each adapter invocation then records a bounded
JSON counter that can be included in release evidence.
"""

from __future__ import absolute_import

import json
import os
import tempfile


def record(surface):
    path = str(os.environ.get("COVERAGE_LEGACY_USAGE_FILE") or "").strip()
    if not path:
        return
    directory = os.path.dirname(os.path.realpath(path)) or "."
    try:
        if not os.path.isdir(directory):
            os.makedirs(directory)
        data = {}
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as stream:
                loaded = json.load(stream)
                if isinstance(loaded, dict):
                    data = loaded
        key = str(surface or "unknown")
        data[key] = int(data.get(key) or 0) + 1
        temporary = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, delete=False,
        )
        try:
            json.dump(data, temporary, sort_keys=True)
            temporary.flush()
            os.fsync(temporary.fileno())
        finally:
            temporary.close()
        os.replace(temporary.name, path)
    except (OSError, ValueError, TypeError):
        # Compatibility telemetry must never break the old public entrypoint.
        return
