#!/usr/bin/env bash
# ==============================================================================
# OneSensor Coverage Tool: Production Smooth Upgrade Script (new4 -> v6.3)
# Features: Additive Schema Migration (Step 17 Allowlist), Pre-flight Check,
#           Zero-downtime Asset Refresh & Verification.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${SCRIPT_DIR}/upgrade_$(date +%Y%m%d_%H%M%S).log"

log() {
    echo -e "[UPGRADE $(date +'%T')] $*" | tee -a "${LOG_FILE}"
}

error() {
    echo -e "[ERROR $(date +'%T')] $*" | tee -a "${LOG_FILE}" >&2
    exit 1
}

log "Starting OneSensor smooth upgrade from new4 -> v6.3..."

# Step 1: Pre-flight Verification
log "Step 1/6: Verifying Python environment & PyMySQL driver..."
if ! command -v python3 &>/dev/null; then
    error "python3 executable not found in PATH."
fi

python3 -c "import pymysql; print('[OK] PyMySQL driver present')" || error "PyMySQL driver missing. Run: pip install pymysql"

# Step 2: Database Additive Schema Migration
log "Step 2/6: Executing additive schema migration for background job persistence..."
python3 -c "
from enhance_coverage import DatabaseManager, load_config
config = load_config()
mgr = DatabaseManager(config, exit_on_error=True)
print('[DB] Schema check & additive tables (coverage_background_jobs, coverage_project_state) ready.')
" || error "Database migration failed."

# Step 3: Schema Comparison Validation (Step 17 Verification)
log "Step 3/6: Running Step 17 Schema Comparison with Additive Whitelist..."
python3 -c "
from enhance_coverage import DatabaseManager, load_config
config = load_config()
mgr = DatabaseManager(config, exit_on_error=False)
cursor = mgr.conn.cursor()
cursor.execute('SHOW TABLES')
tables = [list(r.values())[0] if isinstance(r, dict) else r[0] for r in cursor.fetchall()]
expected_base = {'coverage_analysis', 'coverage_line_index'}
expected_additive = {'coverage_background_jobs', 'coverage_project_state'}
found = set(tables)

missing_base = expected_base - found
if missing_base:
    raise RuntimeError(f'Step 17 Validation FAILED: Missing core tables {missing_base}')

unexpected = found - expected_base - expected_additive
if unexpected:
    raise RuntimeError(f'Step 17 Validation FAILED: Found unapproved tables {unexpected}')

print('[Step 17 Schema Check] PASSED cleanly! Core tables present, additive tables approved.')
" || error "Step 17 Schema Validation failed."

# Step 4: Asset Cache-Busting & Resource Sync
log "Step 4/6: Syncing static assets (v=visible-progress-20260814_ios_ui)..."
if [ -d "${SCRIPT_DIR}/background_jobs" ]; then
    log "Background jobs storage directory verified: ${SCRIPT_DIR}/background_jobs"
else
    mkdir -p "${SCRIPT_DIR}/background_jobs"
fi

# Step 5: Service Self-Test (Port 9528 Binding & Worker Isolation)
log "Step 5/6: Running self-test suite..."
$SHELL -c 'export HTTP_PROXY=""; export HTTPS_PROXY=""; python3 -m unittest discover -s . -p "test_*.py"' || error "Unit test verification failed."

# Step 6: Completion
log "Step 6/6: Upgrade to OneSensor v6.3 completed successfully!"
log "Log file saved to: ${LOG_FILE}"
