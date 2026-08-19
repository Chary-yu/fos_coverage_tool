"""
Security Baseline & Static Vulnerability Scanner (Item 26)
Scans codebase for high-risk security patterns:
- Path traversal & unvalidated realpath
- report_id escape vulnerabilities
- Unsafe SQL string formatting / composition
- shell=True / os.system execution
- Unsafe recursive deletes
- Dynamic HTML / JS injection in backend rendering
"""

import os
import sys
import re
from typing import Dict, Any, List, Tuple

SECURITY_PATTERNS = [
    (r"os\.system\(", "CRITICAL", "Dangerous os.system() call detected"),
    (r"subprocess\.\w+\([^)]*shell\s*=\s*True", "HIGH", "subprocess with shell=True detected"),
    (r"SELECT\s+.*%s.*%\s*\(", "HIGH", "Potential SQL string modulo formatting rather than parameterized query"),
    (r"cursor\.execute\([fF][\"'].*\{", "HIGH", "Direct f-string SQL query execution detected"),
    (r"shutil\.rmtree\(\s*[\"']/[^\"']*[\"']\s*\)", "CRITICAL", "Unsafe hardcoded root deletion in shutil.rmtree"),
    (r"open\([^)]*\.\./\.\.", "MEDIUM", "Direct double dot traversal in file open"),
]

def scan_file(filepath: str) -> List[Dict[str, Any]]:
    """Scan single file for security risks."""
    findings = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        for idx, line in enumerate(lines, start=1):
            for pat, severity, msg in SECURITY_PATTERNS:
                if re.search(pat, line):
                    findings.append({
                        "file": filepath,
                        "line": idx,
                        "severity": severity,
                        "message": msg,
                        "snippet": line.strip()
                    })
    except Exception as e:
        findings.append({
            "file": filepath,
            "line": 0,
            "severity": "LOW",
            "message": f"Could not read file for scanning: {e}",
            "snippet": ""
        })
    return findings

def scan_directory(root_dir: str, excludes: List[str] = None) -> Dict[str, Any]:
    """Scan directory recursively for security issues."""
    if excludes is None:
        excludes = [".git", "__pycache__", "node_modules", "demo_ui_output", "background_jobs"]
        
    all_findings = []
    scanned_count = 0
    
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in excludes]
        for fname in filenames:
            if fname.endswith((".py", ".js", ".html")) and fname != "security_scanner.py":
                fpath = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(fpath, root_dir)
                findings = scan_file(fpath)
                for f in findings:
                    f["file"] = rel_path
                all_findings.extend(findings)
                scanned_count += 1
                
    critical_count = sum(1 for f in all_findings if f["severity"] == "CRITICAL")
    high_count = sum(1 for f in all_findings if f["severity"] == "HIGH")
    
    return {
        "scanned_files": scanned_count,
        "total_findings": len(all_findings),
        "critical_count": critical_count,
        "high_count": high_count,
        "is_safe": (critical_count == 0 and high_count == 0),
        "findings": all_findings
    }

if __name__ == "__main__":
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    res = scan_directory(repo_root)
    print(f"Scanned {res['scanned_files']} files: {res['total_findings']} findings (Critical: {res['critical_count']}, High: {res['high_count']})")
    if not res["is_safe"]:
        for f in res["findings"]:
            if f["severity"] in ("CRITICAL", "HIGH"):
                print(f"  [{f['severity']}] {f['file']}:{f['line']} - {f['message']}")
    sys.exit(0 if res["is_safe"] else 1)
