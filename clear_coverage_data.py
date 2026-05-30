#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Clear persisted coverage review data for local debugging.

Examples:
  python3 clear_coverage_data.py --project review_main_202606 --yes
  python3 clear_coverage_data.py --all --yes
"""

import sys

from enhance_coverage import DatabaseManager, load_config


def get_arg_value(args, name):
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1]
    return None


def has_arg(args, name):
    return name in args


def print_help():
    print("Usage:")
    print("  python3 clear_coverage_data.py --project <project_name> --yes")
    print("    - Delete coverage_analysis and coverage_line_index rows for one project.")
    print("  python3 clear_coverage_data.py --all --yes")
    print("    - Delete all rows from coverage_analysis and coverage_line_index.")


def table_count(cursor, table_name, project_name=None):
    if project_name:
        cursor.execute(f"SELECT COUNT(*) FROM {table_name} WHERE project_name = %s", (project_name,))
    else:
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
    row = cursor.fetchone()
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0] if row else 0


def main():
    args = sys.argv[1:]
    project_name = get_arg_value(args, "--project")
    clear_all = has_arg(args, "--all")
    confirmed = has_arg(args, "--yes")

    if not confirmed or (clear_all == bool(project_name)):
        print_help()
        if not confirmed:
            print("[Error] Refusing to clear data without --yes.")
        else:
            print("[Error] Specify exactly one of --project <project_name> or --all.")
        return 1

    config = load_config()
    manager = DatabaseManager(config)
    cursor = manager.conn.cursor()

    scope = "all projects" if clear_all else f"project '{project_name}'"
    analysis_before = table_count(cursor, "coverage_analysis", None if clear_all else project_name)
    index_before = table_count(cursor, "coverage_line_index", None if clear_all else project_name)

    print(f"[ClearDB] Scope: {scope}")
    print(f"[ClearDB] coverage_analysis rows before: {analysis_before}")
    print(f"[ClearDB] coverage_line_index rows before: {index_before}")

    if clear_all:
        cursor.execute("DELETE FROM coverage_analysis")
        cursor.execute("DELETE FROM coverage_line_index")
        cursor.execute("ALTER TABLE coverage_analysis AUTO_INCREMENT = 1")
        cursor.execute("ALTER TABLE coverage_line_index AUTO_INCREMENT = 1")
    else:
        cursor.execute("DELETE FROM coverage_analysis WHERE project_name = %s", (project_name,))
        cursor.execute("DELETE FROM coverage_line_index WHERE project_name = %s", (project_name,))

    manager.conn.commit()

    analysis_after = table_count(cursor, "coverage_analysis", None if clear_all else project_name)
    index_after = table_count(cursor, "coverage_line_index", None if clear_all else project_name)
    cursor.close()
    manager.conn.close()

    print(f"[ClearDB] coverage_analysis rows after: {analysis_after}")
    print(f"[ClearDB] coverage_line_index rows after: {index_after}")
    print("[ClearDB] Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
