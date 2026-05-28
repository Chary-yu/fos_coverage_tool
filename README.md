# Coverage Tool 部署与使用指南

本工具用于增强 LCOV/gcov 生成的 HTML 覆盖率报告，在未覆盖代码行旁边提供人工分析控件，并将填写结果保存到 MySQL/MariaDB。适用于内网 Linux 服务器部署，用户通过 Windows 浏览器访问网页。

工具特点：

* 不修改业务源码；
* 不破坏原始覆盖率报告，建议输出到新目录；
* 支持多项目、多分支、多版本共用同一套服务；
* 支持按 `project_name` 隔离数据；
* 支持新版本从旧版本继承未变化函数的分析结论；
* 支持导出填写明细、文件汇总、项目汇总和全量进度报表。

---

## 1. 目录说明

核心文件：

```text
/opt/coverage_tool/
  enhance_coverage.py        # 后台脚本：注入、启动服务、导出、继承
  coverage_enhance.js        # 前端增强脚本
  coverage_enhance.css       # 前端样式
  coverage_config.json       # 数据库、服务端口、项目名配置
```

推荐的报告目录结构：

```text
/opt/coverage_tool/
  review_main_202605/
    html/index.html
  review_main_202606/
    html/index.html
```

每个 `review_xxx` 目录对应一个版本、分支或项目的增强覆盖率网页。

---

## 2. 环境准备

目标服务器需要：

* Linux；
* Python 3；
* MySQL 或 MariaDB；
* Nginx；
* Python MySQL 驱动。

安装 Python 驱动：

```bash
pip3 install pymysql
```

如果内网有 PyPI 镜像，按公司镜像源安装即可。

---

## 3. 配置数据库

编辑 `/opt/coverage_tool/coverage_config.json`：

```json
{
  "mysql": {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "coverage_user",
    "password": "你的数据库密码",
    "database": "coverage"
  },
  "server": {
    "host": "127.0.0.1",
    "port": 9528
  },
  "project_name": "review_main_202605"
}
```

说明：

* `mysql.database` 可以多个项目共用一个库；
* `project_name` 是数据隔离的关键字段；
* 每个版本、分支或项目建议使用不同的 `project_name`；
* `server.host` 建议使用 `127.0.0.1`，由 Nginx 反向代理给浏览器访问。

首次启动或注入时，脚本会自动建库、建表并升级表结构。

---

## 4. 生成增强覆盖率网页

假设原始 LCOV HTML 报告在：

```text
/opt/coverage_reports/raw_main_202605
```

执行：

```bash
cd /opt/coverage_tool
python3 enhance_coverage.py inject \
  --dir /opt/coverage_reports/raw_main_202605 \
  --out /opt/coverage_tool/review_main_202605
```

执行后会生成：

```text
/opt/coverage_tool/review_main_202605/
  html/index.html
  html/coverage_enhance.js
  html/coverage_enhance.css
```

`inject` 会做三件事：

* 将原始报告复制到 `--out` 指定目录；
* 注入前端 JS/CSS 控件；
* 将未覆盖行索引同步到数据库，用于全量导出和跨版本继承。

注意：执行 `inject` 前，请确认 `coverage_config.json` 中的 `project_name` 已经改成当前版本对应的名字。

---

## 5. 启动后台服务

执行：

```bash
cd /opt/coverage_tool
python3 enhance_coverage.py server
```

看到类似输出表示启动成功：

```text
[Server] Microservice running on http://127.0.0.1:9528 ...
```

后台服务只需要启动一个。多个网页、多个版本都通过同一个 `/api/coverage` 接口访问后台，后台根据 `project_name` 区分数据。

建议生产环境使用 `systemd` 管理该服务，避免终端关闭后服务退出。

---

## 6. Nginx 配置

Nginx 可以同时提供多个覆盖率网页。推荐让 `/coverage/` 指向 `/opt/coverage_tool/`。

示例配置：

```nginx
server {
    listen 80;
    server_name _;

    location /coverage/ {
        alias /opt/coverage_tool/;
        index index.html;
        try_files $uri $uri/ =404;

        # 按公司办公网段调整。
        allow 10.190.0.0/16;
        allow 127.0.0.1;
        deny all;
    }

    location /api/coverage {
        proxy_pass http://127.0.0.1:9528/api/coverage;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

检查并重载：

```bash
nginx -t
systemctl reload nginx
```

访问示例：

```text
http://服务器IP/coverage/review_main_202605/html/index.html
http://服务器IP/coverage/review_main_202606/html/index.html
```

快速检查：

```bash
curl -I http://127.0.0.1/coverage/review_main_202605/html/index.html
curl -I http://127.0.0.1/coverage/review_main_202605/html/coverage_enhance.js
curl -I http://127.0.0.1/coverage/review_main_202605/html/coverage_enhance.css
```

如果返回 `200 OK`，说明静态资源可以访问。

---

## 7. 多版本或多项目使用方式

核心原则：

* 网页目录隔离展示；
* 数据库用 `project_name` 隔离数据；
* 一个版本、分支或项目使用一个独立 `project_name`。

示例：

```text
旧版本目录：/opt/coverage_tool/review_main_202605
旧版本项目名：review_main_202605

新版本目录：/opt/coverage_tool/review_main_202606
新版本项目名：review_main_202606
```

新版本操作：

1. 修改 `coverage_config.json`

```json
{
  "project_name": "review_main_202606"
}
```

保持 `mysql` 和 `server` 配置不变，只改 `project_name`。

2. 执行新版本注入

```bash
cd /opt/coverage_tool
python3 enhance_coverage.py inject \
  --dir /opt/coverage_reports/raw_main_202606 \
  --out /opt/coverage_tool/review_main_202606
```

3. 打开新版本网页

```text
http://服务器IP/coverage/review_main_202606/html/index.html
```

如果是全新项目，没有历史数据，执行到这里即可开始填写。

---

## 8. 跨版本继承分析结果

如果后一版本的函数没有变化，并且仍然有未覆盖行，可以继承前一版本的分析结果。

继承前提：

* 旧版本已经完成 `inject`；
* 新版本也已经完成 `inject`；
* 两个版本使用不同的 `project_name`；
* 旧版本已经有人工填写过的记录。

继承示例：

```bash
cd /opt/coverage_tool
python3 enhance_coverage.py inherit \
  --from review_main_202605 \
  --to review_main_202606
```

继承规则：

* 只继承新版本仍然未覆盖的行；
* 只继承新版本尚未填写的行，不覆盖人工已填写的新结论；
* 只在同一文件、同一函数内容 hash、同一代码文本和函数内出现顺序一致时继承；
* 函数内容发生变化时不会自动继承；
* 旧版本状态为“未确认”的记录不会继承。

推荐流程：

```bash
# 旧版本：project_name=review_main_202605
python3 enhance_coverage.py inject \
  --dir /opt/coverage_reports/raw_main_202605 \
  --out /opt/coverage_tool/review_main_202605

# 新版本：先把 coverage_config.json 改为 project_name=review_main_202606
python3 enhance_coverage.py inject \
  --dir /opt/coverage_reports/raw_main_202606 \
  --out /opt/coverage_tool/review_main_202606

# 继承旧版本结论
python3 enhance_coverage.py inherit \
  --from review_main_202605 \
  --to review_main_202606
```

---

## 9. 导出数据

启动后台服务后，可以通过 HTTP 导出 CSV。CSV 使用 UTF-8 BOM，Excel 可直接打开。

导出已填写明细：

```bash
curl -o coverage_detail.csv \
  "http://127.0.0.1:9528/api/coverage/export?type=detail"
```

导出文件维度汇总：

```bash
curl -o coverage_file_summary.csv \
  "http://127.0.0.1:9528/api/coverage/export?type=file_summary"
```

导出项目维度汇总：

```bash
curl -o coverage_project_summary.csv \
  "http://127.0.0.1:9528/api/coverage/export?type=project_summary"
```

导出全量明细，包含未填写行：

```bash
curl -o coverage_full_detail.csv \
  "http://127.0.0.1:9528/api/coverage/export?type=full_detail"
```

导出全量文件汇总：

```bash
curl -o coverage_full_file_summary.csv \
  "http://127.0.0.1:9528/api/coverage/export?type=full_file_summary"
```

导出全量项目汇总：

```bash
curl -o coverage_full_project_summary.csv \
  "http://127.0.0.1:9528/api/coverage/export?type=full_project_summary"
```

只导出某个项目：

```bash
curl -o review_main_202606_full_project_summary.csv \
  "http://127.0.0.1:9528/api/coverage/export?type=full_project_summary&project=review_main_202606"
```

如果通过 Nginx 访问，将地址换成：

```bash
curl -o coverage_full_project_summary.csv \
  "http://服务器IP/api/coverage/export?type=full_project_summary"
```

---

## 10. 常见问题排查

### 网页能打开，但控件没有显示

检查 JS/CSS 是否能访问：

```bash
curl -I http://127.0.0.1/coverage/review_main_202606/html/coverage_enhance.js
curl -I http://127.0.0.1/coverage/review_main_202606/html/coverage_enhance.css
```

如果是 `403 Forbidden`，通常是 Nginx 白名单、目录权限或 `alias` 路径配置问题。

### 保存按钮显示 Offline

检查后台服务是否启动：

```bash
ps -ef | grep enhance_coverage.py
curl -I http://127.0.0.1:9528/api/coverage
```

检查 Nginx 是否代理 `/api/coverage`：

```bash
curl -I http://127.0.0.1/api/coverage
```

### 修改 JS/CSS 后浏览器还是旧效果

强制刷新浏览器缓存：

```text
Ctrl + F5
```

或者重新执行 `inject`，脚本会更新资源版本参数。

### 数据串到其他版本

重点检查：

* 执行 `inject` 前 `coverage_config.json` 的 `project_name` 是否正确；
* 旧版本和新版本是否使用了不同的 `project_name`；
* 浏览器打开的是否是对应版本目录。

### 数据库提示 key too long

新版脚本已经使用 `file_path_hash` 和辅助索引规避长路径索引问题。升级后重新运行：

```bash
python3 enhance_coverage.py server
```

或重新执行一次：

```bash
python3 enhance_coverage.py inject --dir <raw_dir> --out <review_dir>
```

脚本会自动补齐表结构。

---

## 11. 安全建议

* 不建议将服务直接暴露到公网；
* Nginx 建议配置办公网段白名单；
* `coverage_config.json` 中包含数据库密码，不要提交到公共仓库；
* Python 服务建议只监听 `127.0.0.1`，由 Nginx 对外代理；
* 公司 IT 扫描网站漏洞时，重点说明这是内网静态报告页面加本地 API 持久化服务，无用户登录，无公网访问。

---

## 12. 最小操作清单

新版本从零到可用：

```bash
cd /opt/coverage_tool

# 1. 修改 coverage_config.json 中 project_name
vi coverage_config.json

# 2. 注入新版本报告
python3 enhance_coverage.py inject \
  --dir /opt/coverage_reports/raw_main_202606 \
  --out /opt/coverage_tool/review_main_202606

# 3. 启动后台服务
python3 enhance_coverage.py server

# 4. 如果需要继承旧版本
python3 enhance_coverage.py inherit \
  --from review_main_202605 \
  --to review_main_202606
```

访问：

```text
http://服务器IP/coverage/review_main_202606/html/index.html
```
