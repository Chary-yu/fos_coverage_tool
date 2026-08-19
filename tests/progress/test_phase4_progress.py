"""
Targeted Tests for Phase 4 Progress Database Aggregation (Items 7, 8)
"""

import unittest
import os
import sys
from unittest.mock import MagicMock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.progress.file_state_service import query_project_progress_aggregated, update_file_state_for_file
from scripts.upgrade.migrate_file_state import reconcile_project_progress

class TestPhase4Progress(unittest.TestCase):

    def test_item_7_file_state_query_with_fallback(self):
        """Verify query_project_progress_aggregated uses file_state when available and fallback otherwise."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        
        # 1. file_state returns populated row: (file_count, total, filled, draft, confirmed, cov, uncov, red)
        mock_cursor.fetchone.return_value = (5, 100, 80, 10, 70, 50, 15, 5)
        res = query_project_progress_aggregated(mock_conn, "ProjA")
        self.assertEqual(res["source"], "coverage_file_state")
        self.assertEqual(res["total_uncovered"], 100)
        self.assertEqual(res["confirmed_total"], 70)
        self.assertEqual(res["pending_unconfirmed"], 30)
        
        # 2. file_state empty (file_count=0) -> falls back to authoritative
        mock_cursor.fetchone.side_effect = [
            (0, 0, 0, 0, 0, 0, 0, 0), # file_state query returns 0 files
            (3, 50, 40, 5, 35, 30, 5, 0) # fallback query
        ]
        res_fb = query_project_progress_aggregated(mock_conn, "ProjB", fallback_authoritative=True)
        self.assertEqual(res_fb["source"], "authoritative_fallback")
        self.assertEqual(res_fb["total_uncovered"], 50)
        self.assertEqual(res_fb["confirmed_total"], 35)

    def test_item_8_reconciliation_exact_match(self):
        """Verify reconciliation passes when aggregated matches authoritative."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        
        # Both queries return matching counts
        matching_row = (10, 200, 150, 20, 130, 100, 20, 10)
        mock_cursor.fetchone.side_effect = [
            matching_row, # aggregated
            matching_row  # authoritative
        ]
        is_match, rep = reconcile_project_progress(mock_conn, "ProjMatch")
        self.assertTrue(is_match)
        self.assertEqual(len(rep["diff"]), 0)

if __name__ == "__main__":
    unittest.main()
