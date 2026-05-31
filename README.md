# Coverage Tool 部署与使用指南

本工具用于增强 LCOV/gcov 生成的 HTML 覆盖率报告，在未覆盖代码行旁边提供人工分析控件，并将填写结果保存到 MySQL/MariaDB。适用于内网 Linux 服务器部署，用户通过 Windows 浏览器访问网页。

工具特点：

* 不修改业务源码；
* 不破坏原始覆盖率报告，建议输出到新目录；
* 同一作用域内连续的简单未覆盖赋值/声明语句会合并为一个分析块，避免重复填写相同结论；
* 遇到 `if/else/for/while/switch/case/default`、`{}`、`return/goto/break/continue`、已覆盖执行行或复杂语句会保守拆分；
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
  clear_coverage_data.py     # 调试脚本：清空单项目或全部数据库数据
  coverage_progress.html     # 独立网页：查看项目/目录/文件分析进度
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
  "worker_threads": 4,
  "render_mode": "lazy",
  "project_name": "review_main_202605"
}
```

说明：

* `mysql.database` 可以多个项目共用一个库；
* `project_name` 是兼容旧流程的默认项目名；
* 推荐执行 `inject` 时用 `--project <项目名>` 显式指定项目名，避免忘记修改配置文件；
* `worker_threads` 控制注入写库和按目录导出 Excel 的并发线程数，建议从 4 开始，数据库压力允许时再调大；
* `render_mode` 控制覆盖率页面右侧控件的默认显示方式，`lazy` 为轻量占位、点击展开，`immediate` 为打开页面后直接渲染完整控件；
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
  --project review_main_202605 \
  --dir /opt/coverage_reports/raw_main_202605 \
  --out /opt/coverage_tool/review_main_202605 \
  --mode lazy \
  --workers 4
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
* 按 `--mode` 或 `coverage_config.json` 中的 `render_mode` 写入前端控件显示模式；
* 将未覆盖行索引同步到数据库，用于全量导出和跨版本继承。

执行过程中会输出进度，例如：

```text
[Injector] Found 1200 .gcov.html file(s). Starting injection and line-index sync...
[Injector] Progress 38/1200 (3.2%) elapsed=18.4s eta=563.1s uncovered=42 index=synced total_indexed=12580 file=xxx/yyy.c.gcov.html
```

字段含义：

* `Progress`：当前处理文件数 / 总文件数；
* `uncovered`：当前文件识别到的未覆盖行索引数；
* `index`：当前文件索引是否已同步到数据库，`synced` 表示已同步；
* `total_indexed`：本次累计同步的未覆盖行索引数；
* `eta`：按当前处理速度估算的剩余时间。

注意：建议每次执行 `inject` 都显式传入 `--project <项目名>`。脚本会拒绝缺少项目名的注入命令，避免误用 `coverage_config.json` 中的旧项目名。

控件显示模式说明：

* `--mode lazy`：默认推荐。页面先显示轻量 `分析` 占位按钮，点击后再展开完整输入框，适合未覆盖块很多的大文件；
* `--mode immediate`：打开页面后直接渲染完整输入框，适合文件较小或希望保持旧交互习惯的项目；
* 未传 `--mode` 时使用 `coverage_config.json` 中的 `render_mode`，配置不存在或非法时默认使用 `lazy`；
* 临时查看时也可以在网页 URL 后追加 `?mode=lazy` 或 `?mode=immediate` 覆盖默认模式；如果 URL 已经带有其他参数，则使用 `&mode=lazy` 或 `&mode=immediate`；
* 覆盖率源码页右下角也提供显示模式切换器，可以在当前页面快速切换 `lazy` / `immediate`。

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

新版服务端使用多线程 HTTP 服务，并为每个工作线程维护独立数据库连接。这样进度页、保存请求和导出请求可以并发处理，避免一个较慢的导出请求阻塞其他网页操作。

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

1. 确认新版本项目名

项目名直接通过命令行传入，不需要为了切换版本反复修改 `coverage_config.json`。

2. 执行新版本注入

```bash
cd /opt/coverage_tool
python3 enhance_coverage.py inject \
  --project review_main_202606 \
  --dir /opt/coverage_reports/raw_main_202606 \
  --out /opt/coverage_tool/review_main_202606 \
  --mode lazy
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
* 只继承新版本尚未填写的行，或状态仍为“未确认”且填写内容为空的行；
* 不覆盖新版本中人工已经填写过的有效结论；
* 跨项目继承按源文件名匹配，例如两个路径都以 `foo.c` 结尾即可进入匹配；
* 项目内要求 C 源文件名不重复，否则同名文件可能被判定为歧义并跳过；
* 匹配项还必须满足函数内容 hash、代码文本 hash 和函数内出现顺序一致；
* 如果旧版本中出现多个无法唯一判断的匹配项，会跳过这些记录，避免误继承；
* 函数内容发生变化时不会自动继承；
* 旧版本状态为“未确认”的记录不会继承。

继承完成后会输出诊断信息，例如：

```text
[Inherit] Source analysis records: 320
[Inherit] Source reviewed analysis records: 240
[Inherit] Source index records: 1800
[Inherit] Source hashable index records: 1700
[Inherit] Source reviewed records joined with index: 220
[Inherit] Target index records: 1900
[Inherit] Target hashable index records: 1810
[Inherit] Target unfilled records: 1810
[Inherit] Filename matched records: 205
[Inherit] Inherited records: 205
```

如果 `Source reviewed records joined with index` 为 0，通常说明旧版本没有用新版脚本重新执行过 `inject`，或者旧版本填写数据保存到了错误的 `project_name`。如果 `Target hashable index records` 为 0，通常说明新版本没有完成 `inject` 或函数识别失败。

推荐流程：

```bash
# 旧版本
python3 enhance_coverage.py inject \
  --project review_main_202605 \
  --dir /opt/coverage_reports/raw_main_202605 \
  --out /opt/coverage_tool/review_main_202605

# 新版本
python3 enhance_coverage.py inject \
  --project review_main_202606 \
  --dir /opt/coverage_reports/raw_main_202606 \
  --out /opt/coverage_tool/review_main_202606

# 继承旧版本结论
python3 enhance_coverage.py inherit \
  --from review_main_202605 \
  --to review_main_202606
```

---

## 9. 导出数据

启动后台服务后，可以通过 HTTP 导出 CSV 或 Excel。CSV 使用 UTF-8 BOM，Excel 可直接打开。

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

导出全量目录汇总：

```bash
curl -o coverage_full_dir_summary.csv \
  "http://127.0.0.1:9528/api/coverage/export?type=full_dir_summary"
```

导出全量项目汇总：

```bash
curl -o coverage_full_project_summary.csv \
  "http://127.0.0.1:9528/api/coverage/export?type=full_project_summary"
```

一次导出项目、目录、文件三个层级的分析进度：

```bash
curl -o coverage_full_progress_summary.csv \
  "http://127.0.0.1:9528/api/coverage/export?type=full_progress_summary&project=review_main_202606"
```

`full_progress_summary` 包含：

* `level=project`：整个项目分析进度；
* `level=dir`：各目录分析进度；
* `level=file`：各文件分析进度。

主要进度字段：

* `total_uncovered`：未覆盖行总数；
* `filled_total` / `unfilled_total`：已填写 / 未填写数量；
* `confirmed_total`：状态不是“未确认”的数量；
* `coverable_total` / `uncoverable_total` / `redundant_total`：可覆盖 / 无法覆盖 / 冗余代码数量；
* `fill_rate` / `confirmed_rate`：填写率 / 确认率。

导出评审模板 Excel：

```bash
curl -o review_main_202606.xlsx \
  "http://127.0.0.1:9528/api/coverage/export?type=review_excel&project=review_main_202606"
```

按单个代码目录导出评审模板 Excel：

```bash
curl -o review_main_202606_dir.xlsx \
  "http://127.0.0.1:9528/api/coverage/export?type=review_excel&project=review_main_202606&dir=src/module_a"
```

按代码目录拆分导出评审模板压缩包：

```bash
curl -o review_main_202606_by_dir.zip \
  "http://127.0.0.1:9528/api/coverage/export?type=review_excel_by_dir&project=review_main_202606"
```

`review_excel` 和 `review_excel_by_dir` 说明：

* 每个源文件一个 sheet，sheet 名为源文件名；
* 明细列为“行号、代码行、覆盖率标识、是否冗余代码，剔除计划、对测试覆盖的建议、无法覆盖原因、开发责任人”；
* 单目录导出时，Excel 只包含该目录下的源文件 sheet，进度 sheet 也只保留该目录和该目录内文件；
* `review_excel_by_dir` 返回 zip，每个代码目录一个 `.xlsx`，避免单个 Excel 过大；
* 按目录导出会先批量查询项目、目录、文件和明细数据，再在内存中按目录分组并并发生成各目录 Excel，减少重复数据库查询；
* 如果浏览器或 `curl` 中途取消下载，服务端会记录断连日志并停止继续写响应，后台服务不会因此退出；
* `review_excel` 和 `review_excel_by_dir` 都必须指定 `project=<项目名>`。

如果下载到的文件很小或不是 zip/xlsx，先检查 `type` 是否拼写为 `review_excel_by_dir`，以及项目是否已经重新执行过 `inject` 同步 `coverage_line_index`。当项目没有可导出的行索引时，zip 中会包含 `README.txt` 说明原因。

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

## 10. 网页查看分析进度

分析进度使用独立网页查看，不嵌入每个覆盖率源码页面。

执行 `inject` 后，脚本会把 `coverage_progress.html` 复制到对应报表目录。例如：

```text
http://服务器IP/coverage/review_main_202606/coverage_progress.html?project=review_main_202606
```

同时也会在 `html/coverage_progress.html` 放一份，兼容只暴露 HTML 子目录的部署方式：

```text
http://服务器IP/coverage/review_main_202606/html/coverage_progress.html?project=review_main_202606
```

如果直接把 `/opt/coverage_tool/` 暴露到 `/coverage/`，也可以访问工具根目录下的页面：

```text
http://服务器IP/coverage/coverage_progress.html?project=review_main_202606
```

如果部署时漏拷了工具目录下的 `coverage_progress.html`，`inject` 会自动生成一个内置版进度页面，并在控制台打印 warning。

如果页面能打开但点击“查看进度”后连不上接口，可以显式指定后台 API 地址：

```text
http://服务器IP/coverage/review_main_202606/coverage_progress.html?project=review_main_202606&api=http://服务器IP:9528/api/coverage
```

进度页会按顺序尝试多个 API 地址：

* URL 中 `api=` 指定的地址；
* 当前域名下的 `/api/coverage`；
* 当前主机的 `:9528/api/coverage`；
* `http://127.0.0.1:9528/api/coverage`；
* 相对路径 `/api/coverage`。

页面顶部会显示正在尝试的接口地址；如果全部失败，会把已尝试地址展示出来，便于定位 Nginx 代理、端口或跨机器访问问题。

页面默认展示：

* 项目未覆盖行总数；
* 已填写数量；
* 填写率；
* 确认率。

同时提供：

* 各目录进度；
* 各文件进度；
* 进度 CSV 导出入口；
* 按目录拆分的 Excel ZIP 导出入口。

该页面依赖后台服务提供的接口：

```text
/api/coverage/progress?project=<project_name>
```

如果已经生成过旧报表，需要重新执行一次 `inject`，把新的 `coverage_progress.html` 复制到报表目录，并更新覆盖率页面使用的 JS/CSS 版本。

---

## 11. 清空调试数据

需要从零开始调试时，可以清空数据库中本工具维护的数据。脚本会读取同目录下的 `coverage_config.json`。

建议优先清空单个项目，确认无误后再使用全量清空。

只清空某个项目：

```bash
python3 clear_coverage_data.py --project review_main_202606 --yes
```

清空全部项目：

```bash
python3 clear_coverage_data.py --all --yes
```

为了避免误操作，脚本不带 `--yes` 会拒绝执行。

典型调试流程：

```bash
# 1. 清空新版本项目数据
python3 clear_coverage_data.py --project review_main_202606 --yes

# 2. 重新注入新版本报告并重建行索引
python3 enhance_coverage.py inject \
  --project review_main_202606 \
  --dir /opt/coverage_reports/raw_main_202606 \
  --out /opt/coverage_tool/review_main_202606

# 3. 如需复测继承，再执行继承命令
python3 enhance_coverage.py inherit \
  --from review_main_202605 \
  --to review_main_202606
```

---

## 12. 常见问题排查

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

### 单个 HTML 文件代码很多，打开很慢

大文件变慢通常有两部分原因：

* LCOV 原始 HTML 本身很大，浏览器解析和渲染代码需要时间；
* 增强脚本需要扫描未覆盖行并创建分析入口。

新版前端支持两种控件显示模式。默认推荐 `lazy`，大文件打开时页面只会先在未覆盖代码块右侧生成一个很小的 `分析` 按钮；点击某一行的 `分析` 按钮后，才会展开状态、确认人、覆盖建议、无法覆盖原因和保存按钮。若执行 `inject` 时使用 `--mode immediate`，则打开页面后直接显示完整输入框。

当分析块较多时，右上角会显示类似进度：

```text
Coverage controls: 800/3200 (25.0%)
```

如果数据库中已有填写结果，占位按钮会直接显示 `可覆盖`、`无法覆盖`、`冗余代码` 等状态。点击后展开的完整输入框会自动带出已有内容。

如果想临时切换当前网页的控件模式，可以使用页面右下角的显示模式切换器，或在 URL 后追加：

```text
?mode=lazy
?mode=immediate
```

如果 URL 已经带有其他查询参数，则改用：

```text
&mode=lazy
&mode=immediate
```

如果仍然明显卡顿，建议从源头拆分覆盖率报告，例如按模块、目录或子工程分别生成 LCOV HTML，再分别执行 `inject`。这样每个 `.gcov.html` 页面更小，浏览器体验会明显更稳。

### 数据串到其他版本

重点检查：

* 执行 `inject` 时 `--project` 是否是当前版本对应的项目名；
* 旧版本和新版本是否使用了不同的 `project_name`；
* 浏览器打开的是否是对应版本目录。

### 数据库提示 key too long

新版脚本已经使用 `file_path_hash` 和辅助索引规避长路径索引问题。升级后重新运行：

```bash
python3 enhance_coverage.py server
```

或重新执行一次：

```bash
python3 enhance_coverage.py inject --project <project_name> --dir <raw_dir> --out <review_dir>
```

脚本会自动补齐表结构。

---

## 13. 安全建议

* 不建议将服务直接暴露到公网；
* Nginx 建议配置办公网段白名单；
* `coverage_config.json` 中包含数据库密码，不要提交到公共仓库；
* Python 服务建议只监听 `127.0.0.1`，由 Nginx 对外代理；
* 公司 IT 扫描网站漏洞时，重点说明这是内网静态报告页面加本地 API 持久化服务，无用户登录，无公网访问。

---

## 14. 最小操作清单

新版本从零到可用：

```bash
cd /opt/coverage_tool

# 1. 注入新版本报告
python3 enhance_coverage.py inject \
  --project review_main_202606 \
  --dir /opt/coverage_reports/raw_main_202606 \
  --out /opt/coverage_tool/review_main_202606

# 2. 启动后台服务
python3 enhance_coverage.py server

# 3. 如果需要继承旧版本
python3 enhance_coverage.py inherit \
  --from review_main_202605 \
  --to review_main_202606
```

访问：

```text
http://服务器IP/coverage/review_main_202606/html/index.html
```
