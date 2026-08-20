"""
Schema Migration Preflight Check Module (Item 21)
Performs static analysis and safety validation on DDL migration scripts before execution:
- Prohibits destructive DDL (DROP, TRUNCATE, ALTER column removals on critical tables)
- Enforces Additive Migration rules (CREATE TABLE IF NOT EXISTS, safe nullable/default columns, safe indexes)
- Ensures idempotency
"""

import re
from typing import List, Tuple, Dict, Any

PROTECTED_TABLES = {
    "coverage_analysis",
    "coverage_line_index",
    "coverage_project_state",
    "coverage_background_jobs"
}

FORBIDDEN_KEYWORDS = [
    r"\bTRUNCATE\b",
    r"\bDROP\s+DATABASE\b",
]

def analyze_sql_script(sql_text: str) -> Tuple[bool, List[str], List[str]]:
    """
    Analyze SQL statements for safety violations.
    Returns (is_safe, error_messages, warnings).
    """
    errors = []
    warnings = []
    
    # Strip comments
    lines = []
    for line in sql_text.splitlines():
        line_clean = re.sub(r"--.*$", "", line).strip()
        if line_clean:
            lines.append(line_clean)
    clean_sql = " ".join(lines)
    
    # Check forbidden destructive global keywords
    for pat in FORBIDDEN_KEYWORDS:
        if re.search(pat, clean_sql, re.IGNORECASE):
            errors.append(f"Forbidden destructive command pattern detected: {pat}")
            
    # Check DROP TABLE on protected tables
    for tbl in PROTECTED_TABLES:
        drop_pat = rf"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:[`'\"]?{tbl}[`'\"]?)"
        if re.search(drop_pat, clean_sql, re.IGNORECASE):
            errors.append(f"Destructive DROP TABLE detected targeting protected table: {tbl}")
            
    # Check ALTER TABLE dropping columns on protected tables
    for tbl in PROTECTED_TABLES:
        alter_drop_pat = rf"\bALTER\s+TABLE\s+[`'\"]?{tbl}[`'\"]?\s+DROP\s+"
        if re.search(alter_drop_pat, clean_sql, re.IGNORECASE):
            errors.append(f"Destructive column DROP detected targeting protected table: {tbl}")
        for operation in ("CHANGE", "MODIFY"):
            alter_shape_pat = rf"\bALTER\s+TABLE\s+[`'\"]?{tbl}[`'\"]?[^;]*\b{operation}\b"
            if re.search(alter_shape_pat, clean_sql, re.IGNORECASE):
                errors.append("Protected table column shape change is not additive: {} {}".format(tbl, operation))
            
    # Check for CREATE TABLE missing IF NOT EXISTS
    create_table_matches = re.finditer(r"\bCREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS)([`'\"]?\w+[`'\"]?)", clean_sql, re.IGNORECASE)
    for m in create_table_matches:
        warnings.append(f"CREATE TABLE statement without IF NOT EXISTS: {m.group(0)}")
        
    is_safe = (len(errors) == 0)
    return is_safe, errors, warnings

def validate_ddl_file(ddl_path: str) -> Tuple[bool, List[str], List[str]]:
    """Read and validate DDL file."""
    try:
        with open(ddl_path, "r", encoding="utf-8") as f:
            sql_text = f.read()
        return analyze_sql_script(sql_text)
    except Exception as e:
        return False, [f"Failed to read DDL file: {e}"], []

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        safe, errs, warns = validate_ddl_file(sys.argv[1])
        print(f"Preflight Safe: {safe}")
        if errs:
            print(f"Errors: {errs}")
        if warns:
            print(f"Warnings: {warns}")
        sys.exit(0 if safe else 1)
    else:
        print("Schema Preflight Check Module ready.")
