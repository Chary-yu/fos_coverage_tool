"""
Production Evidence Manifest Module (Item 28)
Governs and records end-to-end evidence required for production cutover:
- Release identity, commit SHAs, and asset hashes
- Targeted test suite execution results
- Pre/Post data content hashes and reconciliation
- Schema preflight and migration verification
- Performance A/B benchmark evidence
- Path mapping and security audit evidence
- Final Production Gate verification and UPGRADE_SUCCESS certification
"""

import os
import sys
import json
from typing import Dict, Any, List, Optional, Tuple

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.release_identity import get_current_release_identity

EVIDENCE_MANIFEST_FILE = "production_evidence_manifest.json"

class ProductionEvidenceManifest:
    def __init__(self, repo_root: Optional[str] = None):
        self.repo_root = repo_root or _REPO_ROOT
        self.manifest_path = os.path.join(self.repo_root, EVIDENCE_MANIFEST_FILE)
        self.data: Dict[str, Any] = self._load_or_init()

    def _load_or_init(self) -> Dict[str, Any]:
        if os.path.isfile(self.manifest_path):
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        
        identity = get_current_release_identity(self.repo_root)
        return {
            "status": "INITIALIZED",
            "release_identity": identity,
            "source_commit": identity.get("commit_sha", ""),
            "target_commit": identity.get("commit_sha", ""),
            "targeted_tests": {},
            "schema_migration": {},
            "data_hash_verification": {},
            "path_mapping_audit": {},
            "security_audit": {},
            "performance_benchmark": {},
            "sidecar_audit": {},
            "browser_e2e": {},
            "upgrade_log": [],
            "final_gate_passed": False
        }

    def record(self, section: str, result_data: Any) -> None:
        """Record evidence for a specific verification section."""
        self.data[section] = result_data
        self.save()

    def add_log(self, message: str) -> None:
        """Append log message."""
        self.data.setdefault("upgrade_log", []).append(message)
        self.save()

    def save(self) -> None:
        """Save manifest atomically to disk."""
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.manifest_path)

    def validate_final_gate(self) -> Tuple[bool, List[str]]:
        """
        Validate all release governance conditions required before UPGRADE_SUCCESS.
        Returns (passed, list_of_unmet_conditions).
        """
        unmet = []
        # 1. Tests
        tests = self.data.get("targeted_tests", {})
        if not tests or any(v.get("status") != "PASSED" for v in tests.values()):
            unmet.append("Targeted test suites have not all passed.")

        # 2. Data hash verification
        data_hash = self.data.get("data_hash_verification", {})
        if not data_hash.get("verified", False):
            unmet.append("Data hash gate has not verified pre/post zero data loss.")

        # 3. Schema preflight
        schema = self.data.get("schema_migration", {})
        if not schema.get("preflight_safe", False):
            unmet.append("Schema preflight safety check not marked as passed.")

        # 4. Security audit
        sec = self.data.get("security_audit", {})
        if sec.get("critical_count", 1) > 0:
            unmet.append("Security audit contains unresolved critical issues.")

        passed = (len(unmet) == 0)
        self.data["final_gate_passed"] = passed
        if passed:
            self.data["status"] = "UPGRADE_SUCCESS"
        self.save()
        return passed, unmet

if __name__ == "__main__":
    pem = ProductionEvidenceManifest()
    pem.save()
    print(f"Evidence Manifest initialized at {pem.manifest_path}")
