"""
Production Evidence Manifest Manager (Item 28)
Manages the structured release evidence ledger:
- Tracks and verifies all 8 phases of release evidence
- Enforces strict zero-false-positive production governance gates
- Produces production_evidence_manifest.json
"""

import os
import sys
import json
import time
from typing import Dict, Any, List, Tuple, Optional

try:
    from datetime import datetime, timezone
    def get_utc_iso():
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(timezone, "utc") else datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
except ImportError:
    from datetime import datetime
    def get_utc_iso():
        return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

MANIFEST_FILENAME = "production_evidence_manifest.json"

class ProductionEvidenceManifest:
    def __init__(self, repo_root: str):
        self.repo_root = repo_root
        self.manifest_path = os.path.join(repo_root, MANIFEST_FILENAME)
        self.data: Dict[str, Any] = self._load_or_init()

    def _load_or_init(self) -> Dict[str, Any]:
        if os.path.isfile(self.manifest_path):
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "schema_version": 2,
            "created_at": get_utc_iso(),
            "updated_at": get_utc_iso(),
            "status": "INITIALIZED",
            "release_identity": {},
            "schema_migration": {},
            "backup_evidence": {},
            "data_hash_verification": {},
            "targeted_tests": {},
            "browser_smoke_suite": {},
            "path_mapping_audit": {},
            "sidecar_audit": {},
            "security_audit": {},
            "performance_benchmark": {},
            "logs": []
        }

    def record(self, section: str, payload: Dict[str, Any]):
        """Record evidence for a specific section."""
        self.data[section] = payload
        self.data["updated_at"] = get_utc_iso()
        self.save()

    def add_log(self, message: str):
        """Append an event log to the ledger."""
        self.data.setdefault("logs", []).append(f"[{get_utc_iso()}] {message}")
        self.save()

    def save(self):
        temp_path = self.manifest_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, self.manifest_path)

    def validate_final_gate(self) -> Tuple[bool, List[str]]:
        """
        Validate all strict production gates to prevent false UPGRADE_SUCCESS certifications.
        Returns (is_passed, list_of_unmet_requirements).
        """
        unmet = []
        
        # 1. Release Identity
        rel = self.data.get("release_identity", {})
        if not rel.get("version") or not rel.get("commit_sha") or not rel.get("build_id"):
            unmet.append("Incomplete release_identity in manifest")
            
        # 2. Targeted Tests
        tt = self.data.get("targeted_tests", {})
        if not tt or any(v.get("status") != "PASSED" for v in tt.values()):
            unmet.append("Targeted unit test matrix is incomplete or contains failures")
            
        # 3. Browser Smoke Suite
        bss = self.data.get("browser_smoke_suite", {})
        if bss.get("status") != "PASSED":
            unmet.append("Browser smoke suite did not pass")
            
        # 4. Data Hash Verification
        dhv = self.data.get("data_hash_verification", {})
        if not dhv.get("verified"):
            unmet.append("Data hash verification gate is not verified")
            
        # 5. Schema Preflight
        sm = self.data.get("schema_migration", {})
        if not sm.get("preflight_safe"):
            unmet.append("Schema migration preflight is not safe")
            
        # 6. Sidecar Audit
        sa = self.data.get("sidecar_audit", {})
        if not sa.get("is_safe"):
            unmet.append(f"Sidecar audit found safety violations: corrupted={sa.get('corrupted_registries')}, orphan={sa.get('orphaned_cache_count')}")
            
        # 7. Security Audit
        sec = self.data.get("security_audit", {})
        if not sec.get("is_safe") or sec.get("critical_count", 0) > 0 or sec.get("high_count", 0) > 0:
            unmet.append(f"Security audit has unresolved findings: Critical={sec.get('critical_count')}, High={sec.get('high_count')}")
            
        # 8. Performance Benchmark
        pb = self.data.get("performance_benchmark", {})
        required_tiers = ["Tier_A_1k", "Tier_B_10k", "Tier_C_50k", "Tier_D_100k"]
        for tier in required_tiers:
            if tier not in pb or pb[tier].get("status") != "PASSED":
                unmet.append(f"Performance benchmark tier missing or failed: {tier}")
                
        # 9. Backup Evidence
        bk = self.data.get("backup_evidence", {})
        if not bk.get("full_sql_gz_sha256") or bk.get("status") != "BACKUP_VERIFIED":
            unmet.append("Backup evidence missing or unverified")
            
        passed = (len(unmet) == 0)
        self.data["status"] = "UPGRADE_SUCCESS" if passed else "UNMET_GATES"
        self.save()
        return passed, unmet
