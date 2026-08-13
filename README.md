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

### WSL 本地浏览器 Demo

不配置 MySQL 也可以先完整验证“填写 → 暂存/确认 → 查看进展 → 导出报表”链路：

```bash
cd /home/wangsuisheng/coverage_tool
python3 coverage_demo.py
```

然后在 Windows 浏览器打开：

```text
http://localhost:8765/
```

Demo 使用 `.coverage_demo/coverage_demo.sqlite3` 持久化，刷新页面后已暂存内容仍会回显。它复用正式环境的前端脚本、HTTP 接口处理和 Excel/ZIP 生成逻辑，仅将 MySQL 换成了免安装的 SQLite。可用 `--port 8766` 更换端口。

---

## 1. 目录说明

核心文件：

```text
/opt/coverage_tool/
  enhance_coverage.py        # 后台脚本：注入、启动服务、导出、继承
  coverage_check.py          # Git + LCOV 增量覆盖率计算模块/独立命令
  repositories.example.json  # 多仓库增量覆盖率配置示例
  clear_coverage_data.py     # 调试脚本：清空单项目或全部数据库数据
  coverage_progress.html     # 独立网页：查看项目/小组/组长/目录/文件分析进度
  coverage_progress.js       # 进度页外部脚本：后台任务轮询，兼容严格 CSP
  代码目录归属模块统计.xlsx   # 目录 -> 模块 -> 小组/组长归属表
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
* Python 3.6.8 或更高版本；
* MySQL 或 MariaDB；
* Nginx；
* Python MySQL 驱动。

安装 Python 驱动：

```bash
# Python 3.7+：
pip3 install pymysql

# Python 3.6.8：使用项目提供的兼容依赖清单
python3.6 -m pip install -r requirements-py36.txt
```

当前 PyMySQL 版本已不再支持 Python 3.6；Python 3.6 也已停止维护。因此旧环境应使用公司已审计的兼容包并限制在内网，长期建议升级 Python。脚本本身按 Python 3.6.8 兼容实现。

Python 3.6.8 环境的统一运行方式：

```bash
python3.6 enhance_coverage.py inject ...
python3.6 enhance_coverage.py incremental ...
python3.6 enhance_coverage.py server
python3.6 coverage_check.py ...
```

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
  "ownership": {
    "enabled": true,
    "xlsx_path": "代码目录归属模块统计.xlsx"
  },
  "render_mode": "lazy",
  "project_name": "review_main_202605"
}
```

说明：

* `mysql.database` 可以多个项目共用一个库；
* `project_name` 是兼容旧流程的默认项目名；
* 推荐执行 `inject` 时用 `--project <项目名>` 显式指定项目名，避免忘记修改配置文件；
* `worker_threads` 控制注入写库和按目录导出 Excel 的并发线程数，建议从 4 开始，数据库压力允许时再调大；
* `ownership.xlsx_path` 指向代码目录归属表，支持绝对路径；相对路径按 `enhance_coverage.py` 所在目录解析；
* `ownership.enabled=false` 可以临时停用小组/组长归类，但不会影响原有项目、目录、文件进度；
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

## 4.1 生成增量覆盖率可填写网页

当评审范围只需要关注两个 Git commit 之间新增、且尚未覆盖的代码时，使用 `incremental` 子命令。它会计算增量覆盖率，并复用当前的填写、保存、进度和导出能力。

```bash
cd /opt/coverage_tool
python3 enhance_coverage.py incremental \
  --project review_main_202606_incremental \
  --repo /opt/src/main_repo \
  --oldgit a1b2c3 \
  --newgit d4e5f6 \
  --info /opt/coverage_reports/main_202606/coverage.info \
  --dir /opt/coverage_reports/raw_main_202606 \
  --out /opt/coverage_tool/review_main_202606_incremental \
  --mode lazy \
  --workers 4
```

参数说明：

* `--repo`：包含 `oldgit` 和 `newgit` 的 Git 仓库；脚本会执行 `git diff oldgit newgit`，并读取 `oldgit..newgit` 中的 Git author 与提交文件；
* `--info`：单个 LCOV `.info` 文件，或仅包含多个 `.info` 文件的目录；多个文件会在 Python 内合并，不依赖系统 `lcov` 命令；
* `--dir`：由 `genhtml` 生成的原始全量 HTML 报告，只读；
* `--out`：增量审查网页输出目录。和全量 `inject` 一样，若目录已存在会被重新生成；
* `--project`：增量审查数据在数据库中的隔离名称，建议不要与全量报告共用；
* `--excel`：可选，指定增量结果 Excel 的输出位置；未指定时写入 `--out/incremental_coverage.xlsx`。

输出目录的 `html/` 中会包含：

* `incremental_coverage.html`：增量覆盖率汇总页，点击文件可打开源码页；
* `incremental_developer_tasks.html`：按开发人员列出的提交文件和待填写清单，可直接跳转到相应源码页；
* `incremental_coverage.json` / `incremental_coverage.xlsx`：每条新增行的计算结果；JSON 包含开发人员与文件映射，Excel 额外提供 `Developer Summary`、`Developer Files` 工作表；
* `coverage_progress.html`：增量填写进度页；
* 原始 LCOV 源码页：仍保留完整红绿覆盖率显示，但**只在 Git 新增且 LCOV 未覆盖的行旁显示填写控件**。

汇总页默认将未覆盖新增行最多的文件排在前面；可点击仓库、文件、新增行、已覆盖、未覆盖、无需覆盖或覆盖信息缺失表头，按对应列升序或降序排序。
“开发人员待填写清单”按 Git author 的“姓名 + 邮箱”识别人员，并列出该人员在 commit 范围内提交的全部文件、关联 commit 与每个文件的待填写新增行。多人共同提交同一文件时，文件会同时列给相关人员，便于协作确认；待填写行仍以最终 Git diff 中 LCOV 未覆盖的新增行为准。
源码填写面板中的“上一个”和“下一个”按钮可在当前文件内跳转至相邻的可填写控件；到达首个或末个控件时，对应按钮会禁用。
当前文件填写多个控件后，可使用右下角“定位首个待填写”跳转到第一个未确认或暂存控件；随后可在面板中使用“上一个/下一个”继续跳转。单击“继承”只复制上方最近一条已填写结果；单击某个控件的“批量继承”，会把该来源之后至当前控件之间的整段控件一起复制并标记为待暂存。填写完成后，可使用“暂存草稿”一次批量写入数据库；草稿允许保留“未确认”及未完成字段，计入填写进度但不计入已确认结论。“确认提交”会对待暂存控件执行状态、确认人和覆盖说明校验后再批量提交；离开含未暂存内容的页面会收到提示。

统计口径为 `已覆盖 / (已覆盖 + 未覆盖)`。LCOV 中没有 `DA` 记录的新增行记为“无需覆盖”；整个文件未出现在 `.info` 中则记为“覆盖信息缺失”，不会被误算为无需覆盖，也不会生成填写控件。

如只需独立计算而不生成网页，仍可直接执行原脚本：

```bash
python3 coverage_check.py \
  --repo /opt/src/main_repo --oldgit a1b2c3 --newgit d4e5f6 \
  --info /opt/coverage_reports/main_202606/coverage.info \
  --excel incremental_coverage.xlsx --json incremental_coverage.json
```

### 多仓库增量评审

一个 `.info` 同时包含多个独立 Git 仓库时，复制 `repositories.example.json` 为实际配置文件，并为每个仓库填写独立的路径和 commit 范围：

```json
{
  "repositories": [
    {"name": "platform", "path": "/opt/src/platform", "oldgit": "a1b2c3", "newgit": "d4e5f6"},
    {"name": "driver", "path": "/opt/src/driver", "oldgit": "112233", "newgit": "445566"},
    {"name": "app", "path": "/opt/src/app", "oldgit": "abc111", "newgit": "def222"}
  ]
}
```

在配置文件所在目录执行时，`path` 可以写相对路径；仓库名称必须唯一。然后一次生成统一的可填写网页：

```bash
python3.6 enhance_coverage.py incremental \
  --project review_multi_incremental \
  --repos-config /opt/coverage_tool/repositories.json \
  --info /opt/coverage_reports/all_repositories.info \
  --dir /opt/coverage_reports/all_repositories_html \
  --out /opt/coverage_tool/review_multi_incremental \
  --mode lazy \
  --workers 4
```

如果只需统计/导出而无需网页：

```bash
python3.6 coverage_check.py \
  --repos-config /opt/coverage_tool/repositories.json \
  --info /opt/coverage_reports/all_repositories.info \
  --excel multi_incremental.xlsx \
  --json multi_incremental.json
```

多仓库模式会分别执行每个仓库的 `git diff` 和 Git author 提交文件采集，最后汇总总覆盖率；汇总网页与 Excel 会显示仓库列，Excel 额外提供 `Repositories`、开发人员任务工作表。为避免同名文件混淆，`.info` 的 `SF:` 必须是**绝对路径**，且应与 LCOV HTML 源码页标题中的路径一致。若检测到相对 `SF:` 路径，脚本会中止而不是生成可能串数据的结果。

访问增量汇总页：

```text
http://服务器IP/coverage/review_main_202606_incremental/html/incremental_coverage.html
```

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

对百万行级项目，推荐在进度页点击“后台导出详细 CSV”。后台会按 5000 行一批写入临时文件，页面持续显示阶段、百分比和已用时，完成后自动下载。这条链路不会把全部明细一次性放入 Python 内存。

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

一次导出项目、目录、小组/组长、文件四个层级的分析进度：

```bash
curl -o coverage_full_progress_summary.xlsx \
  "http://127.0.0.1:9528/api/coverage/export?type=full_progress_summary&project=review_main_202606"
```

`full_progress_summary` 包含：

* `项目进度` sheet：整个项目分析进度；
* `目录进度` sheet：各目录分析进度；
* `小组进度` sheet：按开发小组和开发主管归类的分析进度；
* `文件进度` sheet：各文件分析进度，并附带模块、小组、组长和归属匹配状态。

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

* 按小组和组长汇总的填写、确认进度；
* 各目录进度；
* 各文件进度及其模块、小组、组长、匹配状态；
* 点击文件路径后，每页 200 行查看该文件的详细填写数据；
* 归属表已匹配/未匹配文件计数，目录无法识别的文件会集中归入“未匹配小组”；
* 进度 Excel 导出入口；
* 按目录拆分的 Excel ZIP 导出入口；
* 带实时任务进度的全量详细 CSV 后台导出入口。

“填写进展”只有文件级粒度：数据库执行一次按文件聚合，每个文件向服务端和浏览器返回一行摘要。即使单文件有 10 万条未覆盖行，进度结果中仍只占一行；逐行数据仅在用户点击文件或启动详细导出时查询。

进度计算使用后台任务。数据库聚合期间无法获得 MySQL 内部的精确扫描行数，页面会保持在“数据库聚合”阶段并持续刷新已用时；查询完成后，项目/目录汇总和文件归属匹配会显示精确百分比。

点击“查看进度”后，进度条会在发出第一个 HTTP 请求前立即出现，并以动画和已用时显示连接、数据库聚合等阶段。进度页逻辑放在同源外部文件 `coverage_progress.js` 中，可在 Nginx 配置 `script-src 'self'` 的严格 CSP 下执行，不需要放开 `unsafe-inline`。

### 小组和组长归属表

进度接口每次查询都会检查 `ownership.xlsx_path` 指向文件的修改时间、大小和文件标识。表格未变化时使用内存缓存；替换或修改表格后会在下一次查询时自动重新读取，不需要重启服务。建议先生成完整新文件，再用同名文件替换旧文件，避免服务恰好读取到保存中的半成品。

匹配链路为：文件路径 -> 最长匹配代码目录 -> 模块 -> 开发小组/开发主管。工作簿和 sheet 名可以变化，程序按表头识别数据：

* 目录表需要“Directory（或目录/代码目录/代码路径/路径）”和“模块（或组件）”；
* 负责人表需要“组件（或模块）”、“开发小组（或小组/开发组）”和“开发主管（或组长/小组组长/主管）”；
* 构建机根目录变化不会影响匹配，例如表中的 `/home/coverage/repo/src/a` 可以匹配报告中的 `/build/work/repo/src/a/file.c`；
* 重复目录或组件如果配置成互相冲突的归属，会被视为歧义并进入未匹配统计，避免把进度错误分给某个小组。

推荐使用一个标准 `.xlsx` 工作簿，放置下面两个 sheet。sheet 名不限制，其他无关列也可以保留。

目录与模块 sheet 示例：

| Directory | 模块 |
| --- | --- |
| `/home/coverage/repo_a/src/network/core` | `NET_CORE` |
| `/home/coverage/repo_b/src/storage` | `STORAGE` |

负责人 sheet 示例：

| 组件 | 开发小组 | 开发主管 |
| --- | --- | --- |
| `NET_CORE` | `网络平台组` | `张三` |
| `STORAGE` | `存储平台组` | `李四` |

填写要求：

* `模块` 和 `组件` 是两张表的关联键，建议保持完全一致；匹配时会忽略大小写和首尾空格；
* `Directory` 填源码目录，不填具体源文件，也不需要通配符；程序使用路径分段和最长目录规则匹配；
* 表头之前可以有标题行，程序会自动寻找包含必需表头的行；
* 合并单元格可以读取，但为了避免归属不清，建议每条目录和组件各占一行；
* 文件必须是 `.xlsx`，不支持旧版二进制 `.xls`；
* 项目已提供一份同名表格；归属发生变化时可以直接更新该文件，或通过 `ownership.xlsx_path` 指向外部维护的表格。

读取 xlsx 只使用 Python 标准库，不依赖 `openpyxl`，可在 Python 3.6.8 环境运行。

该页面主要使用下列后台接口：

```text
/api/coverage/progress/start?project=<project_name>
/api/coverage/jobs/status?id=<job_id>
/api/coverage/details?project=<project_name>&file=<file_path>&page=1&page_size=200
/api/coverage/export/start?type=full_detail&project=<project_name>
/api/coverage/export/download?id=<job_id>
```

旧的同步 `/api/coverage/progress?project=<project_name>` 仍保留兼容，但浏览器页面使用上述后台任务接口，不再受单次 HTTP 请求超时限制。

如果已经生成过旧报表，需要重新执行一次 `inject`，把新的 `coverage_progress.html` 和 `coverage_progress.js` 复制到报表目录，并更新覆盖率页面使用的 JS/CSS 版本。如果只替换后台 Python 文件而没有重新 `inject`，旧报表目录中的静态进度页不会自动更新。

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
