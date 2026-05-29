#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
HTML 覆盖率报告增强主控工具 (Industrial Grade) - 修订版 (支持独立输出目录，保护原始输入，支持确认人列)
1. 静态 HTML 注入与资源拷贝 (inject 子命令 - 支持复制到输出目录后修改)
2. 轻量级支持 CORS 的本地 API 服务与 MySQL 存取 (server 子命令 - 自动建表与表结构热升级)
"""

import os
import sys
import json
import shutil
import re
import hashlib
import csv
import io
import html
import time
from datetime import datetime
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

# 动态探测并优先使用 pymysql 或 mysql.connector
db_module = None
for module_name in ['pymysql', 'mysql.connector']:
    try:
        db_module = __import__(module_name)
        break
    except ImportError:
        continue

# 脚本根目录和配置文件位置
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "coverage_config.json")
JS_SOURCE_PATH = os.path.join(SCRIPT_DIR, "coverage_enhance.js")
CSS_SOURCE_PATH = os.path.join(SCRIPT_DIR, "coverage_enhance.css")
ASSET_VERSION = "dop-redundant-status-20260528"


def calc_file_path_hash(file_path):
    """Return a stable compact key for the full report file path."""
    return hashlib.md5(str(file_path).encode("utf-8")).hexdigest()


def calc_text_hash(value):
    """Return a stable hash for normalized source text."""
    return hashlib.md5(str(value).encode("utf-8")).hexdigest()


def get_source_file_name(file_path):
    """Return the stable source filename used for cross-build inheritance."""
    normalized = str(file_path or "").replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1]


def row_value(row, key, index):
    if isinstance(row, dict):
        return row.get(key)
    return row[index]


CONTROL_FLOW_RE = re.compile(r'\b(if|else|for|while|do|switch|case|default)\b')
FUNC_ENTRY_RE = re.compile(r'^[A-Za-z_][\w\s\*]*\s+[A-Za-z_]\w*\s*\([^;]*\)\s*(\{|$)')


def strip_html_text(value):
    value = re.sub(r'<[^>]+>', '', value)
    return html.unescape(value).replace('\r', '').strip()


def get_code_text(line_text):
    colon_index = line_text.find(':')
    return (line_text[colon_index + 1:] if colon_index >= 0 else line_text).strip()


def is_control_flow_text(code_text):
    return CONTROL_FLOW_RE.search(code_text) is not None


def is_function_entry_text(code_text):
    code_text = re.sub(r'/\*.*?\*/', '', code_text).strip()
    code_text = re.sub(r'\s+', ' ', code_text)
    if not code_text or code_text.endswith(';') or is_control_flow_text(code_text):
        return False
    if re.match(r'^(return|typedef|struct|enum|union)\b', code_text):
        return False
    return FUNC_ENTRY_RE.search(code_text) is not None


def strip_line_comment(code_text):
    return re.sub(r'//.*$', '', code_text or '').strip()


def is_jump_text(code_text):
    return re.match(r'^(return|goto|break|continue)\b', strip_line_comment(code_text)) is not None


def is_structural_text(code_text):
    text = strip_line_comment(code_text)
    return text == '' or re.match(r'^[{}]+;?$', text) is not None


def is_simple_auto_group_text(code_text):
    text = strip_line_comment(code_text)
    text = re.sub(r'/\*.*?\*/', '', text).strip()
    if not text or is_control_flow_text(text) or is_function_entry_text(text) or is_jump_text(text):
        return False
    if re.match(r'^[{}]+;?$', text) or re.match(r'^(case\b.*:|default\s*:|[A-Za-z_]\w*\s*:)$', text):
        return False
    if not text.endswith(';'):
        return False
    has_assignment = re.search(r'(^|[^=!<>])=([^=]|$)', text) is not None or re.search(r'(\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=)', text) is not None
    is_simple_declaration = re.match(r'^(?:const\s+|static\s+|volatile\s+|register\s+|unsigned\s+|signed\s+|struct\s+\w+\s+|enum\s+\w+\s+|union\s+\w+\s+|[A-Za-z_]\w*\s+)+[*\s]*[A-Za-z_]\w*(?:\s*=\s*[^;]+)?\s*;$', text) is not None
    return has_assignment or is_simple_declaration


def extract_function_name(code_text):
    code_text = re.sub(r'/\*.*?\*/', '', code_text).strip()
    code_text = re.sub(r'\s+', ' ', code_text)
    match = re.search(r'([A-Za-z_]\w*)\s*\([^;]*\)\s*(\{|$)', code_text)
    return match.group(1) if match else ""


def normalize_code_for_hash(code_text):
    return re.sub(r'\s+', ' ', (code_text or '').strip())


def extract_report_file_path(content, fallback_path):
    title_match = re.search(r'<title[^>]*>(.*?)</title>', content, re.I | re.S)
    if title_match:
        title_text = strip_html_text(title_match.group(1))
        lcov_match = re.search(r'LCOV\s+-\s+.*?\s+-\s+(.+)$', title_text)
        if lcov_match:
            return lcov_match.group(1).strip()
    return fallback_path.replace(os.sep, '/').replace('.gcov.html', '')


def extract_line_index_records(content, fallback_path, project_name):
    file_path = extract_report_file_path(content, fallback_path)
    file_path_hash = calc_file_path_hash(file_path)
    source_file_name = get_source_file_name(file_path)
    line_pattern = re.compile(r'<span class="lineNum">\s*(\d+)\s*</span>(.*?)(?=<span class="lineNum">|</pre>)', re.S)
    lines = []
    for match in line_pattern.finditer(content):
        tail = match.group(2)
        line_text = strip_html_text(tail)
        code_text = get_code_text(line_text)
        lines.append({
            "project_name": project_name,
            "file_path": file_path,
            "file_path_hash": file_path_hash,
            "source_file_name": source_file_name,
            "line_number": int(match.group(1)),
            "line_text": code_text,
            "is_uncovered": re.search(r'\b(lineNoCov|tlaUNC|tlaBgUNC)\b', tail) is not None,
            "code_text": code_text,
            "function_name": "",
            "function_hash": "",
            "code_line_hash": calc_text_hash(normalize_code_for_hash(code_text)),
            "code_occurrence": 1
        })

    function_ranges = []
    current_start = None
    for index, item in enumerate(lines):
        if is_function_entry_text(item["code_text"]):
            if current_start is not None:
                function_ranges.append((current_start, index - 1))
            current_start = index
    if current_start is not None:
        function_ranges.append((current_start, len(lines) - 1))

    for start, end in function_ranges:
        function_name = extract_function_name(lines[start]["code_text"])
        function_body = "\n".join(
            normalize_code_for_hash(line["code_text"])
            for line in lines[start:end + 1]
            if normalize_code_for_hash(line["code_text"])
        )
        function_hash = calc_text_hash(function_body)
        occurrence_by_line_hash = {}
        for line in lines[start:end + 1]:
            code_line_hash = calc_text_hash(normalize_code_for_hash(line["code_text"]))
            occurrence_by_line_hash[code_line_hash] = occurrence_by_line_hash.get(code_line_hash, 0) + 1
            line["function_name"] = function_name
            line["function_hash"] = function_hash
            line["code_line_hash"] = code_line_hash
            line["code_occurrence"] = occurrence_by_line_hash[code_line_hash]

    records = []
    counted = set()
    for index, item in enumerate(lines):
        if not item["is_uncovered"] or item["line_number"] in counted:
            continue

        block = [item]
        block_type = "function_entry" if is_function_entry_text(item["code_text"]) else "control_flow" if is_control_flow_text(item["code_text"]) else "single"
        if block_type != "control_flow":
            if block_type == "single":
                block_type = "straight_line"
            for next_item in lines[index + 1:]:
                if is_control_flow_text(next_item["code_text"]) or is_function_entry_text(next_item["code_text"]):
                    break
                if next_item["is_uncovered"]:
                    if block_type == "function_entry" and not is_simple_auto_group_text(next_item["code_text"]):
                        break
                    if block_type != "function_entry" and (
                        not is_simple_auto_group_text(item["code_text"]) or
                        not is_simple_auto_group_text(next_item["code_text"])
                    ):
                        break
                    block.append(next_item)
                    continue
                if block_type != "function_entry":
                    break
                if not is_structural_text(next_item["code_text"]):
                    break

        block_start = block[0]["line_number"]
        block_end = block[-1]["line_number"]
        for block_item in block:
            if block_item["line_number"] in counted:
                continue
            counted.add(block_item["line_number"])
            records.append({
                "project_name": block_item["project_name"],
                "file_path": block_item["file_path"],
                "file_path_hash": block_item["file_path_hash"],
                "source_file_name": block_item["source_file_name"],
                "line_number": block_item["line_number"],
                "line_text": block_item["line_text"],
                "block_start_line": block_start,
                "block_end_line": block_end,
                "block_type": block_type,
                "function_name": block_item["function_name"],
                "function_hash": block_item["function_hash"],
                "code_line_hash": block_item["code_line_hash"],
                "code_occurrence": block_item["code_occurrence"]
            })
    return records


def load_config():
    """从配置文件加载配置，若不存在则使用默认配置"""
    default_config = {
        "mysql": {
            "host": "localhost",
            "port": 3306,
            "user": "root",
            "password": "",
            "database": "coverage"
        },
        "server": {
            "host": "0.0.0.0",
            "port": 9528
        },
        "project_name": "Gemini-NOS"
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"[Warning] Failed to load config file: {e}. Using defaults.")
    return default_config


class DatabaseManager:
    """MySQL 数据库管理层，处理连接、建库、建表以及存取操作"""
    def __init__(self, config, exit_on_error=True):
        self.config = config["mysql"]
        self.exit_on_error = exit_on_error
        if not db_module:
            print("[CRITICAL] Missing MySQL driver. Please install PyMySQL to enable database support:")
            print("           pip install pymysql")
            if self.exit_on_error:
                sys.exit(1)
            raise RuntimeError("Missing MySQL driver")
        self.conn = None
        self.init_database()

    def get_connection(self, select_db=True):
        """建立并返回 MySQL 连接"""
        params = {
            "host": self.config["host"],
            "port": int(self.config["port"]),
            "user": self.config["user"],
            "password": str(self.config["password"]),
            "charset": "utf8mb4"
        }
        
        # 兼容 pymysql 和 mysql.connector 的 cursor 字典模式
        if db_module.__name__ == 'pymysql':
            params["cursorclass"] = db_module.cursors.DictCursor
            
        conn = db_module.connect(**params)
        if select_db:
            conn.select_db(self.config["database"])
        return conn

    def ensure_index(self, cursor, table_name, index_name, create_sql):
        cursor.execute(f"SHOW INDEX FROM {table_name} WHERE Key_name = %s", (index_name,))
        if not cursor.fetchall():
            print(f"[DB] Creating index {index_name} on {table_name}...")
            cursor.execute(create_sql)

    def ensure_column(self, cursor, table_name, column_name, alter_sql):
        cursor.execute(f"SHOW COLUMNS FROM {table_name} LIKE %s", (column_name,))
        if not cursor.fetchall():
            print(f"[DB] Adding column {column_name} to {table_name}...")
            cursor.execute(alter_sql)

    def init_database(self):
        """自动检查并初始化数据库、数据表以及升级字段"""
        try:
            # 1. 尝试无数据库连接，若没有指定库则创建
            conn = self.get_connection(select_db=False)
            cursor = conn.cursor()
            db_name = self.config["database"]
            
            print(f"[DB] Checking / Creating database '{db_name}'...")
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS {db_name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            cursor.close()
            conn.close()

            # 2. 连接具体数据库，创建表结构
            self.conn = self.get_connection(select_db=True)
            cursor = self.conn.cursor()
            
            table_sql = """
            CREATE TABLE IF NOT EXISTS coverage_analysis (
                id INT AUTO_INCREMENT PRIMARY KEY,
                project_name VARCHAR(128) NOT NULL,
                file_path VARCHAR(512) NOT NULL,
                file_path_hash CHAR(32) NOT NULL,
                source_file_name VARCHAR(255) DEFAULT '',
                line_number INT NOT NULL,
                reviewer VARCHAR(128) DEFAULT '' COMMENT '确认人',
                status VARCHAR(64) NOT NULL DEFAULT '未确认',
                coverage_method VARCHAR(256) DEFAULT '',
                uncovered_reason TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY ukey_proj_file_line (project_name, file_path_hash, line_number)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """
            print("[DB] Creating coverage_analysis table if not exists...")
            cursor.execute(table_sql)
            self.conn.commit()

            # 3. 动态检查并热升级字段结构 (添加 reviewer 字段)
            cursor.execute("SHOW COLUMNS FROM coverage_analysis")
            columns = []
            for col in cursor.fetchall():
                if isinstance(col, dict):
                    columns.append(col["Field"])
                else:
                    columns.append(col[0])

            if "reviewer" not in columns:
                print("[DB] Upgrading schema: adding 'reviewer' column...")
                alter_sql = "ALTER TABLE coverage_analysis ADD COLUMN reviewer VARCHAR(128) DEFAULT '' AFTER line_number"
                cursor.execute(alter_sql)
                self.conn.commit()
                print("[DB] Schema upgrade complete.")

            if "file_path_hash" not in columns:
                print("[DB] Upgrading schema: adding 'file_path_hash' column...")
                cursor.execute("ALTER TABLE coverage_analysis ADD COLUMN file_path_hash CHAR(32) NOT NULL DEFAULT '' AFTER file_path")
                cursor.execute("UPDATE coverage_analysis SET file_path_hash = MD5(file_path) WHERE file_path_hash = ''")
                self.conn.commit()
                print("[DB] file_path_hash backfill complete.")

            cursor.execute("SHOW INDEX FROM coverage_analysis WHERE Key_name = 'ukey_proj_file_line'")
            index_rows = cursor.fetchall()
            needs_hash_index = True
            if index_rows:
                index_columns = []
                for row in index_rows:
                    if isinstance(row, dict):
                        index_columns.append(row.get("Column_name"))
                    else:
                        index_columns.append(row[4])
                needs_hash_index = index_columns != ["project_name", "file_path_hash", "line_number"]
                if needs_hash_index:
                    print("[DB] Upgrading schema: replacing old unique index with hash index...")
                    cursor.execute("ALTER TABLE coverage_analysis DROP INDEX ukey_proj_file_line")
                    self.conn.commit()

            if needs_hash_index:
                cursor.execute("""
                    ALTER TABLE coverage_analysis
                    ADD UNIQUE KEY ukey_proj_file_line (project_name, file_path_hash, line_number)
                """)
                self.conn.commit()
                print("[DB] Hash unique index ready.")

            index_table_sql = """
            CREATE TABLE IF NOT EXISTS coverage_line_index (
                id INT AUTO_INCREMENT PRIMARY KEY,
                project_name VARCHAR(128) NOT NULL,
                file_path VARCHAR(512) NOT NULL,
                file_path_hash CHAR(32) NOT NULL,
                source_file_name VARCHAR(255) DEFAULT '',
                line_number INT NOT NULL,
                line_text TEXT,
                block_start_line INT NOT NULL,
                block_end_line INT NOT NULL,
                block_type VARCHAR(64) NOT NULL DEFAULT 'single',
                function_name VARCHAR(256) DEFAULT '',
                function_hash CHAR(32) DEFAULT '',
                code_line_hash CHAR(32) DEFAULT '',
                code_occurrence INT NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY ukey_line_index (project_name, file_path_hash, line_number),
                KEY idx_line_index_project (project_name),
                KEY idx_line_index_project_file (project_name, file_path_hash),
                KEY idx_line_index_inherit (project_name(32), source_file_name(64), function_hash, code_line_hash, code_occurrence)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """
            print("[DB] Creating coverage_line_index table if not exists...")
            cursor.execute(index_table_sql)
            self.conn.commit()
            self.ensure_column(cursor, "coverage_line_index", "function_name", "ALTER TABLE coverage_line_index ADD COLUMN function_name VARCHAR(256) DEFAULT '' AFTER block_type")
            self.ensure_column(cursor, "coverage_line_index", "function_hash", "ALTER TABLE coverage_line_index ADD COLUMN function_hash CHAR(32) DEFAULT '' AFTER function_name")
            self.ensure_column(cursor, "coverage_line_index", "code_line_hash", "ALTER TABLE coverage_line_index ADD COLUMN code_line_hash CHAR(32) DEFAULT '' AFTER function_hash")
            self.ensure_column(cursor, "coverage_line_index", "code_occurrence", "ALTER TABLE coverage_line_index ADD COLUMN code_occurrence INT NOT NULL DEFAULT 1 AFTER code_line_hash")
            self.ensure_column(cursor, "coverage_line_index", "source_file_name", "ALTER TABLE coverage_line_index ADD COLUMN source_file_name VARCHAR(255) DEFAULT '' AFTER file_path_hash")
            self.ensure_index(cursor, "coverage_line_index", "idx_line_index_project", "CREATE INDEX idx_line_index_project ON coverage_line_index (project_name)")
            self.ensure_index(cursor, "coverage_line_index", "idx_line_index_project_file", "CREATE INDEX idx_line_index_project_file ON coverage_line_index (project_name, file_path_hash)")
            cursor.execute("SHOW INDEX FROM coverage_line_index WHERE Key_name = 'idx_line_index_inherit'")
            inherit_index_rows = cursor.fetchall()
            inherit_index_columns = []
            for row in inherit_index_rows:
                if isinstance(row, dict):
                    inherit_index_columns.append(row.get("Column_name"))
                else:
                    inherit_index_columns.append(row[4])
            if inherit_index_rows and inherit_index_columns != ["project_name", "source_file_name", "function_hash", "code_line_hash", "code_occurrence"]:
                print("[DB] Rebuilding idx_line_index_inherit for basename-based inheritance...")
                cursor.execute("DROP INDEX idx_line_index_inherit ON coverage_line_index")
                inherit_index_rows = []
            if not inherit_index_rows:
                print("[DB] Creating index idx_line_index_inherit on coverage_line_index...")
                cursor.execute("CREATE INDEX idx_line_index_inherit ON coverage_line_index (project_name(32), source_file_name(64), function_hash, code_line_hash, code_occurrence)")
            self.conn.commit()

            cursor.close()
            print("[DB] Database initialization complete.")
        except Exception as e:
            print(f"[CRITICAL] Database initialization failed: {e}")
            if self.exit_on_error:
                sys.exit(1)
            raise

    def fetch_records(self, project_name, file_path):
        """拉取指定项目与文件的覆盖率分析结论"""
        try:
            try:
                self.conn.ping(reconnect=True)
            except AttributeError:
                pass
                
            cursor = self.conn.cursor()
            file_path_hash = calc_file_path_hash(file_path)
            sql = """
                SELECT line_number, reviewer, status, coverage_method, uncovered_reason
                FROM coverage_analysis
                WHERE project_name = %s AND file_path_hash = %s AND file_path = %s
            """
            cursor.execute(sql, (project_name, file_path_hash, file_path))
            rows = cursor.fetchall()
            cursor.close()

            records = []
            for row in rows:
                if isinstance(row, dict):
                    records.append({
                        "line_number": row.get("line_number"),
                        "reviewer": row.get("reviewer", ""),
                        "status": row.get("status"),
                        "coverage_method": row.get("coverage_method"),
                        "uncovered_reason": row.get("uncovered_reason")
                    })
                else:
                    records.append({
                        "line_number": row[0],
                        "reviewer": row[1] if row[1] is not None else "",
                        "status": row[2],
                        "coverage_method": row[3],
                        "uncovered_reason": row[4]
                    })
            return records
        except Exception as e:
            print(f"[DB Error] Fetch failed: {e}")
            return []

    def save_record(self, project_name, file_path, line_number, reviewer, status, method, reason):
        """保存或更新单行分析结论"""
        try:
            try:
                self.conn.ping(reconnect=True)
            except AttributeError:
                pass
                
            cursor = self.conn.cursor()
            file_path_hash = calc_file_path_hash(file_path)
            sql = """
            INSERT INTO coverage_analysis 
                (project_name, file_path, file_path_hash, line_number, reviewer, status, coverage_method, uncovered_reason) 
            VALUES 
                (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE 
                file_path = VALUES(file_path),
                reviewer = VALUES(reviewer),
                status = VALUES(status), 
                coverage_method = VALUES(coverage_method), 
                uncovered_reason = VALUES(uncovered_reason)
            """
            cursor.execute(sql, (project_name, file_path, file_path_hash, int(line_number), reviewer, status, method, reason))
            self.conn.commit()
            cursor.close()
            return True
        except Exception as e:
            print(f"[DB Error] Save failed: {e}")
            return False

    def sync_line_index(self, project_name, records, batch_size=5000):
        try:
            try:
                self.conn.ping(reconnect=True)
            except AttributeError:
                pass

            cursor = self.conn.cursor()
            records_by_file = {}
            for rec in records:
                records_by_file.setdefault(rec["file_path_hash"], []).append(rec)

            insert_sql = """
            INSERT INTO coverage_line_index
                (project_name, file_path, file_path_hash, source_file_name, line_number, line_text,
                 block_start_line, block_end_line, block_type,
                 function_name, function_hash, code_line_hash, code_occurrence)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                file_path = VALUES(file_path),
                source_file_name = VALUES(source_file_name),
                line_text = VALUES(line_text),
                block_start_line = VALUES(block_start_line),
                block_end_line = VALUES(block_end_line),
                block_type = VALUES(block_type),
                function_name = VALUES(function_name),
                function_hash = VALUES(function_hash),
                code_line_hash = VALUES(code_line_hash),
                code_occurrence = VALUES(code_occurrence),
                created_at = CURRENT_TIMESTAMP
            """
            synced_total = 0
            if records:
                for file_hash, file_records in records_by_file.items():
                    cursor.execute(
                        "DELETE FROM coverage_line_index WHERE project_name = %s AND file_path_hash = %s",
                        (project_name, file_hash)
                    )
                    for start in range(0, len(file_records), batch_size):
                        batch = file_records[start:start + batch_size]
                        payload = [
                            (
                                project_name,
                                rec["file_path"],
                                rec["file_path_hash"],
                                rec.get("source_file_name", get_source_file_name(rec["file_path"])),
                                rec["line_number"],
                                rec["line_text"],
                                rec["block_start_line"],
                                rec["block_end_line"],
                                rec["block_type"],
                                rec.get("function_name", ""),
                                rec.get("function_hash", ""),
                                rec.get("code_line_hash", ""),
                                rec.get("code_occurrence", 1)
                            )
                            for rec in batch
                        ]
                        cursor.executemany(insert_sql, payload)
                        synced_total += len(batch)
                    self.conn.commit()
            cursor.close()
            print(f"[DB] Synced {synced_total} uncovered line index record(s) across {len(records_by_file)} file(s) for project '{project_name}'.")
            return True
        except Exception as e:
            print(f"[DB Error] Line index sync failed: {e}")
            return False

    def delete_line_index_file(self, project_name, file_path_hash):
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "DELETE FROM coverage_line_index WHERE project_name = %s AND file_path_hash = %s",
                (project_name, file_path_hash)
            )
            self.conn.commit()
            cursor.close()
            return True
        except Exception as e:
            print(f"[DB Error] Line index file cleanup failed: {e}")
            return False

    def prune_line_index_project_files(self, project_name, active_file_hashes):
        if not active_file_hashes:
            return True
        try:
            cursor = self.conn.cursor()
            placeholders = ",".join(["%s"] * len(active_file_hashes))
            sql = f"""
                DELETE FROM coverage_line_index
                WHERE project_name = %s
                  AND file_path_hash NOT IN ({placeholders})
            """
            cursor.execute(sql, [project_name] + list(active_file_hashes))
            self.conn.commit()
            cursor.close()
            return True
        except Exception as e:
            print(f"[DB Error] Line index stale-file cleanup failed: {e}")
            return False

    def inherit_analysis(self, source_project, target_project, batch_size=1000):
        """Copy reviewed analysis from an earlier project to unchanged functions in a later project."""
        if not source_project or not target_project or source_project == target_project:
            raise ValueError("source_project and target_project must be different")

        unconfirmed_status = "\u672a\u786e\u8ba4"
        try:
            try:
                self.conn.ping(reconnect=True)
            except AttributeError:
                pass

            cursor = self.conn.cursor()
            source_sql = """
                SELECT si.file_path, si.source_file_name, si.function_hash, si.code_line_hash, si.code_occurrence,
                       a.reviewer, a.status, a.coverage_method, a.uncovered_reason
                FROM coverage_analysis a
                JOIN coverage_line_index si
                  ON si.project_name = a.project_name
                 AND si.file_path_hash = a.file_path_hash
                 AND si.line_number = a.line_number
                WHERE a.project_name = %s
                  AND si.project_name = %s
                  AND si.function_hash <> ''
                  AND si.code_line_hash <> ''
                  AND a.status <> %s
            """
            cursor.execute(source_sql, (source_project, source_project, unconfirmed_status))
            source_rows = cursor.fetchall()

            source_by_key = {}
            ambiguous_keys = set()
            for row in source_rows:
                source_file_name = row_value(row, "source_file_name", 1) or get_source_file_name(row_value(row, "file_path", 0))
                key = (
                    source_file_name,
                    row_value(row, "function_hash", 2),
                    row_value(row, "code_line_hash", 3),
                    row_value(row, "code_occurrence", 4),
                )
                if key in source_by_key:
                    source_by_key.pop(key, None)
                    ambiguous_keys.add(key)
                elif key not in ambiguous_keys:
                    source_by_key[key] = row

            target_sql = """
                SELECT ti.file_path, ti.file_path_hash, ti.source_file_name, ti.line_number,
                       ti.function_hash, ti.code_line_hash, ti.code_occurrence
                FROM coverage_line_index ti
                LEFT JOIN coverage_analysis existing
                  ON existing.project_name = ti.project_name
                 AND existing.file_path_hash = ti.file_path_hash
                 AND existing.line_number = ti.line_number
                WHERE ti.project_name = %s
                  AND ti.function_hash <> ''
                  AND ti.code_line_hash <> ''
                  AND existing.id IS NULL
            """
            cursor.execute(target_sql, (target_project,))
            target_rows = cursor.fetchall()

            payload = []
            ambiguous_skipped = 0
            for row in target_rows:
                target_file_name = row_value(row, "source_file_name", 2) or get_source_file_name(row_value(row, "file_path", 0))
                key = (
                    target_file_name,
                    row_value(row, "function_hash", 4),
                    row_value(row, "code_line_hash", 5),
                    row_value(row, "code_occurrence", 6),
                )
                if key in ambiguous_keys:
                    ambiguous_skipped += 1
                    continue
                source = source_by_key.get(key)
                if not source:
                    continue
                payload.append((
                    target_project,
                    row_value(row, "file_path", 0),
                    row_value(row, "file_path_hash", 1),
                    row_value(row, "line_number", 3),
                    row_value(source, "reviewer", 5) or "",
                    row_value(source, "status", 6) or unconfirmed_status,
                    row_value(source, "coverage_method", 7) or "",
                    row_value(source, "uncovered_reason", 8) or "",
                ))

            insert_sql = """
                INSERT INTO coverage_analysis
                    (project_name, file_path, file_path_hash, line_number,
                     reviewer, status, coverage_method, uncovered_reason)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    file_path = VALUES(file_path)
            """
            inherited = 0
            for start in range(0, len(payload), batch_size):
                batch = payload[start:start + batch_size]
                if not batch:
                    continue
                cursor.executemany(insert_sql, batch)
                self.conn.commit()
                inherited += len(batch)

            cursor.close()
            return {
                "source_project": source_project,
                "target_project": target_project,
                "source_reviewed_records": len(source_rows),
                "target_unfilled_records": len(target_rows),
                "ambiguous_keys": len(ambiguous_keys),
                "ambiguous_skipped_records": ambiguous_skipped,
                "inherited_records": inherited
            }
        except Exception as e:
            print(f"[DB Error] Inherit failed: {e}")
            raise

    def export_report(self, report_type="detail", project_name=None):
        try:
            try:
                self.conn.ping(reconnect=True)
            except AttributeError:
                pass

            cursor = self.conn.cursor()
            where_sql = ""
            params = []
            if project_name:
                where_sql = "WHERE project_name = %s"
                params.append(project_name)

            if report_type == "detail":
                headers = [
                    "project_name", "file_path", "line_number", "reviewer", "status",
                    "coverage_method", "uncovered_reason", "updated_at"
                ]
                sql = f"""
                    SELECT project_name, file_path, line_number, reviewer, status,
                           coverage_method, uncovered_reason, updated_at
                    FROM coverage_analysis
                    {where_sql}
                    ORDER BY project_name, file_path, line_number
                """
                cursor.execute(sql, params)
                rows = cursor.fetchall()
                data = []
                for row in rows:
                    data.append([
                        row_value(row, "project_name", 0),
                        row_value(row, "file_path", 1),
                        row_value(row, "line_number", 2),
                        row_value(row, "reviewer", 3),
                        row_value(row, "status", 4),
                        row_value(row, "coverage_method", 5),
                        row_value(row, "uncovered_reason", 6),
                        row_value(row, "updated_at", 7),
                    ])
            elif report_type == "file_summary":
                headers = [
                    "project_name", "file_path", "review_total", "confirmed_total",
                    "coverable_total", "uncoverable_total", "redundant_total", "unconfirmed_total",
                    "confirmed_rate", "coverable_rate", "uncoverable_rate", "redundant_rate", "last_updated"
                ]
                sql = f"""
                    SELECT project_name, file_path,
                           COUNT(*) AS review_total,
                           SUM(CASE WHEN status <> %s THEN 1 ELSE 0 END) AS confirmed_total,
                           SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS coverable_total,
                           SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS uncoverable_total,
                           SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS redundant_total,
                           SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS unconfirmed_total,
                           ROUND(SUM(CASE WHEN status <> %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS confirmed_rate,
                           ROUND(SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS coverable_rate,
                           ROUND(SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS uncoverable_rate,
                           ROUND(SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS redundant_rate,
                           MAX(updated_at) AS last_updated
                    FROM coverage_analysis
                    {where_sql}
                    GROUP BY project_name, file_path
                    ORDER BY project_name, file_path
                """
                summary_params = ["未确认", "可覆盖", "无法覆盖", "冗余代码", "未确认", "未确认", "可覆盖", "无法覆盖", "冗余代码"] + params
                cursor.execute(sql, summary_params)
                rows = cursor.fetchall()
                data = [[row_value(row, header, idx) for idx, header in enumerate(headers)] for row in rows]
            elif report_type == "project_summary":
                headers = [
                    "project_name", "review_total", "confirmed_total",
                    "coverable_total", "uncoverable_total", "redundant_total", "unconfirmed_total",
                    "confirmed_rate", "coverable_rate", "uncoverable_rate", "redundant_rate", "file_total", "last_updated"
                ]
                sql = f"""
                    SELECT project_name,
                           COUNT(*) AS review_total,
                           SUM(CASE WHEN status <> %s THEN 1 ELSE 0 END) AS confirmed_total,
                           SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS coverable_total,
                           SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS uncoverable_total,
                           SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS redundant_total,
                           SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) AS unconfirmed_total,
                           ROUND(SUM(CASE WHEN status <> %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS confirmed_rate,
                           ROUND(SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS coverable_rate,
                           ROUND(SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS uncoverable_rate,
                           ROUND(SUM(CASE WHEN status = %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS redundant_rate,
                           COUNT(DISTINCT file_path_hash) AS file_total,
                           MAX(updated_at) AS last_updated
                    FROM coverage_analysis
                    {where_sql}
                    GROUP BY project_name
                    ORDER BY project_name
                """
                summary_params = ["未确认", "可覆盖", "无法覆盖", "冗余代码", "未确认", "未确认", "可覆盖", "无法覆盖", "冗余代码"] + params
                cursor.execute(sql, summary_params)
                rows = cursor.fetchall()
                data = [[row_value(row, header, idx) for idx, header in enumerate(headers)] for row in rows]
            elif report_type == "full_detail":
                full_where_sql = ""
                full_params = []
                if project_name:
                    full_where_sql = "WHERE i.project_name = %s"
                    full_params.append(project_name)
                headers = [
                    "project_name", "file_path", "line_number", "line_text",
                    "block_start_line", "block_end_line", "block_type",
                    "fill_status", "status", "reviewer", "coverage_method",
                    "uncovered_reason", "updated_at"
                ]
                sql = f"""
                    SELECT i.project_name, i.file_path, i.line_number, i.line_text,
                           i.block_start_line, i.block_end_line, i.block_type,
                           CASE WHEN a.id IS NULL THEN %s ELSE %s END AS fill_status,
                           COALESCE(a.status, '') AS status,
                           COALESCE(a.reviewer, '') AS reviewer,
                           COALESCE(a.coverage_method, '') AS coverage_method,
                           COALESCE(a.uncovered_reason, '') AS uncovered_reason,
                           a.updated_at
                    FROM coverage_line_index i
                    LEFT JOIN coverage_analysis a
                      ON a.project_name = i.project_name
                     AND a.file_path_hash = i.file_path_hash
                     AND a.line_number = i.line_number
                    {full_where_sql}
                    ORDER BY i.project_name, i.file_path, i.line_number
                """
                cursor.execute(sql, ["未填写", "已填写"] + full_params)
                rows = cursor.fetchall()
                data = [[row_value(row, header, idx) for idx, header in enumerate(headers)] for row in rows]
            elif report_type == "full_file_summary":
                full_where_sql = ""
                full_params = []
                if project_name:
                    full_where_sql = "WHERE i.project_name = %s"
                    full_params.append(project_name)
                headers = [
                    "project_name", "file_path", "total_uncovered", "filled_total",
                    "unfilled_total", "confirmed_total", "coverable_total",
                    "uncoverable_total", "redundant_total", "fill_rate",
                    "confirmed_rate", "last_updated"
                ]
                sql = f"""
                    SELECT i.project_name, i.file_path,
                           COUNT(*) AS total_uncovered,
                           SUM(CASE WHEN a.id IS NULL THEN 0 ELSE 1 END) AS filled_total,
                           SUM(CASE WHEN a.id IS NULL THEN 1 ELSE 0 END) AS unfilled_total,
                           SUM(CASE WHEN a.id IS NOT NULL AND a.status <> %s THEN 1 ELSE 0 END) AS confirmed_total,
                           SUM(CASE WHEN a.status = %s THEN 1 ELSE 0 END) AS coverable_total,
                           SUM(CASE WHEN a.status = %s THEN 1 ELSE 0 END) AS uncoverable_total,
                           SUM(CASE WHEN a.status = %s THEN 1 ELSE 0 END) AS redundant_total,
                           ROUND(SUM(CASE WHEN a.id IS NULL THEN 0 ELSE 1 END) * 100.0 / COUNT(*), 2) AS fill_rate,
                           ROUND(SUM(CASE WHEN a.id IS NOT NULL AND a.status <> %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS confirmed_rate,
                           MAX(a.updated_at) AS last_updated
                    FROM coverage_line_index i
                    LEFT JOIN coverage_analysis a
                      ON a.project_name = i.project_name
                     AND a.file_path_hash = i.file_path_hash
                     AND a.line_number = i.line_number
                    {full_where_sql}
                    GROUP BY i.project_name, i.file_path
                    ORDER BY i.project_name, i.file_path
                """
                summary_params = ["未确认", "可覆盖", "无法覆盖", "冗余代码", "未确认"] + full_params
                cursor.execute(sql, summary_params)
                rows = cursor.fetchall()
                data = [[row_value(row, header, idx) for idx, header in enumerate(headers)] for row in rows]
            elif report_type == "full_project_summary":
                full_where_sql = ""
                full_params = []
                if project_name:
                    full_where_sql = "WHERE i.project_name = %s"
                    full_params.append(project_name)
                headers = [
                    "project_name", "file_total", "total_uncovered", "filled_total",
                    "unfilled_total", "confirmed_total", "coverable_total",
                    "uncoverable_total", "redundant_total", "fill_rate",
                    "confirmed_rate", "last_updated"
                ]
                sql = f"""
                    SELECT i.project_name,
                           COUNT(DISTINCT i.file_path_hash) AS file_total,
                           COUNT(*) AS total_uncovered,
                           SUM(CASE WHEN a.id IS NULL THEN 0 ELSE 1 END) AS filled_total,
                           SUM(CASE WHEN a.id IS NULL THEN 1 ELSE 0 END) AS unfilled_total,
                           SUM(CASE WHEN a.id IS NOT NULL AND a.status <> %s THEN 1 ELSE 0 END) AS confirmed_total,
                           SUM(CASE WHEN a.status = %s THEN 1 ELSE 0 END) AS coverable_total,
                           SUM(CASE WHEN a.status = %s THEN 1 ELSE 0 END) AS uncoverable_total,
                           SUM(CASE WHEN a.status = %s THEN 1 ELSE 0 END) AS redundant_total,
                           ROUND(SUM(CASE WHEN a.id IS NULL THEN 0 ELSE 1 END) * 100.0 / COUNT(*), 2) AS fill_rate,
                           ROUND(SUM(CASE WHEN a.id IS NOT NULL AND a.status <> %s THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS confirmed_rate,
                           MAX(a.updated_at) AS last_updated
                    FROM coverage_line_index i
                    LEFT JOIN coverage_analysis a
                      ON a.project_name = i.project_name
                     AND a.file_path_hash = i.file_path_hash
                     AND a.line_number = i.line_number
                    {full_where_sql}
                    GROUP BY i.project_name
                    ORDER BY i.project_name
                """
                summary_params = ["未确认", "可覆盖", "无法覆盖", "冗余代码", "未确认"] + full_params
                cursor.execute(sql, summary_params)
                rows = cursor.fetchall()
                data = [[row_value(row, header, idx) for idx, header in enumerate(headers)] for row in rows]
            else:
                raise ValueError("Unsupported report type")

            cursor.close()
            return headers, data
        except Exception as e:
            print(f"[DB Error] Export failed: {e}")
            raise


db_manager = None


class CoverageHTTPRequestHandler(BaseHTTPRequestHandler):
    """基于 BaseHTTPRequestHandler 的极轻量跨域 API 服务器"""

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        if parsed_url.path == "/api/coverage/export":
            report_type = query_params.get("type", ["detail"])[0]
            project_name = query_params.get("project", [""])[0] or None

            if report_type not in ("detail", "file_summary", "project_summary", "full_detail", "full_file_summary", "full_project_summary"):
                self.send_error_response(400, "Unsupported export type. Use detail, file_summary, project_summary, full_detail, full_file_summary, or full_project_summary")
                return

            try:
                headers, rows = db_manager.export_report(report_type, project_name)
            except Exception:
                self.send_error_response(500, "Failed to export report")
                return

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            project_part = project_name or "all"
            filename = f"coverage_{report_type}_{project_part}_{timestamp}.csv"
            self.send_csv_response(filename, headers, rows)
        elif parsed_url.path == "/api/coverage":
            project_name = query_params.get("project", [""])[0]
            file_path = query_params.get("file", [""])[0]

            if not project_name or not file_path:
                self.send_error_response(400, "Missing 'project' or 'file' parameter")
                return

            records = db_manager.fetch_records(project_name, file_path)
            self.send_json_response(200, {"status": "success", "records": records})
        else:
            self.send_error_response(404, "Not Found")

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        if parsed_url.path == "/api/coverage":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            
            try:
                payload = json.loads(post_data.decode("utf-8"))
            except ValueError:
                self.send_error_response(400, "Invalid JSON data")
                return

            project_name = payload.get("project_name")
            file_path = payload.get("file_path")
            line_number = payload.get("line_number")
            reviewer = payload.get("reviewer", "")
            status = payload.get("status", "未确认")
            method = payload.get("coverage_method", "")
            reason = payload.get("uncovered_reason", "")

            if not project_name or not file_path or line_number is None:
                self.send_error_response(400, "Missing required parameters (project_name, file_path, line_number)")
                return

            success = db_manager.save_record(project_name, file_path, line_number, reviewer, status, method, reason)
            if success:
                self.send_json_response(200, {"status": "success", "message": "Record saved successfully"})
            else:
                self.send_error_response(500, "Failed to save record to database")
        else:
            self.send_error_response(404, "Not Found")

    def send_json_response(self, status_code, data):
        self.send_response(status_code)
        self.send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def send_csv_response(self, filename, headers, rows):
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(["" if value is None else value for value in row])
        data = ("\ufeff" + output.getvalue()).encode("utf-8")

        safe_filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename)
        self.send_response(200)
        self.send_cors_headers()
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{safe_filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_error_response(self, status_code, message):
        self.send_json_response(status_code, {"status": "error", "message": message})


def inject_coverage_report(input_dir, output_dir):
    """
    非破坏性注入覆盖率报告：
    1. 若 output_dir 与 input_dir 不同，则先自动将整个 input_dir 复制至 output_dir (清除已有的 output_dir)
    2. 在 output_dir 中注入样式表、增强脚本并复制静态资源。
    """
    if not os.path.exists(input_dir):
        print(f"[Error] Input directory '{input_dir}' does not exist.")
        return

    real_input_html = os.path.join(input_dir, "html") if os.path.exists(os.path.join(input_dir, "html")) else input_dir
    real_output_html = os.path.join(output_dir, "html") if os.path.exists(os.path.join(input_dir, "html")) else output_dir

    if input_dir != output_dir:
        print(f"[Injector] Copying input directory '{input_dir}' to '{output_dir}' (protecting original files)...")
        try:
            if os.path.exists(output_dir):
                shutil.rmtree(output_dir)
            shutil.copytree(input_dir, output_dir)
            print("[Injector] Copy complete.")
        except Exception as e:
            print(f"[Error] Failed to copy directory: {e}")
            return
    else:
        print("[Warning] Input and output directories are the same. Original files will be modified.")

    if not os.path.exists(real_output_html):
        print(f"[Error] Real output html directory '{real_output_html}' does not exist.")
        return

    shutil.copy2(JS_SOURCE_PATH, os.path.join(real_output_html, "coverage_enhance.js"))
    shutil.copy2(CSS_SOURCE_PATH, os.path.join(real_output_html, "coverage_enhance.css"))
    print(f"[Injector] Copied static resources to: {real_output_html}")

    config = load_config()
    project_name = config.get("project_name", "Gemini-NOS")
    index_manager = None
    indexed_records = 0
    indexed_files = 0
    active_file_hashes = set()
    try:
        index_manager = DatabaseManager(config, exit_on_error=False)
    except Exception as e:
        print(f"[Warning] Failed to initialize database for full coverage line index: {e}")
        print("[Warning] Inject will continue, but full export requires a successful line-index sync.")

    print(f"[Injector] Scanning report files under: {real_output_html}")
    gcov_files = []
    for root, dirs, files in os.walk(real_output_html):
        dirs.sort()
        for file in sorted(files):
            if file.endswith(".gcov.html"):
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, real_output_html)
                gcov_files.append((file_path, rel_path))

    total_files = len(gcov_files)
    if total_files == 0:
        print("[Injector] No .gcov.html files found. Nothing to inject.")
        if index_manager and index_manager.conn:
            index_manager.conn.close()
        return

    print(f"[Injector] Found {total_files} .gcov.html file(s). Starting injection and line-index sync...")
    started_at = time.time()
    injected_count = 0
    updated_count = 0
    for file_index, (file_path, rel_path) in enumerate(gcov_files, start=1):
        depth = len(rel_path.split(os.sep)) - 1
        prefix = "../" * depth

        css_tag = f'<link rel="stylesheet" type="text/css" href="{prefix}coverage_enhance.css?v={ASSET_VERSION}">\n'
        js_tag = f'<script type="text/javascript" src="{prefix}coverage_enhance.js?v={ASSET_VERSION}"></script>\n'
        inject_code = f"{css_tag}{js_tag}</head>"

        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        report_file_path = extract_report_file_path(content, rel_path)
        report_file_hash = calc_file_path_hash(report_file_path)
        active_file_hashes.add(report_file_hash)
        file_line_index_records = extract_line_index_records(content, rel_path, project_name)
        file_index_synced = False
        if index_manager:
            if file_line_index_records and index_manager.sync_line_index(project_name, file_line_index_records):
                indexed_records += len(file_line_index_records)
                indexed_files += 1
                file_index_synced = True
            elif not file_line_index_records:
                index_manager.delete_line_index_file(project_name, report_file_hash)

        if "coverage_enhance.js" in content:
            new_content = re.sub(r'(href="[^"]*coverage_enhance\.css)(?:\?v=[^"]*)?(")', rf'\1?v={ASSET_VERSION}\2', content)
            new_content = re.sub(r'(src="[^"]*coverage_enhance\.js)(?:\?v=[^"]*)?(")', rf'\1?v={ASSET_VERSION}\2', new_content)
            if new_content != content:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                updated_count += 1
        elif "</head>" in content:
            new_content = content.replace("</head>", inject_code, 1)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            injected_count += 1

        elapsed = time.time() - started_at
        percent = file_index * 100.0 / total_files
        rate = file_index / elapsed if elapsed > 0 else 0
        remaining = (total_files - file_index) / rate if rate > 0 else 0
        index_status = "synced" if file_index_synced else "empty" if not file_line_index_records else "skipped"
        print(
            f"[Injector] Progress {file_index}/{total_files} ({percent:.1f}%) "
            f"elapsed={elapsed:.1f}s eta={remaining:.1f}s "
            f"uncovered={len(file_line_index_records)} index={index_status} "
            f"total_indexed={indexed_records} file={rel_path}",
            flush=True
        )

    print(f"[Injector] Non-destructively enhanced {injected_count} new html report file(s), updated {updated_count} existing enhanced file(s) in: {output_dir}")
    if index_manager:
        index_manager.prune_line_index_project_files(project_name, active_file_hashes)
        if index_manager.conn:
            index_manager.conn.close()
        print(f"[Injector] Synced full coverage line index: {indexed_records} record(s) across {indexed_files} file(s).")
    else:
        print("[Injector] Full coverage line index was not synced because database initialization failed.")


def run_server():
    global db_manager
    config = load_config()

    print("[Server] Initializing MySQL Database...")
    db_manager = DatabaseManager(config)

    host = config["server"]["host"]
    port = int(config["server"]["port"])
    server_address = (host, port)

    httpd = HTTPServer(server_address, CoverageHTTPRequestHandler)
    print(f"[Server] Microservice running on http://{host}:{port} ...")
    print("[Server] Press Ctrl+C to terminate.")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[Server] Shutting down gracefully...")
        if db_manager and db_manager.conn:
            db_manager.conn.close()
        httpd.server_close()
        print("[Server] Stopped.")


def print_help():
    print("Usage:")
    print("  python scripts/enhance_coverage.py inject --dir <input_dir> --out <output_dir>")
    print("    - Scan and inject custom interactive forms into HTML reports.")
    print("  python scripts/enhance_coverage.py server")
    print("    - Start local bridge server for MySQL persistence.")
    print("  python scripts/enhance_coverage.py inherit --from <old_project> --to <new_project>")
    print("    - Reuse reviewed analysis for unchanged functions in a later project/version.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print_help()
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "inject":
        dir_path = None
        out_path = None
        for i in range(len(sys.argv)):
            if sys.argv[i] == "--dir" and i + 1 < len(sys.argv):
                dir_path = sys.argv[i + 1]
            if sys.argv[i] == "--out" and i + 1 < len(sys.argv):
                out_path = sys.argv[i + 1]

        if not dir_path:
            dir_path = os.path.join(SCRIPT_DIR, "../build/coverage")
        if not out_path:
            if "build/coverage" in dir_path or "build\\coverage" in dir_path:
                out_path = os.path.join(os.path.dirname(dir_path), "coverage_review")
            else:
                out_path = dir_path + "_review"
            
        print(f"[Main] Non-destructive injection starts.")
        print(f"[Main] Input (ReadOnly) : {dir_path}")
        print(f"[Main] Output (Enhanced) : {out_path}")
        inject_coverage_report(dir_path, out_path)
    elif cmd == "server":
        run_server()
    elif cmd == "inherit":
        source_project = None
        target_project = None
        for i in range(len(sys.argv)):
            if sys.argv[i] == "--from" and i + 1 < len(sys.argv):
                source_project = sys.argv[i + 1]
            if sys.argv[i] == "--to" and i + 1 < len(sys.argv):
                target_project = sys.argv[i + 1]

        if not source_project or not target_project:
            print("[Error] inherit requires --from <old_project> and --to <new_project>.")
            print_help()
            sys.exit(1)

        config = load_config()
        print(f"[Inherit] Source project: {source_project}")
        print(f"[Inherit] Target project: {target_project}")
        manager = DatabaseManager(config)
        result = manager.inherit_analysis(source_project, target_project)
        if manager.conn:
            manager.conn.close()
        print("[Inherit] Completed.")
        print(f"[Inherit] Source reviewed records: {result['source_reviewed_records']}")
        print(f"[Inherit] Target unfilled records: {result['target_unfilled_records']}")
        print(f"[Inherit] Ambiguous source keys skipped: {result['ambiguous_keys']}")
        print(f"[Inherit] Target records skipped by ambiguity: {result['ambiguous_skipped_records']}")
        print(f"[Inherit] Inherited records: {result['inherited_records']}")
    else:
        print_help()
        sys.exit(1)
