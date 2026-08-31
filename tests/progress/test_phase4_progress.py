"""
Targeted Tests for Phase 4 Progress Database Aggregation (Items 7, 8)
"""

import unittest
import os
import sys
from unittest import mock
from unittest.mock import MagicMock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.progress.file_state_service import (
    mark_project_aggregate_ready,
    query_project_progress_aggregated,
    query_authoritative_progress,
    rebuild_project_file_state,
    update_file_state_for_file
)
from scripts.upgrade import migrate_file_state
from scripts.upgrade.migrate_file_state import reconcile_project_progress

class TestPhase4Progress(unittest.TestCase):

    def test_item_7_file_state_query_with_version_and_fallback(self):
        """Verify query_project_progress_aggregated uses file_state when version matches and fallback when stale/empty."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        
        # 1. Fresh file_state returns populated row: (1,) for data_version, (5, 100, 80, 10, 70, 50, 15, 5, 1, 1)
        mock_cursor.fetchone.side_effect = [
            (1,), # project data_version = 1
            (5, 100, 80, 10, 70, 50, 15, 5, 1, 1) # file_state aggregate with min_v=1, max_v=1
        ]
        res = query_project_progress_aggregated(mock_conn, "ProjA")
        self.assertEqual(res["source"], "coverage_file_state")
        self.assertEqual(res["total_uncovered"], 100)
        self.assertEqual(res["confirmed_total"], 70)
        self.assertEqual(res["pending_unconfirmed"], 30)
        
        # 2. file_state empty (file_count=0) -> falls back to authoritative facts
        mock_cursor.fetchone.side_effect = [
            (1,), # project data_version
            (0, 0, 0, 0, 0, 0, 0, 0, 0, 0), # file_state empty
            (3, 50, 40, 5, 35, 30, 5, 0) # authoritative facts query
        ]
        res_fb = query_project_progress_aggregated(mock_conn, "ProjB", fallback_authoritative=True)
        self.assertEqual(res_fb["source"], "authoritative_facts")
        self.assertEqual(res_fb["total_uncovered"], 50)
        self.assertEqual(res_fb["confirmed_total"], 35)

    def test_item_8_reconciliation_exact_match(self):
        """Verify reconciliation passes when aggregated matches authoritative."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        
        # Reconcile calls:
        # 1. query_project_progress_aggregated -> project version (1,), file_state (10, 200, 150, 20, 130, 100, 20, 10, 1, 1)
        # 2. query_authoritative_progress -> facts row (10, 200, 150, 20, 130, 100, 20, 10)
        mock_cursor.fetchone.side_effect = [
            (1,), # project version
            (10, 200, 150, 20, 130, 100, 20, 10, 1, 1), # aggregated
            (10, 200, 150, 20, 130, 100, 20, 10) # authoritative facts
        ]
        is_match, rep = reconcile_project_progress(mock_conn, "ProjMatch")
        self.assertTrue(is_match)
        self.assertEqual(len(rep["diff"]), 0)

    def test_legacy_ready_cas_rechecks_published_version(self):
        """A concurrent version advance must not be reported as Ready."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [
            (1,),       # version observed before the conditional update
            (2, 0),     # version changed before the publication check
        ]

        self.assertFalse(
            mark_project_aggregate_ready(mock_conn, "ProjRace", 1)
        )

    def test_legacy_upgrade_backfill_delegates_to_shared_projection_owner(self):
        mock_conn = MagicMock()
        expected = {
            "project_name": "ProjOwner", "data_version": 4,
            "status": "READY", "ready": True,
        }
        with mock.patch.object(
                migrate_file_state,
                "rebuild_project_file_state",
                return_value=expected,
        ) as rebuild:
            result = migrate_file_state.backfill_file_state_for_project(
                mock_conn, "ProjOwner"
            )
        rebuild.assert_called_once_with(mock_conn, "ProjOwner", commit=False)
        self.assertEqual(result, expected)
        self.assertEqual(mock_conn.commit.call_count, 2)

    def test_malformed_driver_row_fails_closed_during_legacy_rebuild(self):
        """A non-row test double must not turn fallback into an IndexError."""
        result = rebuild_project_file_state(MagicMock(), "ProjMalformed")
        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "INVALID_PROJECT_STATE")

    def test_dict_cursor_fields_are_read_by_name(self):
        """DictCursor insertion order is not part of the SQL contract."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [
            {"file_state_version": 1, "data_version": 1},
            {
                "max_version": 1, "redundant_total": 5,
                "uncoverable_total": 15, "coverable_total": 50,
                "confirmed_total": 70, "draft_total": 10,
                "filled_total": 80, "total_uncovered": 100,
                "file_count": 5, "min_version": 1,
            },
        ]

        result = query_project_progress_aggregated(mock_conn, "ProjDict")

        self.assertEqual(result["source"], "coverage_file_state")
        self.assertEqual(result["file_count"], 5)
        self.assertEqual(result["total_uncovered"], 100)
        self.assertEqual(result["confirmed_total"], 70)

    def test_malformed_legacy_version_cannot_promote_derived_rows(self):
        """An invalid project version must use authoritative facts."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [
            {"data_version": "not-an-integer", "file_state_version": 1},
            {
                "file_count": 1, "total_uncovered": 10,
                "filled_total": 5, "draft_total": 0,
                "confirmed_total": 5, "coverable_total": 5,
                "uncoverable_total": 0, "redundant_total": 0,
                "min_version": 1, "max_version": 1,
            },
            (1, 10, 5, 0, 5, 5, 0, 0),
        ]

        result = query_project_progress_aggregated(mock_conn, "ProjBadVersion")

        self.assertEqual(result["source"], "authoritative_facts")
        self.assertFalse(result["aggregate_ready"])

    def test_malformed_legacy_ready_rows_fail_closed(self):
        """A malformed CAS row cannot publish a Ready marker."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.return_value = {"data_version": "not-an-integer"}

        self.assertFalse(mark_project_aggregate_ready(mock_conn, "ProjBadCAS", 1))
        self.assertEqual(mock_cursor.execute.call_count, 1)

    def test_malformed_legacy_state_version_fails_closed_during_rebuild(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.return_value = {"data_version": "not-an-integer"}

        result = rebuild_project_file_state(mock_conn, "ProjBadRebuild")

        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "INVALID_PROJECT_STATE")
        self.assertEqual(mock_cursor.execute.call_count, 1)

if __name__ == "__main__":
    unittest.main()
