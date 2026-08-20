# FOS Coverage Tool 最新代码与 Skill 联合审计

- 审计日期：2026-08-20
- 仓库：`Chary-yu/fos_coverage_tool`
- 分支：`main`
- 固定审计 revision：`94f57cf3c358d8886b05dc3e097938a36375f193`
- HEAD 提交：`perf: finish code detail and browser runtime optimizations`
- 使用的最新 Skill 体系：
  - `fos-coverage-maintainer`（总控/路由/Skill drift）
  - `fos-coverage-change-review`（源码、VNext wiring、canonical ownership、源码安全）
  - `fos-coverage-runtime-reliability`（运行时数据、任务、事务、身份正确性）
  - `fos-coverage-performance-ui`（Code Detail、浏览器、请求/DB/Sidecar/缓存放大）
  - `fos-coverage-release-governance`（迁移、回滚、发布证据）

> 证据边界：本次已通过 GitHub 连接器读取固定 SHA 的源码、提交、CI、测试和 Skill 资源；当前执行容器无法 DNS 访问 GitHub，因此没有把仓库 clone 到本地，也没有执行本机单测、真实 HTTP、Playwright 或生产环境检查。以下“确认问题”均可由源码直接证明；需要运行时/生产证据的项会明确标为“待运行验证”，不会按通过处理。

---

## 1. 总体结论

### 1.1 当前状态

**结论：当前 revision 不应判定为 VNext Gate 1–3 完成，也不应进入 VNext 生产切换。**

最关键的结构性事实：

1. `enhance_coverage.py` 虽然已缩成薄入口，但仍直接进入 `app.legacy_runtime`。
2. `run_server()` 只有显式 `runtime_mode=vnext` 才进入 VNext；默认配置与默认 runtime config 仍是 `legacy`。
3. canonical 前端 `web/assets/js/*` 仍主要使用 legacy API 协议；VNext API 的 URL、必填身份字段、请求体和响应结构已经变化。
4. 因此前端真实切到 VNext 后，Code Detail 加载、分析保存、进展页、开发者任务刷新、导出等关键链路会断。
5. VNext 新增的 Scan/Report/RepositorySnapshot 模型仍存在“Scan 创建后继续修改事实”的公共写入口，Scan immutability 尚未真正封死。
6. 最新性能优化改善了 DOM，但仍有网络、DB overlay、JS resident data 和 Sidecar cache 的放大。
7. CI/诊断脚本目前存在 false-green / false-red 风险，不能作为 Gate 1–3 完成的充分证据。
8. 最新 Change Review Skill 的 4 个 audit helper 本身存在语法损坏，Skill 的文字规范比其可执行 helper 更可靠。

### 1.2 Gate 判断

| Gate / 领域 | 本次判断 | 原因 |
|---|---|---|
| Gate 1 Canonical ownership | **BLOCKED / TRANSITIONAL_LEGACY** | `app/legacy_runtime.py` 仍保留数据库、HTTP、inject、incremental、report 等业务实现；默认 CLI 仍进入它 |
| Gate 2 Schema / semantic migration | **证据不足，不判通过** | 本次未接生产数据库和迁移环境；且 Scan immutability 仍有源码级 P1 |
| Gate 3 UI/API/runtime migration | **FAIL / BLOCKED** | canonical 前端与 VNext API 明确不兼容 |
| Runtime reliability | **BLOCKED** | 连接池事务清理、后台任务去重/恢复/关闭存在 P1 |
| Performance/UI | **PARTIAL** | DOM 虚拟化有效，但数据、HTTP、DB、缓存层仍有放大 |
| Release readiness | **NOT READY** | 有未解决源码 P1，且缺真实浏览器、HTTP、MySQL、生产切换证据 |

---

# 2. P1：必须优先修复的代码问题

## P1-01 canonical Code Detail 前端与 VNext API 完全不兼容

**涉及文件**
- `web/assets/js/coverage_enhance.js`
- `app/api/application.py`
- `app/api/endpoints/code_detail.py`
- `app/api/handler.py`

**源码事实**

前端：
- `POST /api/coverage/code-lines/batch`
- body 使用 `project_name / file_path / report_id / scope / load_default_expanded`
- 不传 `scan_id`
- 不传 `ranges`
- 期望响应 `data.data.ranges`

VNext：
- 强制要求 `scan_id + report_id + file_path`
- batch 强制读取 `ranges`
- 空 ranges 返回 `batches: []`
- 响应是顶层对象，不再套 legacy `status/data`

结果：真正使用 `runtime_mode=vnext` 时，Code Detail 初始加载和 chunk 加载会 400、空返回或前端解析不到数据。

**建议**
- 先明确 Gate 3 的唯一 API schema。
- HTML/页面 meta 必须带 immutable `scan_id`，必要时还要带 `repository_name`。
- canonical JS 直接消费 VNext 顶层 DTO，不再依赖 legacy wrapper。
- 新增“真实 canonical JS + 真 VNext HTTP server”集成测试，禁止 mock legacy contract 代替。

---

## P1-02 分析保存链路仍是 legacy `/batch`

**涉及文件**
- `web/assets/js/coverage_enhance.js`
- `app/api/application.py`
- `app/api/endpoints/analysis.py`

前端保存调用：
- `POST /api/coverage/batch`
- 传 `project_name / file_path / records`
- records 使用 `line_numbers`

VNext 实际接口：
- `POST /api/coverage/analysis`
- 必须包含 `project_name + scan_id`
- 推荐以 immutable `line_id` 或完整 file identity + line_number 保存

VNext 根本没有 `/batch`。

**结果**：切 VNext 后人工分析保存直接失败。

---

## P1-03 进展页、开发者任务页和导出仍绑定 legacy API

**涉及文件**
- `web/assets/js/coverage_progress.js`
- `web/assets/js/incremental_developer_tasks.js`
- `tests/browser/coverage_real_browser.spec.js`
- `app/api/application.py`

legacy 前端仍使用：
- `/details`
- `/export/start`
- `/export/download`
- `/projects` 并要求 `status === 'success'`
- `/incremental/unanalyzed` 并读取 `data.files`

VNext 使用：
- `GET /projects` → 顶层 `projects`
- `POST /exports`
- `GET /exports/<job_id>`
- `GET /incremental/unanalyzed` → 顶层 `files`

真实浏览器测试的 harness 也主动 mock 了 legacy `{status:'success', data:...}`，所以当前 Playwright 源码即使运行成功，也无法证明 Gate 3 VNext 契约正确。

---

## P1-04 VNext 导出缺少可下载闭环

**涉及文件**
- `app/services/export_service.py`
- `app/api/application.py`

VNext `ExportService` 可以生成 ZIP，并把服务器绝对路径作为 background job `result_path`。

但 API 只有：
- `POST /exports`
- `GET /exports/<job_id>`

没有安全的：
- `GET /exports/<job_id>/download`
- 或等价的受控文件响应接口

因此新的 VNext 导出即使任务完成，浏览器仍缺最后的下载路径；直接向用户返回服务器文件系统路径也不是正确的浏览器协议。

---

## P1-05 默认 server 仍然启动 legacy，而不是 VNext

**涉及文件**
- `enhance_coverage.py`
- `app/legacy_runtime.py`
- `app/config/runtime_config.py`
- `coverage_config.json`

事实：
- 根 `enhance_coverage.py` 是薄 shim，但直接执行 `app.legacy_runtime`。
- `run_server()` 使用 `config.get('runtime_mode') or 'legacy'`。
- `default_runtime_config()` 明确给 `runtime_mode='legacy'`。
- 当前根 `coverage_config.json` 没有 `runtime_mode`，因此仍落到 legacy。

**判断**：VNext 目前只是显式 opt-in Candidate runtime，不是 canonical runtime。

这本身不要求立即把生产默认改成 VNext；但在前端契约没修完之前，更不能宣称 Gate 3 已完成。

---

## P1-06 `app/legacy_runtime.py` 仍然是大型第二业务实现

文件约 339 KB，仍包含：
- legacy HTTP server
- DB manager / request handling
- inject/report generation
- incremental site generation
- progress / developer task generation
- CLI 分支
- 后台任务恢复等

虽然根入口已变成 thin shim，但 shim 的目标仍是大型 legacy business owner。

**Gate 1 正确状态应为 `TRANSITIONAL_LEGACY`，而不是 `CANONICAL` 或 `LEGACY_RETIRED`。**

正确目标：legacy 只能保留参数翻译、兼容转发和必要的旧入口，不应继续拥有独立业务语义。

---

## P1-07 `read_only=True` 连接返回池时跳过 rollback，破坏事务卫生

**文件**：`app/db/connection_pool.py`

连接创建：
- `autocommit=False`

但 `PooledConnectionWrapper.rollback_if_dirty()`：
- `read_only=True` 时直接跳过 rollback

在 MySQL/PyMySQL 下，即使只有 SELECT，也可能进入事务/持有一致性快照。将连接不 rollback 直接返回池，下一次借用可能继承旧事务语义，导致：
- 陈旧快照
- 长事务
- undo / MVCC 压力
- 意外锁或 session state 延续

`read_only` 目前只是 Python 标志，不是数据库层强制 read-only transaction。

**建议**
- correctness 优先：返回池统一 rollback。
- 如果确实要省 round trip，应使用真正 autocommit read connection pool，或分离 RO/RW pool，并通过 MySQL 实测证明无事务残留。

---

## P1-08 两个 Scan 并发提交同类后台任务时，第二个任务可能永久 `queued`

**文件**
- `app/jobs/service.py`
- `app/jobs/bounded_executor.py`
- `app/db/repositories/job_repository.py`

数据库去重键：
`project_id + scan_id + kind + data_version`

执行器二次去重键却只有：
`project_name(project_id) + job_type(kind:data_version)`

**没有 scan_id。**

场景：
1. Scan A 的 export/rebuild 正在运行。
2. 同 Project 的 Scan B、相同 data_version、相同 kind 提交任务。
3. DB `find_active()` 因 scan_id 不同 → 查不到，先持久化 B 为 `queued`。
4. Executor 看到 A 的 project/job_type 相同 → 直接返回 A 的 JobDescriptor。
5. B 的 callback 从未入队。
6. Service 返回的却是 B 的持久化记录。

结果：B 永久停留 `queued`。

**修复**
- durable repository 是唯一去重 owner。
- VNext 调用 executor 时 `reuse_existing=False`；或 executor dedupe key 必须包含 scan_id / durable job identity。

---

## P1-09 进程快速重启时，`running` job 可能永久无人接管

**文件**
- `app/jobs/service.py`
- `app/db/repositories/job_repository.py`

VNext job 仅在：
- 开始时写一次 heartbeat
- 完成/失败时再写一次

没有周期 heartbeat。

启动 `recover()` 只把：
- 所有 queued
- heartbeat 超过 timeout 的 running

标为 interrupted。

如果进程崩溃后在默认 300 秒内重启：
- 旧 running 的 heartbeat 仍“新鲜”
- startup recover 不处理
- 新进程没有它的 callback
- 后续也没有周期 reaper

结果：job 可永久保持 `running`。

**正确模型**
- 加 `worker_instance_id / lease_owner / process_generation`。
- 新进程启动时立即回收不属于当前 generation 的 running job；或建立周期 reaper + 真 heartbeat lease。

---

## P1-10 shutdown 最多等每个 worker 1 秒，随后就关闭 DB pool

**文件**
- `app/jobs/bounded_executor.py`
- `app/bootstrap.py`

`BoundedJobExecutor.shutdown(wait=True)`：
- 每个线程 `join(timeout=1.0)`
- worker 是 daemon thread

`VNextRuntime.close()` 随后立即：
- `database_manager.close()`

长任务 >1 秒时：
- worker 仍可能运行
- pool 已 shutdown
- callback 完成后 `_save(completed/failed)` 可能使用已关闭/返回失败的连接
- DB 中最终状态可能继续 `running`

**建议**
- shutdown 先停止接新任务。
- 设置全局 drain deadline。
- 等 active workers 归零或显式进入 interrupted/cancelled。
- 最后再关 DB pool。

---

## P1-11 Scan immutability 没有统一写屏障

**文件**
- `app/services/project_service.py`
- `app/db/repositories/project_repository.py`
- `app/db/repositories/line_index_repository.py`

问题包括：

1. `create_scan()` 默认 `status='ready'`。
2. 公开 `ingest_files(scan_id, files)` 仍可继续写 `coverage_files/coverage_lines`。
3. `ProjectRepository.upsert_repository_snapshot()` 可以 UPDATE 已有 snapshot。
4. `bind_report()` 可以 UPDATE 已有 report metadata。
5. `ensure_file()` 可以 UPDATE 已有 immutable file path/source name。
6. `upsert_line()/upsert_lines()` 可以 UPDATE 已有 physical line fact。
7. `create_scan()` 命中已有 scan_key 后仍继续 upsert snapshot / bind report。
8. `create_scan_and_ingest()` 命中已有 scan_key 后仍继续写 files/lines。

这与 VNext “Scan 创建完成后 Project/RepositorySnapshot/File/Line 是不可变历史事实”的核心模型冲突。

**建议**
- 明确 Scan lifecycle：`building/importing -> ready/sealed`。
- 所有 immutable repository mutation primitive 必须要求 `scan.status` 仍在 construction 状态。
- existing scan_key 命中后只做一致性验证，不再修改事实。
- `ingest_files()` 变 private construction helper，或增加强制 guard。

---

## P1-12 VNext Code Detail 丢失 `repository_name`，多仓库同路径会选错文件

**文件**
- `app/code_detail/vnext_service.py`
- `app/api/endpoints/code_detail.py`

数据库真实 file identity 已经包含：
- scan_id
- repository_name
- file_path_hash

但 Code Detail API 只要求：
- scan_id
- report_id
- file_path

查询也是：
`WHERE scan_id=? AND file_path_hash=? AND file_path=?`

**不含 repository_name。**

当两个仓库都有 `src/foo.c`：
- DB 中是两个不同文件
- Code Detail 无法区分
- `fetchone()` 可能拿到错误仓库
- Sidecar key 也只按 file_path 计算，存在同路径冲突

**修复**
- Code Detail immutable identity 增加 `repository_name`。
- 前端 meta / URL / API body 全链路带 repository_name。
- Sidecar file key 必须按 `(repository_name, normalized_file_path)` 或等价唯一身份生成。

---

## P1-13 多仓库 LCOV 相对路径无法唯一归属时仍被接受

**文件**：`app/inject/service.py`

`_repository_name()`：
- 只有 path 唯一命中某 repository_path 才返回 repo name。
- 0 个或多个匹配直接返回 `('', path)`。
- 后续只有绝对路径/盘符才失败。

因此多个仓库 + 相对 LCOV `src/foo.c` 时，可能以 `repository_name=''` 写入，丢失 repository identity。

这与 P1-12 会叠加。

**建议**
- repositories 数量 >1 时，任何不能唯一确定仓库的 LCOV path 都必须 fail closed。
- 不能用空 repository_name 表示“未知但继续导入”。

---

## P1-14 Sidecar 行范围没有上限，可在读文件前造成 CPU/内存 DoS

**文件**：`app/code_detail/sidecar_store.py`

当前只检查：
- start >= 1
- end >= start

随后根据 `end_line` 构造 chunk index set。

如果传极大的 `end_line`，可在读取实际 Sidecar 前就构造巨大 `range/set`。

VNext API：
- 单 GET 没有最大 span
- batch 只限制最多 1000 ranges，没有限制 ranges 的总行数/总 chunk 数

**建议**
- 先读取 metadata 的 `total_lines/total_chunks`。
- `end_line > total_lines` 直接 clamp 或 400。
- 单请求加 `max_lines / max_chunks / max_payload_bytes`。
- batch 同时限制 ranges 数量和总 logical span。

---

## P1-15 HTTP request body 无大小上限，授权前就全部读入内存

**文件**：`app/api/handler.py`

`Content-Length`：
- 直接 `int()`
- 直接 `self.rfile.read(length)`
- 没有最大值
- 没有拒绝负值
- JSON 全部读完后才进入 application 鉴权

风险：
- 巨大 body 内存耗尽
- 慢连接/负 length 行为占用 server thread
- 未授权请求同样能先消耗资源

**建议**
- transport 层限制 `0 <= Content-Length <= MAX_BODY_BYTES`。
- 超限 413。
- endpoint 再做 records/ranges 级别更小限制。

---

## P1-16 CI / 诊断门禁存在 false-green 与 false-red

### 仓库 `runtime_participation_audit.py`
- 仍搜索旧的 `load_lines_range(`，当前实现已是 `load_lines_ranges(`。
- 会制造错误失败。
- 对 VNext wiring 主要做字符串存在性判断，无法证明默认配置真的进入 VNext。

### 仓库 `canonical_ownership_audit.py`
- 主要检查根 `enhance_coverage.py` 是否还包含业务定义。
- 大 monolith 已移动到 `app/legacy_runtime.py` 后，root shim 可以 PASS，但业务所有权并未真正退休。

### `runtime_legacy_dependency_audit.py`
- 把 `app/legacy_runtime.py`、`app/incremental/legacy.py` 标为 transitional legacy。
- 只要 VNext 不反向 import 它们就能 PASS。
- 这只能证明“隔离”，不能证明“legacy retired”。

**结论**：当前 repo diagnostics 不能作为 Gate 1 完成的充分证据。

---

## P1-17 CI 依赖安装失败被 `|| true` 吞掉

**文件**：`.github/workflows/ci.yml`

```sh
pip install -r requirements-py36.txt || true
pip install pymysql || true
```

依赖安装失败后测试仍继续，容易出现：
- 跳过真实 MySQL 路径
- fallback / missing optional dependency 导致 false-green
- 环境不完整但 job 仍显示成功

**建议**：必需依赖 fail closed；真正 optional 的依赖显式检测并标记 SKIPPED，而不是吞错误。

---

# 3. P2：重要优化点 / 条件性风险

## P2-01 Code Detail overlay 在多 chunk GET 下重复全文件 DB 查询

**文件**
- `app/code_detail/vnext_service.py`
- `app/db/repositories/analysis_repository.py`
- `web/assets/js/coverage_enhance.js`

每次 `lines()/lines_batch()` 都调用 `_overlay()`，而 `_overlay()` 使用 `get_by_file()` 读取这个文件的全部分析记录。

前端大 region 按 2000 行拆多个 GET；例如 100k 行需要约 50 个请求。

因此可能发生：
- 50 次 HTTP
- 每次 1 次全文件 analysis overlay SQL
- 大量重复 DB row decode

当前单测只证明“单次 `lines_batch()` 内 overlay 读取一次”，没有覆盖“50 次 HTTP 请求”的跨请求放大。

**优化**
- 前端真正使用 VNext `/code-lines/batch`。
- 或 overlay cache 按 `scan_id/file_id/data_version` 缓存。
- 或 repository 支持按 requested line range 读取 analysis。

---

## P2-02 当前“虚拟滚动”只虚拟 DOM，没有虚拟网络/数据

大 region 展开时：
- 仍会把整个 region 所有 chunk 下载完。
- `allLines` 仍保存所有行。
- 只把屏幕附近少量 DOM 节点挂进 document。

优点：DOM 大幅减少。

未解决：
- 网络 bytes
- JSON parse
- JS heap resident rows
- 服务端请求数
- DB overlay 重复读取

**下一阶段应做 demand-window data virtualization**：只加载 viewport + overscan 邻近范围，并按滚动预取。

---

## P2-03 固定 `VIRTUAL_LINE_HEIGHT=24` 与可变高度 review panel 冲突

review panel 有：
- textarea
- resize grip
- multi-line block 高度
- 用户手动 resize

但虚拟滚动 spacer/index 仍假设每行固定 24px。

可能造成：
- 滚动位置漂移
- revealLine 定位偏差
- 跳动
- 中后段误差累积

可选修复：
- variable-size virtualizer + 实测高度索引；
- 或 review rows 不进入固定高度虚拟列表。

---

## P2-04 每个 block 都构造完整 `lineNums` 数组

`Array.from({length: end-start+1}, ...)` 在 metadata 注册和 panel 构造时都会产生完整行号数组。

大 block 不需要保存所有 line number；只保存 `start/end/length` 即可，提交时再做受控展开或后端接受 range。

---

## P2-05 `_sidecar_stores` 顶层缓存无全局上限

`VNextCodeDetailService` 按 `(report_root, asset_identity)` 永久保存一个 `SidecarStore`。

每个 SidecarStore 自己又有 chunk/meta cache。

单 store 有界不代表整个 runtime 有界；服务多个报告后总内存会不断增加。

建议：顶层 LRU / max report stores + metrics + active report pinning。

---

## P2-06 Sidecar lock 覆盖磁盘 open/json.load，限制并发收益

Sidecar metadata/chunk cache 的锁粒度覆盖了文件读取和 JSON decode。

多个并发 chunk 即使文件不同，也可能在同一 SidecarStore 内被串行。

建议 double-check cache：
1. lock 查缓存
2. unlock 做 I/O/decode
3. lock 插入/去重

---

## P2-07 Analysis API 没有 records 数量限制，会生成超大 IN / OR SQL

**文件**
- `app/api/endpoints/analysis.py`
- `app/services/analysis_service.py`
- `app/db/repositories/line_index_repository.py`
- `app/db/repositories/project_repository.py`
- `app/db/repositories/analysis_repository.py`

当前 `records` 只验证“必须是 list”，无数量上限。

随后可能生成：
- `WHERE id IN (?, ?, ... )`
- 大量 `(file_id=? AND line_number=?) OR ...`
- 大 `executemany`
- 大 readback IN

这与 Performance Skill 的 batch contract 冲突：把 N 小 SQL 合成一个无限大 SQL 不是有效优化。

建议：
- API 限制单批 records，例如 500/1000（以实测确定）。
- repository 内部再分 chunk，避免调用者绕过 API。

---

## P2-08 Scan ingest 每行是 SELECT + INSERT/UPDATE + SELECT 的 N+1 模式

`LineIndexRepository.upsert_lines()` 实际只是：
`[self.upsert_line(...) for record in records]`

而 `upsert_line()` 每行：
- SELECT existing
- INSERT/UPDATE
- SELECT readback

大 LCOV 导入会产生非常高的 DB round trips。

建议实现真正 bulk insert/upsert：
- immutable Scan construction 时 ideally 直接批量 INSERT。
- existing scan 不应走 UPDATE（见 P1 immutability）。

---

## P2-09 `max_workers` 不是全局 worker 上限

VNext 默认：
- default bucket = 4 workers
- database = 2
- cpu = 4
- disk = 2

总计可达 12 worker，而配置表面看是 `max_workers=4`。

这可能是有意的资源隔离，但命名和容量治理不清楚。

建议：
- 明确 `global_worker_budget` 与 `per_resource_workers`。
- 或把 default bucket worker 调整为只服务真正 default 任务。

---

## P2-10 Executor 完成的 `_jobs` 永不清理，metrics 越来越慢

`_jobs` 保存所有历史 JobDescriptor，没有 TTL / max entries / archive。

长期运行：
- 内存持续增长
- duplicate scan 与 metrics 都遍历越来越大的 dict

durable history 已在 DB；executor 内存只需保留 active + 最近少量完成项。

---

## P2-11 未知 resource_class 静默回退 default

配置拼错如 `databsae` 不报错，会落 default queue。

建议 fail closed 或至少 warning + metric。

---

## P2-12 全局 DB pool 绑定第一次配置，多个 Runtime 会互相污染

`get_global_pool(db_config)`：
- 一旦 `_GLOBAL_POOL` 已存在，后续 db_config 被忽略。

`DatabaseManager.close()`：
- 直接 close shared pool。

如果同一进程出现：
- 两个 Runtime
- Candidate / current 测试
- 两个不同数据库配置

第二个可能误连第一个 DB，关闭其中一个 manager 还会把另一个 runtime 的 pool 一起关掉。

建议 Runtime-owned pool；不要进程全局 singleton。

---

## P2-13 ping skip 可能把已被服务器关闭的连接直接交给请求

idle 时间小于 `idle_ping_after_sec` 时，不执行 ping，直接认为 alive。

若 MySQL/网络在这段时间主动断开，第一次 query 失败后才暴露。

建议：
- 对关键请求增加一次安全重试（只对可幂等 read）。
- 或使用更可靠 connection health / server timeout 配置。

---

## P2-14 ReportRegistry 会被“别的 report 的 `.source_cache`”污染判断

`sidecar_required=false` 的老 report，如果所在目录只因为别的 report 创建了 `.source_cache`，当前 prune/resolve 也会要求 `.source_cache/<当前 report_id>` 存在。

结果：一个 unrelated report 的 Sidecar 目录可以让旧报告突然变成 unresolved。

正确判断应只基于当前 report 自己的 metadata/schema，而不是目录里是否存在任何 `.source_cache`。

---

## P2-15 ReportRegistry 重复 register 会合并 root，可能导致 exact resolution 歧义

`register()` 把旧 directories + 新 directories 合并。

如果同 report_id 被搬迁/重新生成到新目录，旧目录仍存在：
- registry 有两个 root
- `resolve_exact_root()` 要求唯一 root
- 最终返回 None

需要明确语义：
- report_id immutable → 新生成必须新 report_id；或
- relocation → 注册时经过 identity 验证后替换旧 root，而不是永久 merge。

---

## P2-16 Sidecar report directory 正缓存缺少 relocation/删除失效

SidecarStore 对 report 目录解析做缓存。报告移动、删除、重新挂载后，正缓存可能继续指向旧位置，直到进程重启/显式更新 search dir。

建议结合 registry version / report identity 做失效。

---

## P2-17 Python 3.6 下 VNext HTTP server 退化成单线程

`app/api/server.py`：
- 有 `ThreadingHTTPServer` 用它
- ImportError 就直接用 `HTTPServer`

Python 3.6 不保证有 ThreadingHTTPServer；仓库 CI 又明确保留 Python 3.6 compatibility。

此时前端 3 并发 chunk 请求会在 server 串行执行。

建议 Python 3.6 fallback 使用 `socketserver.ThreadingMixIn + HTTPServer` 自建 threaded server。

---

## P2-18 auth 默认是 fail-open development 模式，安全性依赖外部环境变量

`default_runtime_config()`：
- server `0.0.0.0:9528`
- auth `disabled`
- runtime `legacy`

`validate_production_config()` 只有 `COVERAGE_ENV=production` 时才禁止 disabled auth。

当前仓库 `coverage_config.json` 是 `127.0.0.1 + reverse_proxy`，这是好的；但如果生产启动流程忘了设置 `COVERAGE_ENV=production` 且使用不完整配置，安全默认仍偏宽松。

建议：
- VNext mutation auth 默认 fail-closed。
- 只有显式 `development=true` 才允许 disabled。

---

## P2-19 API 500/404/Job/Metrics 暴露过多内部细节

`VNextApplication.dispatch()` 直接把 `str(exc)` 返回客户端。

`GET /jobs` / `/jobs/<id>` 无独立读授权，可能返回：
- input_payload
- error_message
- result_path

`metrics()` 的 Sidecar store key 里包含 `report_root|asset_identity`，可能暴露服务器绝对目录。

建议：
- 外部错误返回稳定错误码/简短 message；详细异常只进 server log + trace id。
- jobs/metrics/routes 设运维权限或只绑定受信管理面。
- 不输出绝对文件路径。

---

## P2-20 CI path filter 覆盖范围不完整

当前只触发：
- `**.py`
- `**.js`
- `**.sql`
- workflow 本身

不会因以下运行时关键文件变化自动触发：
- CSS
- HTML/templates
- JSON config
- `package.json/package-lock.json`
- requirements 文件
- shell/service/deployment config

这会产生明显的 TEST_SELECTION_GAP。

---

## P2-21 CI 的“修改点相关测试”映射不完整

目前固定执行：
- Lazy Collapse 根测试
- `tests/vnext`
- 少量 Python 3.6 compatibility tests
- mock DOM

仓库同时存在：
- `tests/database`
- `tests/release`
- `tests/security`
- `tests/progress`
- `tests/incremental`
- `tests/browser`

最新 commit 修改 DB pool / jobs / browser runtime 时，并没有一个根据 changed paths 自动选择这些相关 suite 的机制。

正确做法不是跑全量，而是建立“路径/能力 → test set”映射。

---

## P2-22 Real Browser E2E 是可选 CI，不是 Gate 3 必选证据

workflow：
`if: vars.COVERAGE_ENABLE_REAL_BROWSER == 'true'`

Performance/UI Skill 已明确：mock DOM 不能替代 browser acceptance。

对于修改 `coverage_enhance.js`、CSS、API client、virtual scroll、cancellation 的提交，应将真实浏览器变成相关变更的 required job，而不是可选开关。

---

## P2-23 当前浏览器大文件用例验证的是 DOM 上限，不是完整性能预算

现有测试能证明：
- DOM 节点 < 1500
- LRU 会卸载老 region

但没有强制：
- time-to-first-visible
- p95 expand latency
- payload bytes
- DB query count
- overlay rows
- JS heap/resident line count
- viewport 滚动到中部/末尾的正确性
- variable-height review panel 的定位

因此“100k 文件性能优化完成”目前证据仍不完整。

---

## P2-24 `?api=` 参数在 Code Detail 中被解析但实际不参与 API candidate

前端定义了 `EXPLICIT_API_URL = URL_PARAMS.get('api')`，但 `apiBaseCandidates()` 只返回固定 `SERVER_URL='/api/coverage'`。

如果 Gate 3 决策已明确“只允许同源唯一 API base”，那应该删除这段死配置和后续传播逻辑；否则它是功能回归。

---

## P2-25 staging rollback rehearsal 的“previous release”真实性需要加强

staging 配置中 `start_api` 与 `start_previous_api` 使用同一个脚本/配置/端口。

`run_upgrade.py` 在 staging 缺 `previous_release` 时会把 `previous_release = target identity`。

这只有在 CutoverController 已确实把文件恢复成上一版、并且 release endpoint 能返回上一版真实 identity 时才有证明力。

否则容易演变成“停止当前版本 → 再启动当前版本”的伪 rollback rehearsal。

Release Gate 应强制记录：
- before release SHA/build id
- target release SHA/build id
- rollback 后 endpoint 的 SHA/build id 必须回到 before identity

---

# 4. 已验证的非问题 / 正向变化

## INFO-01 根目录 JS/CSS compatibility copies 不是独立第二实现

本次检查显示根目录兼容资产与 `web/assets/*` canonical 文件使用相同 Git blob/hash。

因此“文件有两份”本身不应报 duplicate canonical implementation；只要继续用生成/哈希门禁保证一致即可。

## INFO-02 根 `enhance_coverage.py` 已经是 thin shim

这是正确的架构方向。

真正未完成的是 shim 目标仍然是大型 `app.legacy_runtime`，而不是根 shim 本身。

## INFO-03 production 配置校验已经开始 fail-closed

`COVERAGE_ENV=production` 时会要求：
- auth 不能 disabled
- trusted proxies
- upgrade lifecycle commands
- current/previous release endpoints
- previous release identity

方向正确，建议把这种 fail-closed 思路扩展到默认 VNext 安全配置、Scan immutability 和 CI gates。

---

# 5. Skill 能力与边界问题

## Skill-P1-01 Change Review 的 4 个 audit helper 资源本身存在 Python 语法损坏

抽查的 4 个：
- `scripts/audit_canonical_ownership.py`
- `scripts/audit_runtime_participation.py`
- `scripts/audit_relocation_closure.py`
- `scripts/audit_scan_immutability.py`

均出现类似：

```python
runtime_text = '
'.join(...)
```

被打包成真正跨行的单引号字符串，或：

```python
text.count('
', ...)
```

被破坏为跨行字符串。

这会直接 SyntaxError。

**分类**：`SKILL_SCRIPT_GAP / SKILL_BUNDLE_MISSING`

**建议**
- Skill 发布前对所有 `.py` helper 强制 `python -m py_compile`。
- 对所有 `.js` helper 至少 `node --check`。
- 增加最小 fixture smoke test。
- Skill bundle 构建必须保留反斜杠转义，不得把 `\n` 变成真实换行。

---

## Skill-P1-02 canonical ownership helper 仍硬编码不存在的 `config/coverage_config.json`

Skill 脚本 `PAIRS`：
- compatibility：`coverage_config.json`
- canonical：`config/coverage_config.json`

但当前仓库 `config/` 只有 example/staging example 等，真实根配置仍在 `coverage_config.json`。

因此 helper 即使修复语法，也会错误产生 `CANONICAL_MISSING P1`。

**分类**：`SKILL_RESOURCE_REFERENCE_GAP`

正确做法：按实际 config loader/当前目录模型判断 runtime config ownership，而不是固定写死一个不存在的 canonical path。

---

## Skill-P1-03 Skill helper 与仓库 diagnostics 已成为两套不同规则

仓库有：
- `scripts/diagnostics/runtime_participation_audit.py`
- `canonical_ownership_audit.py`
- `runtime_legacy_dependency_audit.py`

Skill 又有：
- `audit_runtime_participation.py`
- `audit_canonical_ownership.py`
- 等

两边规则、pattern、状态定义已经不同，并且都发生 drift。

**分类**：`SKILL_SCRIPT_GAP`

建议只有一个 canonical machine-readable contract：
- Skill 维护规范/schema；
- repo CI vendoring/生成同版本 helper；
- 输出带 `contract_version`；
- CI 检查 helper hash/version 与 Skill contract 对齐。

---

## Skill-P1-04 缺少“canonical frontend ↔ 真 VNext HTTP API”的确定性契约门禁

Performance/UI Skill 文字规范已经明确要求：
- Code Detail 初始加载
- save
- incremental
- real HTTP integration
- real browser
- 不得依赖 legacy fallback

但当前可执行 helper/CI 没有自动对比：
- frontend 实际 route
- request method/body/query keys
- mandatory scan/report/repository identity
- response DTO shape

所以本次最严重的 Gate 3 断链仍能进入 HEAD。

**分类**：`SKILL_SCRIPT_GAP`

建议新增：
- `audit_frontend_vnext_api_contract.py`
- 最小真实 VNext server + canonical web asset integration fixture
- 所有 canonical API client route 必须在 VNext router 注册且 schema 一致

---

## Skill-P1-05 Scan immutability helper 的定位是正确的，但 helper 损坏导致无法真正执行

Skill 已经意识到“Scan 创建后不能修改 repository/file/line/report 事实”，甚至专门提供 `audit_scan_immutability.py`。

本次源码恰好存在它本应抓到的问题：
- `ingest_files()` post-create mutation
- repository update primitives
- existing scan_key 后继续写事实

说明：
- **Skill 定位正确**
- **Skill 执行能力失效**

修复 helper 后，这应该成为 Gate 1 required audit。

---

## Skill-P2-01 runtime participation helper 不判断“当前配置实际走哪条 branch”

其 `RUNTIME_WIRED` 主要根据：
- provider 存在
- consumer 文本有 reference
- required pattern 命中

这无法区分：
- 代码中有 `create_vnext_server`
- 默认/current config 实际仍 `runtime_mode=legacy`

建议新增状态：
- `CONDITIONALLY_WIRED`
- `ACTIVE_IN_CONFIG`
- `CANONICAL_DEFAULT`

并支持 `--runtime-config`。

---

## Skill-P2-02 canonical ownership helper 对 thin shim 的“目标业务所有权”检查不足

仅证明根文件变薄不够。

helper 应递归检查 shim target 是否仍拥有：
- HTTP dispatch
- SQL / DB business methods
- progress/analysis semantics
- job lifecycle
- report generation
- incremental business logic

否则“把 monolith 从 root 移到 app/legacy_runtime.py”很容易得到形式上的好看结果。

---

## Skill-P2-03 Runtime Reliability 缺少连接池事务卫生 helper

当前 Skill 对 DB/data_version 很强，但没有专门验证：
- autocommit=False + read_only return
- rollback-on-return
- stale transaction reuse
- killed connection within ping-skip window
- multiple DatabaseManager different configs
- shared pool shutdown

建议新增 `audit_connection_pool_transactions.py`，并要求 MySQL integration evidence。

---

## Skill-P2-04 `audit_vnext_jobs.py` 只会抓 timeout stale job，抓不到本次两个核心 job bug

现有 helper 可检查：
- job project/scan identity
- active job data_version stale
- running heartbeat timeout

但抓不到：
1. 进程刚重启、heartbeat 仍新鲜但 worker 已不存在。
2. DB dedupe 带 scan_id、executor dedupe 不带 scan_id 导致 queued orphan。
3. shutdown 1 秒后 worker 仍活着。

建议扩充 durable owner / process generation / executor enqueue identity 检查。

---

## Skill-P2-05 Performance/UI 已要求跨层指标，但当前验收仍偏 DOM

Skill 文字 contract 已明确要求：
- HTTP request count
- DB query count/rows
- Sidecar decode
- payload bytes
- p95
- peak RSS

这点定位正确。

但当前仓库浏览器 gate 主要看 DOM count / request concurrency，没有把这些指标变成 required evidence。

建议 helper 增加：
- `resident_js_lines`
- `overlay_db_queries`
- `overlay_db_rows`
- `time_to_first_visible_ms`
- `time_to_target_line_ms`
- `max_response_bytes`
- `report_store_count`

以区分 DOM virtualization 与 data virtualization。

---

## Skill-P2-06 TEST_SELECTION_GAP：缺少 changed path → specialist test suite 的映射

Skill Maintainer 已有“只跑 modification-related tests”的理念，但缺少可执行映射。

建议建立例如：

- `app/db/**` → `tests/database + relevant tests/vnext`
- `app/jobs/**` → `tests/vnext/test_jobs.py + recovery fixture`
- `web/assets/js/coverage_enhance.js` → mock DOM + Playwright + API integration
- `app/api/**` → API integration + browser contract
- `app/upgrade/**` → tests/release
- security boundary → tests/security

这样既不跑全量，又不会漏改动相关测试。

---

## Skill-P2-07 事务正确性与性能优化的 ownership 边界需要更明确

本次 `read_only=True` 跳 rollback 很典型：
- 引入动机：Performance
- 后果：事务/数据正确性
- 源码 root cause：Change Review

建议在 routing contract 明确：
- commit/rollback/session/lease correctness → Runtime Reliability
- pool wait/ping/connection count/latency → Performance/UI
- 某次 commit 引入的源码 defect → Change Review 作为 root source finding，Runtime 提供 active evidence

避免同一个问题被三个 Skill 重复登记。

---

## Skill-P2-08 Executor 容量与 lifecycle 边界也需要更明确

建议：
- queue/thread/resource budget → Performance/UI
- durable job state/recovery/orphan/cancel → Runtime Reliability
- release freeze/drain/worker fencing → Release Governance
- source change correctness → Change Review

Maintainer 负责唯一 root owner 和 handoff，不重复开根因。

---

## Skill-P2-09 Release Skill 应把“rollback 前后 release identity 不同且恢复到 before identity”变成强门禁

现有 Release Skill 已强调真实 previous release/rollback evidence，方向正确。

建议再增加机器约束：
- `before_release_id`
- `target_release_id`
- `rollback_release_id`
- 必须 `rollback == before`
- 当 target 与 before 不同时，禁止 `previous_release = target` 作为真实 rollback evidence

---

# 6. Skill 总体评价

## 做得好的地方

最新 Skill 体系的**职责大方向已经比较清楚**：

- Change Review：源码 root cause / canonical ownership / security
- Runtime Reliability：当前 DB/Scan/job/data correctness
- Performance/UI：HTTP/DB/Sidecar/browser/DOM/缓存放大
- Release Governance：生产 inventory / migration / rollback / evidence
- Maintainer：跨域路由与 Skill drift

尤其以下原则是正确的，并且本次审计确实有用：
- provider/test 存在 ≠ runtime participation
- mock DOM ≠ real browser evidence
- transitional legacy ≠ canonical
- Gate 3 不能靠 fallback legacy endpoint 过关
- 性能必须分网络/DB/Sidecar/browser，不得把放大从一层搬到另一层

## 当前最大短板

**不是“Skill 分得太细”，而是“机器可执行能力落后于文字规范”。**

最突出三件事：
1. Change Review helper bundle 已出现语法损坏。
2. helper 规则与最新仓库目录/API/配置发生 drift。
3. HTTP/frontend/active-runtime 这类真正的 wiring 尚缺自动 contract gate。

---

# 7. 推荐修复顺序

## 第一优先级：先恢复“真值门禁”

1. 修复 Change Review 4 个 audit helper 的语法损坏。
2. 修正 canonical config path / runtime participation active-config 判断。
3. 统一 repo diagnostics 与 Skill helper contract/version。
4. CI 去掉依赖安装 `|| true`。
5. canonical frontend + 真 VNext HTTP contract integration 设为 required。

## 第二优先级：修 VNext 正确性 P1

6. 完成前端所有 legacy API → VNext API 迁移。
7. 增加 scan_id + repository_name 全链路 immutable identity。
8. 修复 save/progress/incremental/export/download。
9. 封死 Scan post-create mutation。
10. 修 read-only pool rollback。
11. 修 job cross-scan dedupe、fresh orphan recovery、graceful shutdown。
12. 限制 HTTP body、Sidecar ranges、analysis batch size。

## 第三优先级：再继续性能优化

13. 从 DOM virtualization 升级到 data virtualization。
14. 消除每 chunk 全文件 overlay DB 查询。
15. Sidecar store 顶层 LRU + 更细锁粒度。
16. bulk Scan ingest。
17. 明确全局 worker/resource budget。
18. 修 Python 3.6 threaded server fallback。

## 第四优先级：Release 证据

19. 在 Candidate 环境跑真实 MySQL integration。
20. 跑真实 VNext HTTP + canonical browser E2E。
21. 跑大文件真实数据 A/B：HTTP、DB rows、Sidecar、payload、p95、RSS。
22. 真实 previous → target → rollback rehearsal，验证 release identity 回到 before。
23. 最后才考虑把默认 `runtime_mode` 从 legacy 切到 VNext。

---

# 8. 本次仍缺失的证据

以下不能因静态代码检查而标记 PASS：

- 当前生产是否仍在 v10 baseline、实际启动参数和 Python 版本
- 当前生产是否显式设置 `runtime_mode`
- MySQL 真实 transaction/session 状态
- 当前 DB schema/migration 状态
- 当前 background job 表中是否已经存在 orphan jobs
- 真实 50k/100k 文件 p95 / RSS / DB rows / payload
- Chromium 中 variable-height virtual scroll 的真实漂移
- Nginx reverse-proxy/auth 实际暴露面
- staging rollback 是否真实恢复上一 release 文件树
- GitHub Actions 对该 SHA 的完整 run 结果（本次连接器状态信息不足以据此宣告运行成功或失败）

这些应该由对应 specialist Skill 在后续有执行环境时补证，而不是用源码推断。

---

# 9. 最终审计结论

**Revision `94f57cf3c358d8886b05dc3e097938a36375f193`：**

- 代码结构相较旧 monolith 有明显进步；
- Code Detail DOM 性能优化方向有效；
- VNext 服务、持久化 Job、Scan/Report identity、Release lifecycle 的骨架已经建立；
- 但 VNext 仍未成为端到端 canonical runtime；
- 当前最严重的问题是 frontend/API contract、Scan immutability、多仓库 identity、DB transaction hygiene、Job durable lifecycle；
- CI/diagnostics 还不足以阻止这些问题进入 HEAD；
- 最新 Skill 的文字职责边界总体合理，但 Change Review helper bundle 已损坏且机器能力发生 drift。

**当前发布判断：`NOT READY`（针对 VNext Gate 3 / VNext 生产切换）。**

如果生产仍使用 legacy v10/现有稳定路径，本报告不等价于“当前生产已经故障”；它说明的是：**不能把这份最新源码直接视为 VNext 已收敛、已完成 Gate 1–3、可以无条件切换生产。**

---

# 10. 整改执行记录（2026-08-20）

本次工作树已按上述清单完成代码整改，且没有把生产环境尚未取得的证据伪造为 PASS。

## 10.1 已完成的代码整改

- P1-01～P1-05：canonical Code Detail、analysis save、Progress/Developer/Incremental、Export download 和默认 VNext runtime 已统一到 VNext API；`server --config` 会严格加载指定配置，Candidate 配置断言为 `vnext / coverage_candidate / 19528`。
- P1-06～P1-12：Legacy 明确标记为 `TRANSITIONAL_LEGACY`；Scan 只允许在 construction 状态写入并由 `seal_scan()` 发布；Code Detail/文件哈希带 `scan_id + repository_name`；连接池、Job lease/recovery/shutdown、批量分析和批量 ingest 已收口。
- P1-13～P1-17：LCOV 路径歧义 fail-closed；Sidecar 与 HTTP body 有上限；诊断输出覆盖 active runtime、frontend/API contract、Scan immutability、canonical ownership；CI 依赖安装不再吞错，并强制运行 VNext、specialist 和 real-browser suites。
- P2-01～P2-25：overlay/Sidecar LRU、数据版本失效、批量 SQL、Progress SQL 分页、资源队列、连接断连只读重试、ReportRegistry 归属、数据虚拟滚动、事件委托、changed-path 测试映射和 rollback identity 校验已实现。

## 10.2 本地可复现证据

以下证据在本次整改工作树中已执行通过：

- Python：`88` 项全量测试通过，`1` 项真实 MySQL 测试因未设置 `COVERAGE_TEST_MYSQL=1` 跳过；新增 Job 入队失败、只读 MySQL 断连重试等针对性用例通过。
- Browser：真实 Chromium `5 passed`。
- 100k 虚拟滚动：`request_count=2`、`loaded_line_count=1268`、`dom_line_count=317`、`first_visible=true`、`scrolled_visible=true`；首屏和目标行耗时已写入性能证据。
- Architecture/contract：runtime participation、canonical ownership、legacy dependency、active runtime、frontend VNext API contract、Scan immutability、Sidecar registry 均通过。
- 兼容副本：root CSS/JS 与 `web/assets` canonical SHA 一致。

## 10.3 仍需外部环境补证的项目

以下项目不能由本地静态代码或 fixture 代替：真实 Candidate MySQL transaction/session、真实生产 schema/migration、生产 job orphan inventory、真实 50k/100k 数据集的 p95/RSS/DB rows、Nginx/auth 暴露面、真实 previous→target→rollback 文件树和 GitHub Actions run。Rollback rehearsal 现在对缺失真实 before/target identity 或 release endpoint 直接 fail-closed。

因此整改后的准确结论是：代码与本地 Gate 证据已收口，但 VNext 生产切换仍须完成上述外部证据，不将 `TRANSITIONAL_LEGACY` 宣称为 `RETIRED`。
