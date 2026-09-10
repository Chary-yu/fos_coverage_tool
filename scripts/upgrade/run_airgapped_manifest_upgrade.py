#!/usr/bin/env python3
"""Production entrypoint for the manifest-derived air-gapped R8 release path."""

from __future__ import print_function

import sys

from scripts.upgrade.airgapped_gateway_contract import (
    install_airgapped_gateway_contract,
)


def main():
    # Install before importing/delegating to the conductor's main execution so
    # its canonical UpgradeOrchestrator uses the origin-only preflight and the
    # manifest-derived browser/auth evidence validators for this process only.
    install_airgapped_gateway_contract()
    from scripts.upgrade.run_airgapped_upgrade import main as conductor_main
    return conductor_main()


if __name__ == "__main__":
    sys.exit(main())
