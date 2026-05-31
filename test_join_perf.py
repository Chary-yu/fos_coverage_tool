#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Verification test asserting that table joins are fully index-based.
"""

import unittest
from enhance_coverage import source_file_join_condition


class TestJoinConditionPerformance(unittest.TestCase):
    def test_optimized_join_condition(self):
        cond = source_file_join_condition("i", "a")
        print("\n[Performance Verification] Join Condition:", cond)
        
        # Verify direct B-Tree index fields are used
        self.assertIn("a.file_path_hash = i.file_path_hash", cond)
        self.assertIn("a.project_name = i.project_name", cond)
        self.assertIn("a.line_number = i.line_number", cond)
        
        # Verify unindexed string manipulations are completely removed
        self.assertNotIn("REPLACE", cond)
        self.assertNotIn("SUBSTRING", cond)
        self.assertNotIn("SUBSTRING_INDEX", cond)
        self.assertNotIn("COALESCE", cond)
        self.assertNotIn("NULLIF", cond)
        print("[Performance Verification] Index-based join optimization successfully verified!")


if __name__ == "__main__":
    unittest.main()
