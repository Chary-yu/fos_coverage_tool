#!/usr/bin/env python3
"""Production entrypoint for the manifest-derived air-gapped R8 release path.

This wrapper keeps the operator-facing config compatible with older examples
that may still contain a concrete Candidate HTML placeholder.  Before the
canonical conductor starts, it writes a private process-local config copy in
which both Candidate browser URL fields are reduced to the configured Gateway
origin.  The real report URL is then discovered from the immutable Candidate
release manifest and supplied by browser/auth evidence at runtime.
"""

from __future__ import print_function

import json
import os
import sys
import tempfile

from scripts.upgrade.airgapped_gateway_contract import (
    _origin,
    install_airgapped_gateway_contract,
)


def _config_argument(argv):
    for index, item in enumerate(argv):
        if item == "--config" and index + 1 < len(argv):
            return index + 1, argv[index + 1]
    raise RuntimeError("--config is required")


def _normalized_private_config(source_path):
    with open(source_path, "r", encoding="utf-8") as stream:
        config = json.load(stream)
    if not isinstance(config, dict):
        raise RuntimeError("upgrade config must be a JSON object")
    upgrade = config.get("upgrade") or {}
    if not isinstance(upgrade, dict):
        raise RuntimeError("upgrade config section must be a JSON object")
    profile = upgrade.get("airgapped_operator_browser") or {}
    if not isinstance(profile, dict) or profile.get("enabled") is not True:
        raise RuntimeError("airgapped_operator_browser.enabled=true is required")
    origin = _origin(
        profile.get("candidate_gateway_origin") or
        upgrade.get("candidate_gateway_origin") or ""
    )

    # The canonical controller historically consumes candidate_browser_url.
    # In this air-gapped process that field is intentionally an origin-only
    # contract; the installed adapter validates the later manifest-derived
    # concrete HTML URL without weakening any identity or evidence gate.
    upgrade["candidate_gateway_origin"] = origin
    upgrade["candidate_browser_url"] = origin
    profile["candidate_gateway_origin"] = origin
    upgrade["airgapped_operator_browser"] = profile

    integration = upgrade.get("production_integration") or {}
    if not isinstance(integration, dict):
        raise RuntimeError("production_integration must be a JSON object")
    gateway = integration.get("candidate_gateway") or {}
    if not isinstance(gateway, dict):
        raise RuntimeError("candidate_gateway must be a JSON object")
    gateway["origin"] = origin
    gateway["browser_url"] = origin
    integration["candidate_gateway"] = gateway
    upgrade["production_integration"] = integration
    config["upgrade"] = upgrade

    fd, temporary = tempfile.mkstemp(prefix="fos-r8-airgapped-config-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass
        return temporary
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def main():
    config_index = None
    temporary = None
    try:
        config_index, source_config = _config_argument(sys.argv)
        temporary = _normalized_private_config(source_config)
        sys.argv[config_index] = temporary
        install_airgapped_gateway_contract()
        from scripts.upgrade.run_airgapped_upgrade import main as conductor_main
        return conductor_main()
    finally:
        if temporary:
            try:
                os.remove(temporary)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
