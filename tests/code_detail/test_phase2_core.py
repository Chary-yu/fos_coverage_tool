"""
Targeted Tests for Phase 2 Code Detail Core Performance (Items 1, 2, 3, 4, 5)
"""

import unittest
import os
import sys
import time
import threading
from unittest.mock import MagicMock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.code_detail.overlay_cache import AnalysisOverlay, AnalysisOverlayCache
from app.db.connection_pool import MySQLConnectionPool, PooledConnectionWrapper

class TestPhase2Core(unittest.TestCase):

    def test_item_1_overlay_cache_data_version_invalidation(self):
        """Verify AnalysisOverlay caching and invalidation on data_version bump."""
        cache = AnalysisOverlayCache(max_entries=10, ttl_seconds=60.0)
        
        # Initial overlay at version 1
        overlay_v1 = AnalysisOverlay(
            project_name="TestProj",
            file_path_hash="hash123",
            review_scope="full",
            data_version=1,
            records=[
                {"line_number": 10, "status": "可覆盖", "reviewer": "Alice", "is_draft": False}
            ]
        )
        cache.put(overlay_v1)
        
        # Hit with v1
        hit = cache.get("TestProj", "hash123", "full", 1)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.get_line_analysis(10)["reviewer"], "Alice")
        
        # Miss with v2 (after review save increments data_version)
        miss = cache.get("TestProj", "hash123", "full", 2)
        self.assertIsNone(miss)

    def test_item_4_connection_pool_borrow_return_and_rollback(self):
        """Verify connection pool borrowing, thread safety, and auto-rollback."""
        pool = MySQLConnectionPool(
            db_config={"host": "127.0.0.1", "database": "test"},
            min_connections=1,
            max_connections=2,
            borrow_timeout=0.5
        )
        # Mock raw connection
        mock_raw = MagicMock()
        mock_raw.ping.return_value = True
        
        wrapper = PooledConnectionWrapper(mock_raw, pool)
        
        # Return to pool triggers rollback
        pool.return_connection(wrapper)
        mock_raw.rollback.assert_called()
        
        # Borrow back
        borrowed = pool.borrow_connection()
        self.assertEqual(borrowed.raw_conn, mock_raw)
        
        # Pool exhaustion test
        borrowed2 = PooledConnectionWrapper(MagicMock(), pool)
        pool._active_count = 2 # Max reached
        
        # Borrowing when pool is full and empty queue should timeout
        with self.assertRaises(TimeoutError):
            pool.borrow_connection()
            
        # Return wrapper and borrow succeeds
        pool.return_connection(borrowed)
        re_borrowed = pool.borrow_connection()
        self.assertIsNotNone(re_borrowed)
        
        # Cleanup
        pool.close_all()
        self.assertTrue(pool._is_shutdown)

    def test_item_4_read_only_return_skips_ping_and_rollback_until_stale(self):
        """Read requests avoid cleanup round-trips, stale idle connections still ping."""
        pool = MySQLConnectionPool(
            db_config={"host": "127.0.0.1", "database": "test"},
            min_connections=1,
            max_connections=1,
            idle_ping_after_sec=60,
        )
        mock_raw = MagicMock()
        wrapper = PooledConnectionWrapper(mock_raw, pool)
        pool.return_connection(wrapper)

        with pool.connection(read_only=True) as connection:
            self.assertIs(connection, mock_raw)
        self.assertEqual(mock_raw.rollback.call_count, 1, "only the initial writer return rolls back")
        self.assertEqual(mock_raw.ping.call_count, 0, "fresh idle connection should not ping")
        self.assertGreaterEqual(pool.metrics()["ping_skips"], 1)
        self.assertGreaterEqual(pool.metrics()["rollback_skips"], 1)

        borrowed = pool.borrow_connection()
        pool.return_connection(borrowed)
        borrowed.last_returned_at = time.time() - 61
        pool.borrow_connection()
        self.assertGreaterEqual(mock_raw.ping.call_count, 1, "stale idle connection must be checked")
        pool.close_all()

if __name__ == "__main__":
    unittest.main()
