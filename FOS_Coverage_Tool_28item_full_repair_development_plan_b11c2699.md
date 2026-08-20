# FOS Coverage Tool 28 项联合优化——完整修复开发计划

> 基线仓库：`Chary-yu/fos_coverage_tool`  
> 基线分支：`main`  
> 修复计划基线：`b11c2699a10638f83fe2c9581d744d73eea70c3e`  
> 计划日期：2026-08-20  
> 当前结论：`NOT_READY`  
> 测试原则：默认只运行修改点相关的定向测试，不运行无关全量测试。  
> 数据原则：历史用户分析事实数据、索引、后台任务状态、项目状态不得因本次修复丢失、覆盖或被“重建替代”。

---

## 1. 修复目标

本轮不是继续“补模块”，而是把已经存在但未真正落入生产主路径的 28 项优化收口成一套可验证、可发布、可回滚的实现。

必须同时解决四类问题：

1. **正确性问题**：Code Detail Overlay 使用旧 `pending_lines` 快照导致待分析/已确认统计可能错误。
2. **运行时接线问题**：DB Pool、Bounded Executor、Progress Aggregate、Excel Streaming、Parse Once、Directory Signature、LCOV Path Index、Chunked Sidecar 等 provider 已存在，但主入口仍走旧逻辑。
3. **架构与安全问题**：根入口仍承载大量业务实现、配置/静态资源存在双份活动实现、写 API 没有可靠身份绑定。
4. **发布证据问题**：release/evidence manifest、backup、data hash、browser/performance evidence 存在 synthetic/placeholder/旧 commit 混用，能够产生 false-green `UPGRADE_SUCCESS`。

完成后必须达到：

```text
DEFINED_ONLY / TEST_ONLY
        ↓
RUNTIME_REFERENCED
        ↓
RUNTIME_WIRED
        ↓
RUNTIME_VERIFIED
        ↓
STAGING_VERIFIED
        ↓
PRODUCTION_READY
```

禁止再次用“文件存在 + 单测通过”替代真实主路径接线和真实验收。

---

# 2. 不可突破的修复红线

## 2.1 权威事实数据绝不由派生表反写

继续认定以下为权威事实/状态：

- `coverage_analysis`
- `coverage_line_index`
- `coverage_background_jobs`
- `coverage_project_state`

`coverage_file_state` 只能是派生聚合表：

```text
coverage_analysis + coverage_line_index
              ↓
       coverage_file_state
```

不得出现：

```text
coverage_file_state
       ↓
覆盖 / 修复 / 回写 coverage_analysis
```

## 2.2 Draft 语义保持不变

- Draft 仍计入待分析。
- Confirmed 必须是非 Draft 且 status 属于确认态。
- 保存成功必须推进/失效项目派生状态。
- Code Detail、Progress、Incremental 三条消费者链对“待分析”必须使用同一语义。

## 2.3 Legacy Sidecar 必须继续可读

新版本只能“读旧 + 写新”，不能要求升级时重建所有历史报告。

## 2.4 不允许 fake production evidence

以下 evidence 不能进入生产 Gate：

- mock MySQL backup；
- 固定的 1000/5000 行数；
- `pre_hash_xxx_verified` 类占位 hash；
- Node fake DOM 冒充 Chromium；
- synthetic microbenchmark 冒充真实浏览器/生产性能；
- checked-in 的 `UPGRADE_SUCCESS` 文本；
- 缺少 commit/build identity 的旧报告。

## 2.5 生产修复不使用无差别全量测试

每阶段只运行与修改行为相关的测试；若没有对应测试，新增 focused test，而不是用无关全量测试覆盖风险。

---

# 3. 28 项修复后的目标状态

| Item | 工作项 | 当前状态 | 本轮目标 |
|---|---|---|---|
| 1 | Analysis Overlay + data_version Cache | 部分接线且有正确性问题 | 修正静态/动态边界，真实主路径验证 |
| 2 | 网络 Chunk / DOM Batch 解耦 | 已接线 | 保持，实现真实浏览器验收 |
| 3 | 2~4 路有界 Chunk 并发 | 已接线 | 保持，实现取消/乱序真实验证 |
| 4 | MySQL Connection Pool | provider-only | 接入所有 request/job/runtime DB 路径 |
| 5 | Browser Region LRU | 已接线 | 保持，增加 eviction + draft survive E2E |
| 6 | Background Job Bounded Executor | provider-only | 替换裸 `threading.Thread`，支持恢复/限流 |
| 7 | coverage_file_state | provider-only | 成为 Progress 默认读取层，带 authoritative fallback |
| 8 | Additive Migration + Backfill | 部分实现 | 真正 migration/backfill/reconcile/readiness gate |
| 9 | Excel ZIP Streaming | provider-only | 主导出路径改为有界内存流式实现 |
| 10 | Inject Parse Once | provider-only | 主 Inject 每个源码只解析一次 |
| 11 | Directory Signature Incremental Hash | provider-only | 替换全量重复 hash |
| 12 | LCOV Path Lookup Index | provider-only | 替换 O(N×M) suffix 搜索 |
| 13 | Chunked Sidecar | reader/provider 部分存在 | 新 inject 写 chunked，Code Detail 按 chunk 读 |
| 14 | Legacy Sidecar Read | 已接线 | 保持并补混合/损坏测试 |
| 15 | 项目目录重整 | 目录存在、ownership 未收口 | 一个能力只保留一个 canonical implementation |
| 16 | 配置/状态/runtime 分离 | 双配置/根入口过重 | 单一配置来源 + 状态目录 + 薄入口 |
| 17 | 生产根目录稳定 | 基本满足 | 保持，不借重构迁移历史报告/MySQL |
| 18 | Release Identity | provider 存在、manifest 陈旧 | build-time 生成、runtime 只验证、不自愈掩盖漂移 |
| 19 | Pre/Post Data Hash | fake data | 真实数据库内容 hash |
| 20 | MySQL Dump + SHA256 | dry-run 可 mock | 真实可恢复 dump + 结构/恢复验证 |
| 21 | Schema Preflight | 基本存在 | 加强到实际 target schema + DDL |
| 22 | Sidecar/Registry Audit | 0 sidecar 可误 PASS | 实际 inventory + 完整性/逃逸/缺失检查 |
| 23 | Real Browser E2E | fake DOM | Playwright/Chromium 真实验收 |
| 24 | Performance A/B | Python 微基准 | baseline/candidate 同 workload 实测 |
| 25 | Path Mapping Audit | synthetic | 实际 LCOV/repository path 数据审计 |
| 26 | Security Review | 正则扫描过弱 | API auth/CORS/path/CI 等真实信任边界修复 |
| 27 | Manifest Upgrade/Rollback | 验证脚手架 | 真正停机、冻结、切换、回滚控制器 |
| 28 | Production Evidence | false-green | 可追溯、不可伪造、强一致 final gate |

---

# 4. 总体开发顺序

建议拆成 11 个阶段，严格顺序如下：

```text
Phase 0  修复门禁与测试骨架
   ↓
Phase 1  Code Detail Overlay / Sidecar 正确性
   ↓
Phase 2  DB Pool + Bounded Job Executor
   ↓
Phase 3  Progress derived aggregate
   ↓
Phase 4  Excel / ZIP 有界流式导出
   ↓
Phase 5  Inject / Incremental / Chunked Sidecar 主路径
   ↓
Phase 6  Canonical ownership / 目录与配置收口
   ↓
Phase 7  API 安全边界
   ↓
Phase 8  Release Identity / 数据安全 / Evidence
   ↓
Phase 9  Real Browser + Performance A/B
   ↓
Phase 10 Staging 停机升级 / 自动回滚演练
```

每一阶段未达到自己的 Exit Gate，不进入后一阶段。

---

# 5. Phase 0：先修“验收规则”，禁止继续 false-green

## 5.1 目的

在继续改业务代码之前，先确保后续每次提交都无法再通过弱证据冒充“完成”。

## 5.2 修改内容

### 修改 `.github/workflows/ci.yml`

当前问题：

- 仅 `workflow_dispatch`；
- Python 只覆盖 3.10/3.12；
- 没有生产 Python 3.6 compatibility gate；
- 没有最小 workflow permissions；
- 当前 smoke 不能区分 mock-DOM 与 real browser。

改为：

1. 保留 `workflow_dispatch`；
2. 增加 `pull_request` / `push` 的 path-aware targeted job；
3. 显式：

```yaml
permissions:
  contents: read
```

4. Python 3.10/3.12 跑功能定向测试；
5. 单独 `py36-compat` job：
   - changed runtime Python `py_compile`；
   - 运行明确支持 3.6 的 focused tests；
   - 禁止把 3.10/3.12 PASS 视作 3.6 PASS；
6. Node fake-DOM smoke 命名为 `mock-dom-regression`；
7. Real Browser job 命名为 `real-browser-e2e`，后续 Phase 9 才启用；
8. GitHub Actions 尽量固定到 immutable commit SHA；至少不能给 workflow 多余 write 权限。

### 修改 `production_evidence_manifest.json` 管理方式

当前 checked-in 文件不能再作为 release truth。

处理：

- 将当前文件标记为历史无效 evidence 或移到 `artifacts/history/invalid/`；
- 不再在 Git 仓库根目录保留可被运行时误读的 `UPGRADE_SUCCESS` 文件；
- `production_evidence_manifest.json` 改为每次 release 在独立 evidence 输出目录生成；
- 加入 `.gitignore`，防止把真实生产 evidence 当源代码提交。

### 修改 `release_manifest.json` 管理方式

- 当前 stale manifest 不再作为源文件长期维护；
- 由 build/release 阶段生成；
- runtime 只读验证，不允许发现不匹配时自动重写 manifest 后继续运行。

## 5.3 新增测试规则

新增/修改 focused tests：

- stale release manifest 必须 FAIL；
- placeholder data hash 必须 FAIL；
- mock backup evidence 必须 FAIL production gate；
- mock DOM evidence 不得满足 `real_browser_required=true`；
- `SKIPPED` / `UNAVAILABLE` 不得自动视作 PASS。

## 5.4 Exit Gate

只有 evidence validator 能正确拒绝当前仓库这类旧 `UPGRADE_SUCCESS`，Phase 0 才算完成。

---

# 6. Phase 1：修复 Code Detail Overlay / Sidecar 正确性

对应 Item：1、13、14；同时保护 2、3、5。

## 6.1 根因

当前 chunk/meta 快速路径把 Sidecar 生成时的 `pending_lines` 当成后续动态分析的候选全集。

正确模型应是：

```text
Sidecar = 静态事实
- source lines
- coverage_state
- all uncovered line numbers
- function ranges
- basic block/static region hints

Overlay = 动态事实
- status
- is_draft
- reviewer
- method/reason
- data_version
```

不能在 Sidecar 中把某次生成时的 `pending_lines/confirmed_count` 当长期真值。

## 6.2 修改 `source_reader.py`

### 必改 1：拆开 DB hash 与 Sidecar key

当前代码中存在 hash 命名混淆风险。

明确保留两个不同函数：

```python
compute_db_file_path_hash()      # 继续使用历史 DB 所需 MD5 32 hex
compute_sidecar_file_key()       # SHA256(normalized_path)[:32]
```

禁止再出现：

```python
compute_file_path_hash = calc_sidecar_file_key
```

因为现有 DB `coverage_analysis/coverage_line_index.file_path_hash` 已经是 MD5 兼容契约。

### 必改 2：静态 SourceContext

为 Sidecar 增加静态字段：

```text
uncovered_lines
static_total_uncovered_count
function_ranges
line/block static metadata
```

Sidecar 新格式不持久化当前分析状态作为权威数据。

### 必改 3：Legacy 读取归一化

Legacy `.source.json` 读取时：

- 读取其中 source/static 字段；
- 历史保存的 analysis fields 只作为兼容输入，不作为当前状态真值；
- 最终返回给 Code Detail 前必须再叠加当前 DB Overlay。

## 6.3 修改 `app/code_detail/overlay_cache.py`

Overlay cache key：

```text
(project_name, file_path_hash, project_data_version)
```

缓存值建议为：

```text
line_number -> {
  status,
  is_draft,
  reviewer,
  coverage_method,
  uncovered_reason,
  fill_status
}
```

要求：

- data_version 改变后旧 cache 不可复用；
- cache miss 只查当前文件分析 rows；
- 不根据旧 `pending_lines` 缩小 DB 查询集合；
- 可设置容量/TTL，但 version 必须是 correctness key。

## 6.4 修改 `code_detail_service.py`

### `/code-layout`

改为：

```text
SidecarStore.load_metadata()
        ↓
取得 static uncovered_lines + function ranges
        ↓
读取当前 project data_version
        ↓
AnalysisOverlayCache.get(file, version)
        ↓
对全部 static uncovered_lines 计算当前 pending
        ↓
build_code_regions(pending_lines)
```

`confirmed_count` 必须动态计算：

```text
confirmed = uncovered && !is_draft && status in CONFIRMED_STATUS_SET
```

不得再用：

```text
旧 pending - overlay changed lines
```

推导当前 confirmed。

### `/code-lines`

- static line 由 Sidecar chunk 获取；
- dynamic review fields 由 Overlay merge；
- 未加载 region 不应触发整文件 JSON read；
- explicit report_id mismatch 必须 fail closed。

## 6.5 修改 `app/code_detail/sidecar_store.py`

新 meta 至少包含：

```json
{
  "schema_version": 2,
  "report_id": "...",
  "project_name": "...",
  "file_path": "...",
  "sidecar_file_key": "...",
  "static_total_uncovered_count": 123,
  "uncovered_lines": [1, 5, 10],
  "total_lines": 1000,
  "function_ranges": [],
  "chunk_size": 2000,
  "chunks": [],
  "content_hash": "..."
}
```

完整性要求：

- meta/chunk atomic replace；
- chunk range 连续且不交叉；
- meta chunk list 必须真实存在；
- content hash 可验证；
- path realpath 不得逃出 `<report_root>/.source_cache/<report_id>`；
- symlink 必须按明确策略拒绝或限制。

读取顺序：

```text
new chunk sidecar
  ↓ missing
legacy source.json
  ↓ missing
FileNotFound / controlled fallback
```

## 6.6 前端保持项

`network chunk=500`、`DOM batch=250`、`concurrency=3`、LRU 不在本阶段重写。

只补：

- layout meta 中 confirmed/pending 变化后统计刷新；
- 取消/重进不得重复挂载；
- draft store 永远独立于 line LRU。

## 6.7 定向测试

至少：

1. 80 confirmed + 20 pending，无状态变化 -> `confirmed_count=80`；
2. 原 confirmed 改回 draft -> 重新进入 pending；
3. 原 pending 确认 -> 从 pending 消失；
4. 多个 status 混合；
5. data_version 改变 -> overlay cache miss；
6. new sidecar only；
7. legacy only；
8. both present -> new 优先；
9. corrupt new + valid legacy 的策略必须明确且测试；
10. wrong report_id / path escape fail closed。

建议运行：

```text
python -m unittest tests.code_detail.test_phase2_core -v
python -m unittest tests.code_detail.test_phase6_sidecar -v
python -m unittest test_code_detail_service.py -v
python -m unittest test_code_detail_api.py -v
node --check web/assets/js/coverage_enhance.js
```

---

# 7. Phase 2：MySQL Pool + Bounded Background Executor 真正接线

对应 Item：4、6。

## 7.1 修改 `app/db/connection_pool.py`

保留 provider，但补齐生产契约：

- `max_size`；
- `min_idle` 可选；
- acquire timeout；
- ping/reconnect；
- 归还连接前 rollback 未提交事务；
- broken connection 不回池；
- shutdown；
- metrics：active/idle/waiters/acquire_timeout/reconnect。

必须兼容当前生产 Python 版本。

## 7.2 新增 `app/db/manager.py`

把 `enhance_coverage.py` 内的 `DatabaseManager` 抽成 canonical DB manager。

原则：

```text
DatabaseManager 不再拥有“每次 connect”的策略
             ↓
       统一通过 ConnectionPool
```

提供明确 context：

```python
with db_manager.connection() as conn:
    ...
```

或者 manager 内部统一 acquire/release。

所有 request/job 结束必须释放连接。

## 7.3 修改 `enhance_coverage.py`

阶段性先改引用，不一次性删全部旧代码：

- import `app.db.manager.DatabaseManager`；
- 根文件旧 class 暂时变成 thin compatibility alias；
- 禁止两个 class 独立发展。

最终 Phase 6 删除旧实现。

## 7.4 修改 `app/jobs/bounded_executor.py`

要求：

- worker 数来自配置；
- queue 有上限；
- 相同 `(kind, project, data_version)` 不重复并发；
- cancellation token；
- shutdown/drain；
- 每个 worker 使用 DB pool，不创建长期裸连接；
- heartbeat 由统一 job service 更新。

## 7.5 新增 `app/jobs/service.py`

负责：

```text
create job
persist DB
submit executor
heartbeat
finish/fail/cancel
restart recovery
retention cleanup
```

`progress`、`full_detail_export` 作为 recoverable job。

服务启动时：

- 查询 DB 中 `running`/recoverable records；
- 超过 heartbeat 阈值的记录不能永久保持“running”；
- 按产品规则标 `interrupted/recoverable` 并安全重排队，或等待下一请求复用；
- 同一 job 不可多实例重复执行。

## 7.6 修改 `start_background_job()` 主路径

彻底删除：

```python
worker = threading.Thread(...)
worker.daemon = True
worker.start()
```

生产主路径全部改走 `BoundedJobExecutor`。

`threading.Timer` cleanup 也应改为：

- 周期性 maintenance loop；或
- executor scheduler / service cleanup；

避免每个 job 创建一个独立 Timer 线程。

## 7.7 状态目录

当前 `SCRIPT_DIR/background_jobs` 改为 configurable `state_root/jobs`。

建议配置：

```json
{
  "runtime_state": {
    "root": "/var/lib/onesensor-coverage",
    "jobs_dir": "jobs",
    "registry_dir": "report-registry"
  }
}
```

若为开发环境无权限，可 fallback 到明确的本地 state 目录，但 production preflight 必须确认目标目录可写。

## 7.8 定向测试

- pool max size；
- acquire timeout；
- broken connection reconnect；
- rollback-on-return；
- worker concurrency cap；
- queue overflow fail closed；
- duplicate key reuse；
- cancel；
- restart recovery；
- completed job output reload；
- terminal/service model不依赖交互 CMD 生命周期。

建议：

```text
python -m unittest tests.test_phase3_jobs_export -v
python -m unittest test_concurrency.py -v
```

并新增 `tests/database/test_connection_pool_integration.py`。

---

# 8. Phase 3：Progress `coverage_file_state` 真正成为默认聚合层

对应 Item：7、8。

## 8.1 设计调整：增加 project aggregate readiness

当前仅在 `coverage_file_state` row 上保存 `data_version`，不足以证明整个项目 aggregate 已经同步完成。

建议给 `coverage_project_state` additive 新增：

```sql
file_state_version BIGINT NOT NULL DEFAULT 0
```

语义：

```text
file_state_version == data_version
=> coverage_file_state 已完整反映该 project 当前 authoritative facts
```

否则 Progress 必须 fallback authoritative query。

## 8.2 修改 `scripts/upgrade/schema_v2_additive.sql`

必须只做 additive：

- `CREATE TABLE IF NOT EXISTS coverage_file_state ...`；
- `ALTER TABLE coverage_project_state ADD COLUMN file_state_version ...`，需用兼容的幂等策略；
- 索引按 progress/query 实际条件添加；
- 不 DROP/RENAME 核心事实列。

## 8.3 修改 `app/progress/file_state_service.py`

实现三层：

### 读 readiness

```text
SELECT data_version, file_state_version
FROM coverage_project_state
```

### Ready

```text
file_state_version == data_version
→ 读取 coverage_file_state
```

### Not Ready

```text
fallback_authoritative=True
→ authoritative facts query
→ meta 标明 fallback_reason
```

API meta 增加：

```json
{
  "source": "coverage_file_state|authoritative_facts",
  "data_version": 123,
  "file_state_version": 123,
  "fallback_reason": null
}
```

禁止 silent fallback 后仍对外声称 aggregate hit。

## 8.4 分析写入事务

单条/批量 review 写入建议统一事务边界：

```text
BEGIN
↓
写 coverage_analysis
↓
推进 coverage_project_state.data_version
↓
重算受影响 file 的 coverage_file_state
↓
设置 file_state_version = new data_version
↓
COMMIT
```

若任一步失败：ROLLBACK。

这样正常 review 写入后 aggregate 可以立即 ready。

## 8.5 Inject / bulk index 更新

Inject 会批量改变 `coverage_line_index`，不适合每文件都推进 project version。

流程改为：

```text
开始 inject
↓
标记/记录 aggregate 将失效
↓
同步全部 touched line index
↓
统一推进 data_version 一次
↓
按 touched files 或项目重建 coverage_file_state
↓
reconcile authoritative
↓
成功后 file_state_version = data_version
```

如果 rebuild/reconcile 失败：

- `file_state_version` 不更新；
- Progress 自动 authoritative fallback；
- 不影响用户事实数据。

## 8.6 修改 `scripts/upgrade/migrate_file_state.py`

Backfill 必须：

- 从 facts 表生成；
- idempotent；
- 不改变 facts；
- 完成后执行 project-level reconciliation；
- exact match 后才设置 `file_state_version=data_version`；
- reconciliation mismatch 返回非 0 / FAIL。

## 8.7 修改 Progress API/UI

`coverage_progress.js`：

- 接收 `meta.source`；
- aggregate fallback 时可展示轻量状态，例如“正在使用权威查询”；
- 保存 review 后 data_version 更新，页面 foreground/reload 能获取新结果；
- 不缓存旧 version 的 completed progress job。

## 8.8 定向测试

- fresh aggregate；
- stale aggregate -> authoritative fallback；
- analysis confirm 后版本同步；
- draft 仍算 pending；
- batch confirm 只推进一次 version；
- inject rebuild 成功；
- inject rebuild 失败后安全 fallback；
- backfill exact reconcile；
- mismatch fail closed；
- Progress result 与 legacy authoritative 逐字段一致。

建议：

```text
python -m unittest tests.progress.test_phase4_progress -v
```

新增 transaction integration test，使用隔离测试库，不使用 MagicMock 代替全部 SQL 行为。

---

# 9. Phase 4：Excel ZIP 导出改成真正有界流式

对应 Item：9。

## 9.1 当前问题

当前 `review_excel_by_dir` 路径会：

```text
fetch 整个项目 all_detail_rows
↓
全部放内存
↓
detail_rows_by_dir
↓
所有目录 futures 一次性提交
```

大型项目仍然有高内存峰值。

## 9.2 修改 `app/jobs/excel_streaming.py`

设计为：

1. 先取 directory summary；
2. 每次只处理有限个目录；
3. 每个目录 detail 采用 cursor/batch 查询；
4. XLSX 优先写临时文件而不是大 bytes 常驻内存；
5. ZIP 写完一个目录即释放对应 rows/temp；
6. in-flight directory tasks 有上限；
7. client disconnect 时取消未开始任务。

建议：

```text
MAX_INFLIGHT_DIR_EXPORTS = min(worker_count, 2~4)
DETAIL_BATCH_SIZE = configurable
```

不要一次把所有 directory future 放入列表。

## 9.3 修改 `DatabaseManager`

新增/固化：

```text
iter_review_excel_rows(project, dir, batch_size)
iter_full_detail_batches(...)
```

确保使用索引：

- project_name
- file_path_hash / path directory lookup strategy

禁止大型导出走无界 `fetchall()`。

## 9.4 修改 HTTP 导出

`send_review_excel_by_dir_response()` 改为调用 streaming service。

根 handler 只负责：

```text
validate request
↓
streaming service
↓
response
```

不再包含完整业务组装实现。

## 9.5 验收

必须比较 old/new：

- Excel 内容一致；
- 目录数量一致；
- row 数一致；
- ZIP 可正常解压；
- 中断下载不残留大 temp；
- 10万/百万级 detail 数据 peak RSS 明显受控；
- DB fetch 不再一次性取全部 detail rows。

建议定向测试：

```text
python -m unittest tests.test_phase3_jobs_export -v
python -m unittest test_export_formats.py -v
python -m unittest test_export_system.py -v
python -m unittest test_export_perf.py -v
```

---

# 10. Phase 5：Inject / Incremental / Chunked Sidecar 主路径收口

对应 Item：10、11、12、13、14、22、25。

## 10.1 修复 `app/inject/parse_once.py`

必须先修 hash identity：

`ParsedSourceArtifact` 同时保存：

```text
db_file_path_hash = historical MD5
sidecar_file_key   = SHA256[:32]
```

不要用 Sidecar key 写进 DB `file_path_hash`。

Artifact 一次解析产生：

```text
SourceContext static lines
function ranges
line_index_records
uncovered_lines
sidecar chunks/meta input
```

## 10.2 修改 Inject 主路径

`process_gcov_file_for_inject()` 从：

```text
extract_line_index_records(source_content)
↓
parse_source_lines_from_gcov_html(source_content)
```

改为：

```text
artifact = parse_gcov_source_once(source_content)
↓
artifact.line_index_records -> DB
artifact.source_context -> Sidecar writer
```

一个源文件只进行一次完整 parse。

## 10.3 修改 Sidecar writer

新 inject 默认：

```text
SidecarStore.save_chunked_sidecar()
```

而不是旧：

```text
save_source_sidecar(...source.json)
```

历史报告不主动迁移。

## 10.4 修改 `app/inject/directory_signature.py`

真正接入 `inject_coverage_report()` reuse 判断。

建议 manifest 不写入原始 LCOV source 目录内部；放在：

```text
<state_root>/signatures/<input-root-hash>.json
```

或者 output cache 明确目录。

Manifest key：

```text
relative_path
size
mtime_ns
sha256
```

损坏时全量重新计算 signature，但不能删除 source。

## 10.5 修改 `coverage_check.py`

在加载完 LCOV `coverage_data` 后，一次构建 path index。

不再每个 Git path：

```text
for source_file in coverage_data:
    suffix compare
```

改为：

```text
LCOVPathLookupIndex
exact
→ normalized
→ unique suffix
→ ambiguous fail closed
→ basename rejected
```

Multi-repo 必须以 repo identity 分 namespace。

输出 mapping statistics：

```json
{
  "exact": 100,
  "normalized": 20,
  "unique_suffix": 5,
  "ambiguous": 1,
  "miss": 2,
  "basename_rejected": 3
}
```

## 10.6 修改 `scripts/diagnostics/path_mapping_audit.py`

不再只跑 synthetic list。

支持输入：

- repositories config；
- LCOV `.info`；
- Git diff target paths；
- baseline audit JSON。

Gate：

- ambiguous 绝不自动映射；
- basename-only 绝不信任；
- multi-repo 不串仓；
- miss rate 不得无解释劣于 baseline。

## 10.7 修改 `scripts/diagnostics/sidecar_registry_audit.py`

当前必须补：

- registry 指向不存在目录应报错，而不是过滤掉；
- duplicate report identity；
- missing expected cache；
- orphan cache；
- meta/chunk JSON parse；
- chunk range；
- chunk file hash；
- symlink / realpath escape；
- mixed legacy/new policy；
- stale registry；
- report id 与目录 identity 不一致；
- 输出 legacy/new 数量和 pre/post diff。

不能出现：

```text
registered_report_count > 0
sidecar_count = 0
但无条件 AUDIT_PASSED
```

应由实际 report inventory 判断是否合理。

## 10.8 定向测试

- modern LCOV parse once；
- legacy LCOV parse once；
- DB MD5 与 Sidecar SHA key 不串；
- unchanged input 复用 signature；
- 单文件改动只 rehash 单文件；
- corrupt signature fallback；
- exact/normalized/unique/ambiguous/miss；
- multi-repo 同名文件；
- chunked write + read；
- legacy read；
- registry corruption；
- missing chunk；
- path escape。

建议：

```text
python -m unittest tests.incremental.test_phase5_inject_path -v
python -m unittest test_incremental_coverage.py -v
python -m unittest tests.code_detail.test_phase6_sidecar -v
```

---

# 11. Phase 6：Canonical Ownership / 目录与配置彻底收口

对应 Item：15、16、17。

## 11.1 目标原则

一个业务能力只有一个 active implementation。

允许旧路径存在 thin shim，但不能两份代码都在运行。

## 11.2 目标目录

建议：

```text
app/
  api/
    server.py
    handlers.py
  config/
    runtime_config.py
  db/
    manager.py
    connection_pool.py
  code_detail/
    service.py
    overlay_cache.py
    sidecar_store.py
  jobs/
    service.py
    bounded_executor.py
    excel_streaming.py
  progress/
    service.py
    file_state_service.py
  inject/
    service.py
    parse_once.py
    directory_signature.py
  incremental/
    service.py
    path_index.py

web/
  assets/
  templates/

scripts/
  diagnostics/
  maintenance/
  upgrade/

tests/
```

## 11.3 根 `enhance_coverage.py`

最终只保留：

```text
CLI 参数解析
兼容旧命令
调用 app.* service
```

以下必须迁出：

- DB implementation；
- background jobs implementation；
- progress aggregation；
- export implementation；
- inject parsing implementation；
- HTTP handler 业务代码。

## 11.4 静态资源 canonical path

选择 `web/` 为唯一源码：

```text
web/assets/js/coverage_enhance.js
web/assets/js/coverage_progress.js
web/assets/js/incremental_coverage.js
web/assets/js/incremental_developer_tasks.js
web/assets/css/coverage_enhance.css
web/templates/coverage_progress.html
```

`enhance_coverage.py` 的 source path 全部指向上述 canonical 文件。

根目录重复 JS/CSS/HTML：

- 第一阶段可保留 generated compatibility copy；
- 必须由脚本生成并 hash 对比；
- 不允许人工独立修改；
- 稳定后删除不再使用的 duplicate source。

## 11.5 配置 canonical ownership

当前根目录和 `config/` 下有两份 `coverage_config.json`。

为降低生产风险，建议：

- 生产现有根 `coverage_config.json` 暂时继续作为 canonical runtime config；
- `config/coverage_config.json` 改名为 `config/coverage_config.example.json`；
- 新增 `app/config/runtime_config.py` 统一读取；
- 支持 `COVERAGE_CONFIG_PATH` 显式覆盖；
- 如果未来迁移 config 路径，必须单独 release，不和本轮业务修复混在一起。

这样既消除双 active config，又保持生产路径稳定。

## 11.6 Runtime state

以下运行态不能继续散落在 source root：

- background job results；
- report registry；
- diagnostics output；
- production evidence；
- backup artifacts。

统一走 configurable runtime/evidence/backup roots。

## 11.7 验收

- runtime participation audit：所有 provider 均为 `RUNTIME_WIRED`；
- canonical ownership audit：无双实现；
- root JS/CSS 与 web source 无漂移；
- CLI 旧命令兼容；
- production root 不迁移历史 reports/MySQL/git repos/ownership workbook。

---

# 12. Phase 7：修复 API 安全与 reviewer 身份信任边界

对应 Item：26，属于 Change Review P1。

## 12.1 当前风险

写接口允许 payload 自行提交：

```text
reviewer
status
method
reason
```

而 production Nginx 示例会将 `/api/coverage` 暴露到允许办公网段。

因此 source 层必须建立真实“谁可以写、写入者是谁”的边界。

## 12.2 建议认证模型

生产默认：

```json
{
  "auth": {
    "mode": "reverse_proxy",
    "user_header": "X-Remote-User",
    "trusted_proxy_addresses": ["127.0.0.1", "::1"],
    "allowed_origins": ["https://coverage.example.internal"]
  }
}
```

开发模式可允许：

```text
auth.mode = disabled
```

但 Release Gate 禁止 production 使用 disabled。

## 12.3 修改 HTTP handler

所有 write endpoints：

- 只接受 `application/json`；
- 校验 Origin；
- 从可信 reverse proxy header 取得用户 identity；
- 不信任客户端提交的 reviewer；
- 服务端写入 reviewer = authenticated user；
- 未认证返回 401；
- 无权限返回 403；
- 日志记录 user/project/file/action，但不记录敏感凭据。

涉及：

- `POST /api/coverage`
- `POST /api/coverage/batch`
- 后续任何 mutate endpoint。

## 12.4 CORS

`Access-Control-Allow-Origin: *` 改为配置允许 origin。

生产通过 Nginx 同源访问时，默认只允许同源。

## 12.5 Nginx

修改 `nginx配置.txt` 示例：

- 保留办公网 `allow/deny`；
- 加 existing SSO / `auth_request` 或最低限度 `auth_basic`；
- 转发可信 `X-Remote-User`；
- 清除客户端自带同名 header 后再由 Nginx 设置；
- API 仍只 proxy 到 `127.0.0.1:9528`。

## 12.6 其他安全补强

- 所有文件/registry/cache path 做 realpath root containment；
- 禁止 report_id 导致目录逃逸；
- SQL 保持参数化；
- subprocess 禁止 shell=True；
- upgrade rm/mv/cp 目标必须经过 safe-root 检查；
- CI workflow 最小权限和 immutable actions；
- checked-in 文件不得包含真实 DB password/token。

## 12.7 定向测试

新增：

- no auth write -> 401；
- spoof reviewer -> 服务端忽略；
- trusted proxy user -> reviewer 正确；
- untrusted X-Remote-User -> 拒绝；
- disallowed Origin -> 拒绝；
- path traversal；
- report identity cross-fallback；
- batch write auth。

---

# 13. Phase 8：Release Identity / 数据安全 / Evidence / Upgrade Controller

对应 Item：18、19、20、21、22、25、27、28。

## 13.1 Item 18：Release Identity

### 修改 `app/release_identity.py`

当前 `get_current_release_identity()` 不应在 production 发现 manifest 与 HEAD 不一致时自动生成并写回，这会掩盖发布漂移。

改成两个动作：

```text
build_release_identity()     # build/release 阶段生成
verify_release_identity()    # runtime 只验证
```

生产 mismatch：FAIL。

Release Identity 至少：

```text
version
commit_sha
build_id
asset_hash
schema_version
built_at
artifact_sha256（如使用 release tar/zip）
```

### 增加 API

```text
GET /api/coverage/release
```

返回当前 runtime identity，方便 post-cutover 验证。

### JS/CSS identity

注入 HTML 增加 meta：

```text
coverage-build-id
coverage-asset-hash
```

JS 不再依赖硬编码 `ENHANCE_VERSION` 作为唯一 release truth；URL cache-busting 使用 manifest asset/build identity。

---

## 13.2 Item 19：真实 Pre/Post Data Hash

修改 `scripts/diagnostics/data_hash_gate.py`。

必须直接连接目标 MySQL，对核心表按稳定主键顺序流式 hash。

### `coverage_analysis`

至少 hash：

```text
project_name
file_path_hash
line_number
reviewer
status
is_draft
coverage_method
uncovered_reason
```

### `coverage_line_index`

hash：

```text
project_name
file_path_hash
file_path
line_number
line_text
block_start/end
block_type
function_name/hash
code_line_hash
code_occurrence
```

### `coverage_project_state`

hash：

```text
project_name
data_version
file_state_version
```

### `coverage_background_jobs`

在 freeze/drain 后 hash稳定业务字段。

输出每张表：

```json
{
  "row_count": 123,
  "sha256": "...",
  "first_key": "...",
  "last_key": "..."
}
```

禁止 hardcode counts/hash。

---

## 13.3 Item 20：真实 MySQL Backup

修改 `scripts/maintenance/mysql_backup.py`。

生产模式要求：

- 从实际 config 获取 database name；
- `mysqldump` exit=0；
- gzip 可完整解压；
- 文件尺寸合理；
- SHA256；
- 记录 mysqldump version；
- 记录 schema/table inventory；
- 运行 `verify_mysql_backup`；
- 推荐恢复到隔离 scratch DB 做 restore smoke。

`allow_mock_in_test=True` 只能用于测试 evidence class=`mock`，final production gate 必须拒绝。

---

## 13.4 Item 21：Schema Preflight

增强 `scripts/upgrade/schema_preflight.py`：

- 检查 DROP/TRUNCATE/RENAME；
- 检查 protected table column DROP/CHANGE/MODIFY 破坏兼容；
- 检查实际 target schema 是否已有列/索引；
- 输出 planned DDL diff；
- DDL 必须幂等；
- Python/SQL 兼容目标 MySQL 版本。

---

## 13.5 Item 27：真正的 Manifest-driven Upgrade

当前 `directory_manifest.json` 不是部署 manifest。

替换/新增：

```text
scripts/upgrade/deployment_manifest.json
```

每个文件动作明确：

```json
{
  "op": "ADD|MODIFY|MOVE|DELETE",
  "source": "...",
  "source_sha256": "...",
  "destination": "...",
  "backup_required": true
}
```

禁止 wildcard broad replace。

### `run_upgrade.py` 改造成真实 orchestrator

必须显式参数：

```text
--mode staging|production
--manifest <deployment_manifest>
--target-release <release_manifest>
--config <coverage_config>
```

默认不能 `dry_run=True` 后生成 `UPGRADE_SUCCESS`。

正式流程：

```text
PRECHECK
↓
VERIFY TARGET RELEASE
↓
DEPENDENCY PREFLIGHT
↓
FREEZE TRAFFIC / WRITES
↓
DRAIN BACKGROUND JOBS
↓
PRE DB HASH + SCHEMA + RUNTIME INVENTORY
↓
FULL MYSQL BACKUP + VERIFY
↓
APP / WEB / CONFIG / REGISTRY BACKUP
↓
STOP API
↓
ADDITIVE SCHEMA MIGRATION
↓
BACKFILL + RECONCILE coverage_file_state
↓
APPLY EXPLICIT FILE MANIFEST
↓
START API（traffic still closed）
↓
VERIFY RELEASE ENDPOINT
↓
VERIFY DB/API/SIDECAR/PATH
↓
REAL BROWSER SMOKE
↓
POST DB HASH
↓
FINAL EVIDENCE VALIDATION
↓
OPEN TRAFFIC
↓
UPGRADE_SUCCESS
```

### 自动回滚

在 traffic 未开放前 application failure：

```text
stop candidate
restore app/web/config from backup
restore old directory layout
start previous release
verify previous release identity
verify authoritative data hash
```

如果事实表 hash 出现非预期变化：

```text
DATA_SAFETY_HOLD
```

禁止自动覆盖生产 DB。

---

## 13.6 Item 28：Production Evidence Manifest

重写 `scripts/upgrade/evidence_manifest.py`。

每一 gate 必须记录：

```text
name
status: PASS|FAIL|SKIPPED|UNAVAILABLE
revision
command
exit_code
started_at/finished_at
host/environment
artifact_path
artifact_sha256
evidence_class
```

关键 evidence class：

```text
unit
integration
mock_dom
real_browser
synthetic_benchmark
production_database
production_backup
staging_cutover
production_cutover
```

Final Gate 必须明确要求：

- backup = real/verified；
- data hash = actual DB；
- browser = real_browser；
- target revision identity exact；
- unresolved P1 = 0；
- schema migration/reconcile PASS；
- rollback evidence available；
- release endpoint matches target；
- production mode auth enabled。

`SKIPPED/UNAVAILABLE` 不能被布尔转换为 true。

---

# 14. Phase 9：Real Browser E2E + 真实 Performance A/B

对应 Item：23、24，同时最终验收 1~14。

## 14.1 新增 Playwright/Chromium 测试

建议：

```text
tests/browser/package.json
tests/browser/lazy_collapse.spec.js
tests/browser/run_lazy_collapse_e2e.js
```

运行对象必须是：

- 实际 API server；
- 实际 generated report；
- Chromium；
- 非 fake DOM。

## 14.2 必测场景

至少：

1. 初始 expanded/collapsed；
2. 展开普通 region；
3. 正确显示 exact lines；
4. 编辑未提交 draft；
5. collapse/re-expand draft 不丢；
6. loading 中 Restore Default / cancel；
7. stale response 不 mount；
8. re-expand 仍可用；
9. 重复展开不出现 duplicate DOM lines；
10. 50k+ region chunk；
11. chunk failure + retry；
12. response out-of-order 行顺序正确；
13. LRU eviction；
14. eviction 后 draft survive；
15. navigate away/back；
16. legacy sidecar；
17. chunked sidecar；
18. report mismatch fail closed；
19. console error = 0；
20. unexpected failed network = 0。

## 14.3 Performance workload

固定同一数据集：

```text
A ~1k
B ~10k
C ~50k
D >=100k
Huge Function >=50k single function
```

必须记录 baseline/candidate 的：

### Code Detail

- `/code-layout` latency；
- `/code-lines` latency；
- request count；
- payload bytes；
- initial visible time；
- large-region expand time；
- Expand All time；
- browser peak memory；
- server RSS；
- DB query count；
- cache cold/warm。

### Progress

- authoritative query time；
- file_state query time；
- returned rows；
- DB query count；
- fallback frequency；
- project A/B dataset identity。

### Export

- rows；
- total time；
- peak RSS；
- temp usage；
- output hash/content parity。

### Inject / Incremental

- parse count；
- input files hashed count；
- path resolution CPU/time；
- sidecar generation size/time。

## 14.4 Gate

不设置脱离实测的绝对承诺，但以下必须 FAIL：

- OOM/SIGKILL；
- request explosion；
- 明显错误行顺序；
- duplicate DOM；
- draft loss；
- large regression 无解释；
- candidate 与 baseline workload/cache state 不一致却声称改善。

---

# 15. Phase 10：Staging 停机升级 + 回滚演练

本阶段不直接生产上线。

## 15.1 staging 必须模拟生产状态

至少包含：

- 生产结构相同的 app root；
- MySQL schema copy / sanitized data copy；
- Legacy Sidecar；
- Chunked Sidecar；
- background job rows；
- representative reports；
- Nginx/API route；
- production Python runtime compatibility。

## 15.2 演练两次

### 演练 A：正常升级

完整跑：

```text
freeze → backup → migration → backfill → switch → browser → hash → open
```

### 演练 B：故意失败回滚

在 application switch 后、traffic open 前主动触发失败，验证：

- candidate 停止；
- old app/web 恢复；
- old release identity 恢复；
- facts hash 不变；
- traffic 未错误开放；
- additive table 留存不影响旧版本；
- rollback 日志/evidence 完整。

只有两次都成功，才能生成 production upgrade candidate。

---

# 16. 文件级修改清单

以下是本轮应纳入开发范围的主要文件。

## 16.1 必改现有文件

### Runtime / API

- `enhance_coverage.py`
- `code_detail_service.py`
- `source_reader.py`
- `coverage_check.py`

### Code Detail

- `app/code_detail/overlay_cache.py`
- `app/code_detail/sidecar_store.py`

### DB / Jobs / Progress

- `app/db/connection_pool.py`
- `app/jobs/bounded_executor.py`
- `app/jobs/excel_streaming.py`
- `app/progress/file_state_service.py`

### Inject / Incremental

- `app/inject/parse_once.py`
- `app/inject/directory_signature.py`
- `app/incremental/path_index.py`

### Web

- `web/assets/js/coverage_enhance.js`
- `web/assets/js/coverage_progress.js`
- `web/assets/js/incremental_coverage.js`
- `web/assets/js/incremental_developer_tasks.js`
- `web/assets/css/coverage_enhance.css`
- `web/templates/coverage_progress.html`

### Release / Diagnostics / Upgrade

- `app/release_identity.py`
- `scripts/diagnostics/data_hash_gate.py`
- `scripts/diagnostics/path_mapping_audit.py`
- `scripts/diagnostics/perf_benchmark.py`（降级为 synthetic microbenchmark，不再冒充 final gate）
- `scripts/diagnostics/security_scanner.py`
- `scripts/diagnostics/sidecar_registry_audit.py`
- `scripts/maintenance/mysql_backup.py`
- `scripts/upgrade/schema_preflight.py`
- `scripts/upgrade/schema_v2_additive.sql`
- `scripts/upgrade/migrate_file_state.py`
- `scripts/upgrade/run_upgrade.py`
- `scripts/upgrade/evidence_manifest.py`
- `scripts/upgrade/directory_manifest.json`（替换用途或退役）

### Config / CI

- `coverage_config.json`
- `config/coverage_config.json`（改为 example 或退役）
- `.github/workflows/ci.yml`
- `.gitignore`
- `requirements-py36.txt`
- `nginx配置.txt`

## 16.2 建议新增文件

```text
app/config/runtime_config.py
app/db/manager.py
app/jobs/service.py
app/progress/service.py
app/inject/service.py
app/incremental/service.py
app/api/server.py
app/api/handlers.py

config/coverage_config.example.json

scripts/upgrade/deployment_manifest.json

artifacts/README.md   # 只描述目录，不提交生产 evidence

tests/database/test_connection_pool_integration.py
tests/progress/test_file_state_transaction.py
tests/security/test_api_auth.py
tests/release/test_evidence_authenticity.py
tests/release/test_upgrade_manifest.py
tests/browser/package.json
tests/browser/lazy_collapse.spec.js
tests/browser/run_lazy_collapse_e2e.js
```

## 16.3 建议退役/不再作为 canonical source

- 根目录重复 `coverage_enhance.js`
- 根目录重复 `coverage_progress.js`
- 根目录重复 `incremental_coverage.js`
- 根目录重复 `incremental_developer_tasks.js`
- 根目录重复 `coverage_progress.html`
- 根目录重复 CSS（若 web/assets 已成为 canonical）
- checked-in stale `release_manifest.json`
- checked-in `production_evidence_manifest.json`

注意：退役必须在所有引用切换和 compatibility test 完成后进行，不能第一步直接删。

---

# 17. 数据库修改计划

只允许 additive。

## 17.1 新表 `coverage_file_state`

继续作为 derived state。

建议字段：

```text
project_name
file_path_hash
file_path
total_uncovered
filled_total
draft_total
confirmed_total
coverable_total
uncoverable_total
redundant_total
last_updated
data_version / last_change_version
```

## 17.2 `coverage_project_state`

新增：

```text
file_state_version BIGINT NOT NULL DEFAULT 0
```

## 17.3 禁止变更

本轮禁止：

- DROP `coverage_analysis`；
- DROP `coverage_line_index`；
- truncate；
- 清空重建；
- 改变历史 `file_path_hash` 算法；
- 批量更新 reviewer/status/method/reason；
- 用 Sidecar key 替换 DB MD5。

---

# 18. API 合同修改

## 18.1 `/api/coverage/code-layout`

新增/稳定：

```text
project_name
file_path
report_id
total_lines
total_uncovered_count
confirmed_count
pending_count
data_version
regions
```

`confirmed_count/pending_count` 必须来自当前 overlay，不来自 Sidecar 历史 snapshot。

## 18.2 `/api/coverage/code-lines`

返回 static line + current overlay merge。

## 18.3 Progress

meta 增加：

```text
source
data_version
file_state_version
fallback_reason
```

## 18.4 `/api/coverage/release`

新增 current runtime release identity。

## 18.5 Write API

- authenticated identity；
- server-derived reviewer；
- allowed origin；
- JSON content type；
- clear 401/403/400/409。

---

# 19. 定向测试矩阵

| 修改域 | 必跑 |
|---|---|
| Overlay | current pending/confirmed、draft、version invalidation |
| Sidecar | legacy/new/both/corrupt/missing/path escape |
| DB Pool | concurrency/reconnect/rollback/timeout/shutdown |
| Job Executor | bounded queue/dedupe/recovery/cancel/heartbeat |
| Progress | transactional update/fallback/reconcile/version readiness |
| Export | parity/large data/disconnect/peak resource |
| Parse Once | modern/legacy/DB hash/Sidecar key parity |
| Directory Signature | unchanged/changed/corrupt manifest |
| LCOV Path | exact/normalized/unique/ambiguous/multi-repo |
| Architecture | runtime participation/canonical ownership/CLI compatibility |
| Security | auth/reviewer spoof/CORS/path trust |
| Release Identity | stale manifest/mismatched asset/commit |
| Backup | real dump structure/sha/restore smoke |
| Data Hash | actual DB stable streaming hash |
| Upgrade | staging success + forced rollback |
| Browser | actual Chromium full lifecycle |
| Performance | same baseline/candidate workload A/B/C/D |
| Python | production 3.6 compatibility for changed runtime paths |

不要求执行：

```text
python -m unittest discover
```

除非后续明确要求全量。

---

# 20. Commit / MR 拆分建议

不要重新形成一个“28 项大提交”。

建议至少拆成：

```text
R1  evidence guardrails + CI classification
R2  overlay correctness + static/dynamic Sidecar contract
R3  DB pool runtime wiring
R4  bounded job executor + recovery
R5  progress aggregate + transaction + migration
R6  Excel streaming
R7  parse-once + directory signature + path index
R8  chunked Sidecar writer + integrity audit
R9  canonical ownership + thin entrypoint + config cleanup
R10 API auth/CORS/reviewer identity
R11 release identity + real data hash + real backup
R12 deployment manifest + upgrade/rollback controller
R13 real browser E2E
R14 performance A/B + final release evidence
```

每个 MR 必须：

- 写明影响 Item；
- 指出 canonical runtime entrypoint；
- 列出 targeted tests；
- 说明未执行的 evidence；
- 不在 MR 描述中把 `TEST_ONLY` 写成 `RUNTIME_VERIFIED`。

---

# 21. 优先级

## P1：必须先修，未完成禁止发布

1. Overlay pending/confirmed 正确性；
2. DB Pool 主路径接线；
3. Background Executor 主路径接线；
4. Progress derived aggregate + authoritative fallback；
5. Excel streaming 主路径；
6. Parse Once / Sidecar DB hash identity；
7. Chunked Sidecar writer 主路径；
8. API reviewer/auth trust boundary；
9. Release Identity exact binding；
10. 真实 data hash；
11. 真实 MySQL backup；
12. Manifest cutover / rollback；
13. Production Evidence authenticity；
14. Real Browser E2E；
15. staging rollback rehearsal。

## P2：在发布前建议一并收口

- Directory Signature；
- LCOV Path Index 性能；
- canonical source cleanup；
- CI actions immutable pinning；
- 更完整的 security scanner；
- benchmark automation；
- evidence history archive。

---

# 22. 每阶段完成定义（DoD）

一个 Item 只有同时满足以下条件才能标记“完成”：

```text
[1] 代码实现存在
[2] 主生产 entrypoint 引用该实现
[3] 旧实现已退化为 shim 或不再 active
[4] targeted unit/integration tests PASS
[5] 目标 production Python/runtime compatibility PASS
[6] 对应 domain acceptance PASS
[7] evidence 绑定 exact commit/build
```

其中：

- Browser Item 还必须 real browser；
- Performance Item 还必须 baseline/candidate same workload；
- Release Item 还必须 staging/production evidence；
- Data Safety Item 还必须真实 DB/backup。

---

# 23. 最终 Release Gate

只有以下全部成立，才把当前 `NOT_READY` 改为 `READY`：

## Source / Change Review

- runtime participation 无 provider-only；
- canonical ownership 无双 active implementation；
- Python 3.6 compatibility 通过；
- 无 unresolved source P1；
- write API trust boundary 已修复。

## Runtime Reliability

- save → DB → data_version → file_state → API freshness 全链正确；
- background job 恢复/限流正确；
- path mapping miss/ambiguous 可解释；
- Sidecar/Registry 无 integrity violation。

## Performance/UI

- Chromium lifecycle 全通过；
- no stuck loading；
- no duplicate DOM lines；
- no lost draft；
- no cross-report data；
- A/B/C/D 无明显不可接受回退/OOM/request explosion。

## Release Governance

- target release identity exact；
- real MySQL dump verified；
- pre/post authoritative hash 可核验；
- additive migration + reconcile 通过；
- staging upgrade 成功；
- forced rollback 成功；
- evidence manifest authenticity PASS；
- traffic 只有 final gate PASS 后才开放。

---

# 24. 推荐的实际开发启动点

不要首先修改 `run_upgrade.py`。

正确顺序是：

```text
第一批：Phase 0 + Phase 1
```

也就是先完成：

1. evidence/CI 防 false-green；
2. Overlay 正确性；
3. DB hash 与 Sidecar key 正式拆开；
4. static Sidecar metadata 契约；
5. CodeDetailService 对全部 uncovered lines 重新计算当前 pending/confirmed；
6. 对应 focused tests。

这一批完成后再进入 DB Pool / Job Executor。

原因：Overlay/Sidecar 是当前已经存在的用户可见正确性风险，也是后续 Parse Once、Chunked Sidecar 和真实性能验收的基础。如果先继续接线/重构，会把错误状态模型扩散到更多模块。

---

# 25. 本轮明确不做的事情

除非后续另行批准，本修复计划不包括：

- 删除/重建历史分析数据；
- 强制重建所有历史 `coverage_line_index`；
- 批量转换所有 legacy Sidecar；
- 搬迁 MySQL 数据目录；
- 搬迁 Git repository；
- 搬迁全部历史报告根；
- 大规模 UI 视觉改版；
- 新增与 28 项无关的业务功能；
- 无关全量单元测试；
- 在修复尚未验收前直接生成生产升级脚本。

---

# 26. 最终预期

完成本计划后，28 项应从目前的：

```text
“很多 provider 已落盘，但部分未接线 + evidence false-green”
```

转变为：

```text
单一 canonical implementation
+ 明确 static/dynamic state contract
+ 有界 DB/job/export/browser resource
+ authoritative fallback
+ real browser/performance evidence
+ authentic zero-data-loss release evidence
+ 可执行、可回滚的 production cutover
```

届时再重新执行一次完整 28 项联合审计，只有所有 P1 关闭且 staging rollback 已证明，才进入生产升级脚本生成阶段。
