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
ASSET_VERSION = "dop-lineNum-rowfix-20260526"


def calc_file_path_hash(file_path):
    """Return a stable compact key for the full report file path."""
    return hashlib.md5(str(file_path).encode("utf-8")).hexdigest()


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
    def __init__(self, config):
        self.config = config["mysql"]
        if not db_module:
            print("[CRITICAL] Missing MySQL driver. Please install PyMySQL to enable database support:")
            print("           pip install pymysql")
            sys.exit(1)
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

            cursor.close()
            print("[DB] Database initialization complete.")
        except Exception as e:
            print(f"[CRITICAL] Database initialization failed: {e}")
            sys.exit(1)

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
        if parsed_url.path == "/api/coverage":
            query_params = urllib.parse.parse_qs(parsed_url.query)
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

    injected_count = 0
    for root, dirs, files in os.walk(real_output_html):
        for file in files:
            if file.endswith(".gcov.html"):
                file_path = os.path.join(root, file)
                
                rel_path = os.path.relpath(file_path, real_output_html)
                depth = len(rel_path.split(os.sep)) - 1
                prefix = "../" * depth

                css_tag = f'<link rel="stylesheet" type="text/css" href="{prefix}coverage_enhance.css?v={ASSET_VERSION}">\n'
                js_tag = f'<script type="text/javascript" src="{prefix}coverage_enhance.js?v={ASSET_VERSION}"></script>\n'
                inject_code = f"{css_tag}{js_tag}</head>"

                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()

                if "coverage_enhance.js" in content:
                    new_content = re.sub(r'(href="[^"]*coverage_enhance\.css)(?:\?v=[^"]*)?(")', rf'\1?v={ASSET_VERSION}\2', content)
                    new_content = re.sub(r'(src="[^"]*coverage_enhance\.js)(?:\?v=[^"]*)?(")', rf'\1?v={ASSET_VERSION}\2', new_content)
                    if new_content != content:
                        with open(file_path, 'w', encoding='utf-8') as f:
                            f.write(new_content)
                    continue

                if "</head>" in content:
                    new_content = content.replace("</head>", inject_code, 1)
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(new_content)
                    injected_count += 1

    print(f"[Injector] Non-destructively enhanced {injected_count} html report file(s) in: {output_dir}")


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
    else:
        print_help()
        sys.exit(1)
