# FOS Coverage Tool 下一次大版本：28 项联合优化详细开发计划

> 状态：开发计划，不包含代码补丁  
> 日期：2026-08-19  
> 适用范围：当前生产基线 v10 → 下一次停机大版本升级  
> 核心约束：允许停机升级；历史分析数据不得丢失；失败必须可回滚；默认只运行修改点相关的定向测试，不运行无关全量单元测试。

---

## 1. 总体目标

本次版本不是单一性能优化，而是一次有边界的“性能 + 架构 + 数据安全 + 发布治理”升级。

最终目标：

1. 大型代码详情页显著降低后端重复扫描、数据库访问、HTTP 请求和浏览器内存占用。
2. Progress 从按“未覆盖行规模”扫描逐步转为按“文件规模”聚合。
3. 大型 Excel/ZIP 导出由高峰值内存模式转为有界流式处理。
4. Inject / Incremental 减少重复 HTML 解析、重复文件 Hash 和 O(N×M) 路径搜索。
5. Sidecar 支持新分块格式，同时继续读取旧 `.source.json`。
6. 项目内部代码目录完成职责拆分，但生产根目录、MySQL 和历史报告位置原则上保持不变。
7. 下一次升级采用停机、冻结写入、完整备份、pre/post 内容 Hash、additive migration 和自动回滚。
8. 所有优化都必须服从“历史用户分析事实数据不可被重建、覆盖或删除”。

---

## 2. 不可突破的设计红线

### 2.1 四张核心事实/状态表必须保留

以下现有表继续保持兼容：

- `coverage_analysis`
- `coverage_line_index`
- `coverage_background_jobs`
- `coverage_project_state`

其中：

- `coverage_analysis` 是用户分析结论的重要事实源。
- `coverage_line_index` 是覆盖率行索引和继承/路径匹配的重要事实基础。
- `coverage_project_state.data_version` 继续承担派生缓存失效版本。
- `coverage_background_jobs` 保持后台任务持久化和恢复语义。

本次优化不得通过以下方式“解决问题”：

- DROP/重建上述表；
- TRUNCATE；
- 清空后重新导入；
- 为性能目的重新生成全部历史分析；
- 用新的聚合表反向覆盖事实表；
- 在升级时强制重跑 Git、LCOV 或全部 `coverage_line_index`。

### 2.2 新数据库结构只允许 Additive Migration

可以新增：

- 新表；
- 安全索引；
- 有默认值或可空字段；
- 派生数据。

本计划中的 `coverage_file_state` 必须是可重建的派生聚合表：

```text
coverage_analysis + coverage_line_index
            ↓
      coverage_file_state
```

禁止反向依赖：

```text
coverage_file_state
            ↓
覆盖/重写 coverage_analysis
```

### 2.3 生产根目录原则上不变

本次允许整理项目内部目录，但优先保持当前 production application root 不变。

目录重构重点是：

```text
runtime code
configuration
persistent metadata
generated web
cache/temp
tests
upgrade/diagnostics
```

之间的边界，而不是顺便搬迁：

- MySQL；
- 历史报告；
- ownership workbook；
- Git 仓库；
- Nginx Web 根；
- 用户分析数据。

### 2.4 Sidecar 必须“读旧写新”

新版本允许写新的 Chunked Sidecar，但必须支持：

```text
先读新 chunk sidecar
        ↓
不存在
        ↓
读 legacy *.source.json
        ↓
仍不存在
        ↓
fail closed
```

不能要求下一次升级期间批量重建全部历史报告。

### 2.5 默认只跑定向测试

每阶段只运行本阶段修改相关的测试和验收。

不执行：

```text
python -m unittest discover
```

之类无差别全量测试，除非之后用户明确要求。

---

## 3. 28 项工作总表

| 编号 | 工作项 | 优先级 | 类型 | 主要依赖 |
|---|---|---|---|---|
| 1 | Analysis Overlay + `data_version` Cache | P1 | Code Detail | 19、24 |
| 2 | 网络 Chunk 与 DOM Batch 解耦 | P1 | Frontend/API | 1 |
| 3 | 2~4 路有界 Chunk 并发 | P1 | Frontend/API | 2 |
| 4 | MySQL Connection Pool / Connection Reuse | P1 | Backend | 18、19 |
| 5 | Browser Region Line Cache LRU | P1 | Frontend | 2、3 |
| 6 | Background Job Bounded Executor | P1 | Runtime | 4 |
| 7 | `coverage_file_state` Progress 聚合层 | P1 | Database | 8、19、21 |
| 8 | Additive Migration + Backfill | P1 | Database | 19、20、21、28 |
| 9 | Excel ZIP Streaming | P1/P2 | Export | 4、6 |
| 10 | Inject Parse Once | P2 | Generator | 24 |
| 11 | Directory Signature Incremental Hash | P2 | Generator | 18 |
| 12 | LCOV Path Lookup Index | P2 | Incremental | 25 |
| 13 | Chunked Sidecar | P2 | Storage | 14、22 |
| 14 | Legacy Sidecar Read Compatibility | P1 | Storage | 22 |
| 15 | 项目代码目录结构重整 | P1/P2 | Architecture | 16、17、27 |
| 16 | 配置/持久化状态与 runtime code 分离 | P1 | Architecture | 15 |
| 17 | 保持生产根目录稳定 | P1 | Deployment | 15、27 |
| 18 | Release Identity 统一 | P1 | Release | 无 |
| 19 | Pre/Post 数据内容 Hash 门禁 | P1 | Data Safety | 18 |
| 20 | 完整 MySQL Dump + SHA256 | P1 | Data Safety | 18 |
| 21 | Schema Migration Preflight | P1 | Database Safety | 8 |
| 22 | Sidecar / Registry 完整性检查 | P1/P2 | Storage Safety | 13、14 |
| 23 | Real Browser Lazy Collapse E2E | P1 | Acceptance | 1~5、13~14 |
| 24 | 性能 A/B 基线门禁 | P1 | Acceptance | 18 |
| 25 | Path Mapping 完整审计 | P1/P2 | Data Quality | 12 |
| 26 | Security 边界扫描 | P2 | Security | 15、16 |
| 27 | Manifest 驱动停机升级 / 自动回滚 | P1 | Deployment | 15~22 |
| 28 | Production Evidence Manifest | P1 | Release Governance | 全部 |

---

## 4. 开发阶段与依赖顺序

不建议 28 项平行开发，建议拆成 8 个阶段：

```text
Phase 0  基线、数据安全、发布身份
        ↓
Phase 1  目录与运行时边界整理
        ↓
Phase 2  Code Detail 核心性能
        ↓
Phase 3  后台任务与导出资源治理
        ↓
Phase 4  Progress 数据库聚合
        ↓
Phase 5  Inject / Incremental 生成链优化
        ↓
Phase 6  Sidecar 新旧兼容存储
        ↓
Phase 7  联合验收 + 停机升级 + 回滚
```

任何阶段未通过自己的退出门禁，不进入后一阶段。

---

# 5. Phase 0：建立基线与“零数据丢失”门禁

涉及：18、19、20、21、24、25、26、28。

这是整个计划的第一阶段。没有这一层，后续性能优化即使代码正确，也无法证明“没有损坏生产数据”。

## 18. Release Identity 统一

### 目标

解决同一显示版本可能对应多个 Commit 的问题。

统一身份至少绑定：

```text
display_version
commit_sha
build_id
asset_hash
build_time
schema_version
```

### 实现建议

新增统一 Release Metadata：

```json
{
  "version": "vNext",
  "commit_sha": "...",
  "build_id": "...",
  "asset_hash": "...",
  "built_at": "...",
  "schema_version": 2
}
```

Python、JS、静态页面和 Evidence Manifest 都引用同一 Release Identity。

### 验收

- 同版本不同 Commit 必须能被检测；
- JS/CSS 内容变化必须导致 asset hash 变化；
- 服务 API 能暴露当前 Release Identity 或至少能从部署文件准确读取；
- 升级脚本必须校验目标 identity 后才能 cutover。

### 回滚

Release metadata 是代码资产，不改业务数据，恢复旧应用文件即可。

---

## 19. Pre/Post 数据内容 Hash 门禁

### 目标

把“数据没丢”从 COUNT 校验提升到内容校验。

### `coverage_analysis`

对以下关键字段稳定排序后分块 Hash：

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

原则上不把纯更新时间字段作为主要业务 Hash；如要校验 timestamp，单独生成辅助 Hash。

### `coverage_line_index`

至少：

```text
project_name
file_path_hash
line_number
block_start_line
block_end_line
block_type
function_hash
code_line_hash
code_occurrence
```

### `coverage_project_state`

保存：

```text
project_name
data_version
```

### `coverage_background_jobs`

升级前必须确认无可恢复 running/queued job，再保存最终状态分布。

### 验收

正常停机迁移后：

- 核心事实表 row count 不减少；
- `coverage_analysis` 业务内容 Hash 不变化；
- `coverage_line_index` 业务内容 Hash 不变化；
- `data_version` 若发生变化必须能被升级行为明确解释；
- 不允许项目状态消失。

### 失败策略

任何核心事实 Hash 非预期变化：

```text
禁止开放用户流量
→ 停止新版本
→ 进入回滚/数据库调查
```

---

## 20. 完整 MySQL Dump + SHA256

### 升级前必须产生

```text
full.sql.gz
full.sql.gz.sha256
schema.sql
critical-counts.json
critical-content-hashes.json
```

### 备份完成门禁

只有同时满足：

- mysqldump 成功退出；
- gzip 文件非空；
- SHA256 计算成功；
- dump 可做基本结构检查；
- 备份目录可读；

才能继续停机迁移。

正常 code/additive-schema 回滚不优先恢复数据库 Dump。Dump 是最后安全网。

---

## 21. Schema Migration Preflight

### 目标

在生产数据库之前识别：

- destructive DDL；
- unsafe NOT NULL；
- unique/index 冲突；
- 主键变化；
- 字段类型/charset/collation 风险；
- 非幂等迁移。

### 本版本约束

允许：

```text
CREATE TABLE coverage_file_state ...
CREATE INDEX ...
```

原则上禁止：

```text
DROP coverage_analysis...
DROP coverage_line_index...
ALTER existing business identity columns...
```

### 开发验收

先在隔离的 Schema/生产数据副本上执行：

```text
pre snapshot
migration
post snapshot
diff
```

旧事实数据 Hash 必须不变。

---

## 24. 性能 A/B 基线

### 数据集

至少四档：

- A：约 1,000 行；
- B：约 10,000 行；
- C：约 50,000 行；
- D：100,000+ 行或项目最大现实文件；
- 额外准备一个 50,000+ 行单函数压力样本。

### 每档记录

```text
/code-layout median/p95
initial batch median/p95
normal region expand
>50k region expand
Expand All
HTTP request count
API payload bytes
initial DOM node count
Expand All DOM node count
backend RSS
browser RSS
cold cache
warm cache
```

后续每个性能阶段都与 Phase 0 Baseline 对比。

---

## 25. Path Mapping 基线审计

输出：

```text
exact
normalized
unique_suffix
ambiguous_suffix
basename_only
miss
```

发布门禁：

- 新优化不得增加 path miss；
- `basename-only` 不得被自动当作可信唯一映射；
- ambiguous suffix 必须 fail closed 或明确降级。

---

## 26. Security 基线扫描

重点：

```text
path traversal
report_id escape
unsafe realpath
SQL string composition
shell=True
os.system
unsafe rm -rf
sidecar public exposure
registry writable trust
dynamic HTML/JS injection
```

静态命中只作为审计热点，需要人工确认。

---

## 28. Production Evidence Manifest 骨架

Phase 0 先建立空 Evidence Manifest，后续追加：

```text
release identity
source commit
target commit
targeted tests
runtime compatibility
schema
data hash
path mapping
performance
browser e2e
security
sidecar/registry
upgrade
rollback
deployment
```

只有 Final Gate 全部通过后才能把 candidate 标记为 production。

---

# 6. Phase 1：目录与运行时边界整理

涉及：15、16、17。

这个阶段只整理职责和引用，不引入性能行为变化。

## 15. 项目代码目录结构重整

### 推荐目标结构

```text
<production-root>/
├── app/
│   ├── api/
│   ├── db/
│   ├── code_detail/
│   ├── progress/
│   ├── jobs/
│   ├── inject/
│   └── incremental/
│
├── web/
│   ├── assets/
│   │   ├── js/
│   │   └── css/
│   └── templates/
│
├── scripts/
│   ├── upgrade/
│   ├── diagnostics/
│   └── maintenance/
│
├── tests/
│   ├── code_detail/
│   ├── incremental/
│   ├── progress/
│   ├── database/
│   └── browser/
│
├── config/
│
└── <legacy-compatible entrypoints>
```

### 兼容策略

第一版目录重构保留极薄旧入口，例如：

```text
enhance_coverage.py
```

继续作为原 CLI/systemd 入口，但内部 import 新模块。

### 迁移 Manifest

每个文件记录：

```text
old_path
new_path
class
operation
old_hash
new_hash
consumers
rollback_action
```

### 定向测试

- Python import smoke；
- CLI `--help`；
- `inject` 参数解析；
- `incremental` 参数解析；
- server import/bootstrap；
- 静态资源定位；
- ownership/config 路径读取。

### 验收

- 无 duplicate active implementation；
- 旧 shim 只转发，不保留第二份业务逻辑；
- 所有移动后的 import/resource path 有测试；
- production root 不变。

---

## 16. 配置/持久化状态与 Runtime Code 分离

### 分类

Configuration：

```text
coverage_config.json
repositories.json
ownership workbook path/config
```

Persistent Metadata：

```text
report registry
background job result metadata
release state
```

Generated Web：

```text
generated reports
incremental pages
```

Disposable Cache：

```text
new chunk sidecar cache
temporary benchmark output
temporary export .part
```

### 原则

- 部署 runtime code 不覆盖 configuration；
- 清理 cache 不删除 persistent metadata；
- upgrade rollback 不用恢复用户数据；
- generated reports 只有明确要求才重新生成。

---

## 17. 保持生产根目录稳定

本版本不主动修改 application root。

目标：

```text
production root：稳定
内部 app/web/scripts/tests：重构
```

这样尽量避免同时修改：

- systemd `WorkingDirectory`；
- `ExecStart`；
- Nginx root/alias；
- backup root；
- log path；
- report registry root。

---

# 7. Phase 2：Code Detail 核心性能优化

涉及：1、2、3、4、5。

## 1. Analysis Overlay + `data_version` Cache

### 当前问题

`SourceContext` 缓存与分析状态刷新耦合，Chunk 请求可能重复：

```text
fetch_records(file)
→ rebuild analysis map
→ traverse all context.lines
```

### 新设计

拆成：

```text
StaticSourceContext
    source
    coverage
    function ranges
    block metadata
    static regions

AnalysisOverlay
    project
    file_path_hash
    data_version
    status/reviewer/draft/method/reason
```

缓存 Key：

```text
(project_name, file_path_hash, review_scope, data_version)
```

### 失效

成功保存分析：

```text
DB transaction commit
→ increment data_version
→ overlay cache naturally stale
```

### 定向测试

1. 同 data_version 连续 `code-lines` 不重复 fetch 全文件 records；
2. 保存后 data_version+1；
3. 下一次请求得到新状态；
4. Draft 仍计入 pending；
5. confirmed 状态不回退；
6. cache eviction 不改变结果。

---

## 2. 网络 Chunk 与 DOM Batch 解耦

建议独立配置：

```text
NETWORK_CHUNK_LINES = 1000~2000（最终由 benchmark 决定）
RENDER_BATCH_LINES = 200~400
```

网络一次多取，DOM 仍小批 yield。

定向测试：

- 1 行、边界行、跨 chunk；
- 非整除结尾；
- 50k region；
- cancellation；
- retry；
- no duplicate lines；
- loadGeneration 仍有效。

---

## 3. 2~4 路有界 Chunk 并发

对超大 region：

```text
chunk queue
↓
max 2~4 active
↓
按行号顺序提交到 renderer
```

不能简单 `Promise.all` 全部 chunk。

必须处理：

- cancellation；
- Restore Default；
- timeout；
- 单 chunk failure；
- retry；
- response reorder；
- stale generation；
- concurrent expand same region。

---

## 4. MySQL Connection Pool / Connection Reuse

### 约束

必须兼容：

```text
Python 3.6.8
PyMySQL 0.10.1
```

优先轻量、自控实现，不为此引入大型数据库框架。

### Pool 能力

```text
min/max connection
borrow timeout
ping/reconnect
rollback dirty transaction
return-to-pool
shutdown close-all
```

### 定向测试

- 多线程 borrow/return；
- broken connection reconnect；
- transaction rollback；
- pool exhaustion；
- API finish 不误关闭整个 pool；
- server shutdown close。

---

## 5. Browser Region Line Cache LRU

增加：

```text
global cached lines budget
large-region priority eviction
LRU access
```

LRU 清理源码 line DTO 时：

- ReviewDraftStore 不清；
- 用户未保存编辑不丢；
- 再展开允许重新请求；
- loading/state machine 不被破坏。

定向测试：

```text
expand
→ edit draft
→ collapse
→ eviction
→ re-expand
→ draft still exists
```

---

# 8. Phase 3：后台任务与导出资源治理

涉及：6、9。

## 6. Background Job Bounded Executor

新结构：

```text
Job Registry
    ↓
Bounded Executor
    ↓
N workers
    ↓
progress/export
```

必须保留：

- DB job persistence；
- restart recovery；
- data_version invalidation；
- duplicate job reuse；
- cancellation；
- retention cleanup。

增加：

- queue length；
- worker count；
- overload response；
- per-kind concurrency；
- shutdown/drain。

---

## 9. Excel ZIP Streaming

新设计：

```text
directory summary
→ current directory
→ keyset/batched detail rows
→ build XLSX
→ write ZIP
→ release directory memory
→ next directory
```

如果保留并发，只允许有界 2~4 个目录。

验收：

- 输出内容与旧版本对等；
- 文件数/目录数一致；
- 百万级 detail 模拟数据不一次 materialize 全部；
- Peak RSS 明显下降；
- client disconnect 正常释放；
- `.part` 清理正确。

---

# 9. Phase 4：Progress 数据库聚合层

涉及：7、8。

## 7. `coverage_file_state` 聚合层

定位：

> 可重建的派生状态表，不是事实源。

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
data_version
updated_at
```

主键建议：

```text
(project_name, file_path_hash)
```

更新策略：

- Inject / line-index sync 更新 `total_uncovered`；
- Review save 只更新受影响文件；
- 聚合记录保留版本/更新时间；
- 第一版保留旧 authoritative query fallback。

---

## 8. Additive Migration + Backfill

停机后：

```text
读取 coverage_line_index
LEFT JOIN coverage_analysis
GROUP BY project,file
→ INSERT coverage_file_state
```

Backfill 必须：

- 只读旧事实表；
- 不修改旧事实表；
- 可重复执行；
- 支持幂等；
- 有进度；
- 最终 reconciliation。

Reconciliation：

```text
old authoritative summary
vs
new file-state aggregate
```

必须核对：

- total_uncovered；
- filled；
- draft；
- confirmed；
- 各状态计数。

回滚时 v10 直接忽略 `coverage_file_state`，原则上不 DROP 新表。

---

# 10. Phase 5：Inject / Incremental 生成链优化

涉及：10、11、12、25。

## 10. Inject Parse Once

新流程：

```text
read source content
    ↓
parse once
    ↓
ParsedSourceArtifact
    ├── line index records
    ├── SourceContext / sidecar
    ├── function ranges
    └── metadata
```

要求 line-index 和 sidecar 使用同一解析结果。

---

## 11. Directory Signature Incremental Hash

每文件 manifest：

```text
relative_path
size
mtime_ns
sha256
```

新一轮：

```text
path/size/mtime 未变
→ 复用旧 sha

发生变化
→ 重新 hash
```

manifest 缺失/损坏时必须退回完整 hash，不能误复用旧报告。

---

## 12. LCOV Path Lookup Index

加载 LCOV 时构建：

```text
exact_map
normalized_map
suffix_index
basename_index
ambiguity_index
```

匹配优先级：

```text
exact
→ normalized exact
→ unique safe suffix
→ ambiguous => no match
```

多仓库同名文件不能串仓。

---

## 25. Path Mapping 最终审计

完成 12 后重新运行路径审计。

Gate：

- miss 不能比 baseline 增加；
- ambiguous 不自动映射；
- normalization 有 repository identity；
- multi-repo 同名文件不串仓。

---

# 11. Phase 6：Chunked Sidecar + Legacy Compatibility

涉及：13、14、22。

## 14. 先实现 Legacy Read Compatibility

先抽象：

```text
SidecarStore
├── load_metadata
├── load_ranges
└── load_legacy
```

新格式启用前，旧 `.source.json` 必须通过统一接口正常读取。

测试：

- legacy only；
- new only；
- both present；
- corrupt new + valid legacy 策略；
- wrong report_id；
- missing sidecar；
- path escape。

---

## 13. Chunked Sidecar

推荐：

```text
.source_cache/<report_id>/<file-key>/
├── meta.json
├── lines-000000-001999.json
├── lines-002000-003999.json
└── ...
```

`meta.json`：

```text
schema_version
project
file identity
total_lines
function ranges
pending/static metadata
chunk index
content hash
```

读路径：

```text
/code-layout
→ 只读 meta

/code-lines 40001~40500
→ 只读覆盖该范围的 chunk
```

只对新生成/明确重新 inject 的报告写新格式，历史报告不强制迁移。

---

## 22. Sidecar / Registry 完整性检查

检查：

- malformed registry；
- missing registered dirs；
- missing report_id cache；
- orphan cache；
- duplicate report id；
- path escape；
- new/legacy mixed state；
- stale registry。

审计阶段不自动删除。

---

# 12. Phase 7：联合验收、Release、停机升级

## 23. Real Browser Lazy Collapse E2E

必须使用 Chromium/Playwright 或等价真实浏览器。

必测：

1. 默认 expanded/collapsed；
2. 展开普通 Region；
3. 编辑但不提交；
4. collapse/re-expand 后 draft 仍在；
5. Expand All；
6. 加载中 Restore Default；
7. loading 清理；
8. placeholder 可再次点击；
9. 超大 region chunk；
10. chunk failure/retry；
11. initial-batch mismatch；
12. navigate away/back；
13. static asset version；
14. console errors；
15. failed network requests；
16. LRU eviction 后 re-expand；
17. legacy sidecar；
18. new chunk sidecar。

退出标准：

- 无 stuck loading；
- 无 duplicate lines；
- 无 lost draft；
- 无 wrong region；
- 无 cross-report data；
- 无 fatal console error。

---

## 24. 最终性能 A/B

对 Phase 0 的 A/B/C/D workload 复测。

目标区间只作为工程目标，不作为无实测支撑的承诺：

```text
50k+ 大函数展开：约 2~4x 改善目标
Progress 大项目：约 5~30x 改善目标
Excel peak RSS：约下降 50~90% 目标
```

发布必须以实测结果为准。

---

## 26. 最终 Security Review

新增重点：

- chunk file path；
- meta chunk index trust；
- symlink；
- registry roots；
- pool transaction state；
- migration SQL；
- upgrade rollback `rm/mv/cp`；
- generated HTML/JSON injection。

---

## 27. Manifest 驱动停机升级 / 自动回滚

正式流程：

```text
PRECHECK
↓
FREEZE USER WRITES
↓
DRAIN BACKGROUND JOBS
↓
PRE SNAPSHOT
↓
FULL MYSQL BACKUP
↓
APP/WEB/CONFIG/REGISTRY BACKUP
↓
STOP API
↓
ADDITIVE MIGRATION
↓
BACKFILL coverage_file_state
↓
DATA HASH VERIFY
↓
DIRECTORY MIGRATION
↓
APPLICATION SWITCH
↓
STATIC ASSET SWITCH
↓
START API（traffic still closed）
↓
API / DB / BROWSER / PERFORMANCE SMOKE
↓
POST SNAPSHOT
↓
FINAL DATA HASH DIFF
↓
OPEN TRAFFIC
↓
UPGRADE_SUCCESS
```

Cutover 前拒绝：

- active/recoverable background job；
- inject/incremental/inherit worker；
- backup 不完整；
- hash snapshot 失败；
- schema preflight fail；
- targeted staging test fail；
- runtime compatibility fail。

### 自动回滚

如果 additive migration 后但流量开放前失败：

```text
stop candidate
restore app/web
restore old directory layout
start v10
leave additive table unused
verify old data hash
```

如果发现事实表出现非预期变化：

```text
keep traffic closed
preserve current DB copy
compare backup
determine restore strategy
```

不能直接覆盖现场数据库。

---

## 28. Final Production Evidence Manifest

记录：

```text
production baseline
target release
commit SHA
artifact hashes
runtime compatibility
targeted tests
browser E2E
performance baseline/candidate
schema migration
pre/post row counts
pre/post content hashes
path mapping
sidecar audit
security audit
upgrade manifest
backup hashes
service/API verification
rollback evidence
UPGRADE_SUCCESS
```

只有 critical checks 全部通过并出现 `UPGRADE_SUCCESS`，才能更新 production baseline。

---

# 13. 开发提交建议

不要把 28 项放进一个巨大 Commit。

建议形成逻辑独立的阶段提交：

```text
A. release/data safety foundations
B. runtime directory refactor
C. code-detail overlay/cache
D. chunk/concurrency/LRU
E. DB pool/job executor
F. Excel streaming
G. progress aggregate + migration
H. inject parse/signature/path
I. sidecar compatibility
J. chunked sidecar
K. release/upgrade integration
```

每组必须：

- 可独立审查；
- 有对应定向测试；
- 不把未完成 Schema 逻辑和运行时代码混成不可运行状态；
- 中间 Commit 至少可 import/compile。

---

# 14. 定向测试矩阵

| 修改领域 | 必跑测试 |
|---|---|
| 目录结构 | import、CLI、resource path、server bootstrap |
| Overlay Cache | cache hit、data_version invalidation、draft/confirmed |
| Chunk | range boundary、out-of-order、retry、cancel |
| LRU | eviction、draft survive、re-expand |
| DB Pool | concurrency、rollback、reconnect、shutdown |
| Job Executor | queue、reuse、recovery、cancel |
| Progress Aggregate | backfill、dual update、fallback、reconcile |
| Excel Streaming | content parity、large export、disconnect |
| Inject Parse Once | modern/legacy LCOV、line-index/sidecar parity |
| Signature | unchanged reuse、changed rehash、corrupt manifest fallback |
| Path Index | exact/normalized/ambiguous/multi-repo |
| Sidecar | legacy/new/both/missing/corrupt/wrong-report |
| Browser | full Lazy Collapse lifecycle |
| Migration | isolated DB pre/post |
| Upgrade | staging dry-run + rollback |
| Release | identity/evidence manifest |

默认不执行与这些改动无关的全量测试。

---

# 15. 数据安全专项验收标准

以下任意一项失败都不能开放系统。

### `coverage_analysis`

- 行数不减少；
- 业务内容 Hash 不变化；
- Draft 不丢；
- reviewer/status/method/reason 不丢。

### `coverage_line_index`

如果本次升级不明确要求重建：

- 行数不减少；
- 内容 Hash 不变化。

### `coverage_project_state`

- 项目不能消失；
- `data_version` 变化必须有明确原因。

### 历史报告

- 原目录存在；
- 原 HTML 不因升级强制重建；
- Legacy Sidecar 仍可读取。

### 配置

- `coverage_config.json` 保留；
- repository mapping 保留；
- ownership config/workbook 保留。

---

# 16. 性能验收目标

## Code Detail

记录：

```text
initial render
/code-layout
initial batch
large region
Expand All
request count
DB query count
browser/server RSS
```

优先保证：

```text
正确性不回退
→ 请求数量下降
→ warm cache latency 下降
→ 50k/100k 不 OOM
```

## Progress

同时比较：

```text
legacy authoritative query
vs
coverage_file_state query
```

第一版保留 fallback。

## Export

记录：

```text
wall time
peak RSS
output size
row/file counts
```

## Inject / Incremental

记录：

```text
directory signature
HTML parse
DB sync
path lookup
total
```

---

# 17. 目录迁移验收

完成后检查：

```text
旧 runtime implementation 不可达/已移除
legacy shim 只有转发
config 不在 release overwrite 范围
persistent metadata 不在 disposable cache 范围
generated web 与 runtime code 分离
systemd 启动路径明确
Nginx 静态路径明确
backup 路径明确
```

禁止存在两套业务实现都可能被 import 的状态。

---

# 18. 回滚专项设计

## 应用级回滚

必须可以单独恢复：

- runtime code；
- JS/CSS；
- root Web assets；
- 目录 MOVE/DELETE；
- systemd 变更（若有）。

## DB 回滚

优先：

```text
additive table 留着
旧 v10 忽略
```

只有事实表异常改动时才评估 DB restore。

## Sidecar

旧格式不删除，因此：

```text
v10 rollback
→ 继续读取 legacy source.json
```

## 新聚合表

```text
v10 rollback
→ 不读取 coverage_file_state
```

---

# 19. 最终发布 Gate

### 数据

- [ ] 完整 DB dump + SHA256
- [ ] 核心表 pre/post count
- [ ] 核心事实内容 pre/post hash
- [ ] 无非预期 data_version 变化
- [ ] additive migration reconciliation 通过

### 功能

- [ ] 保存 confirmed 正确
- [ ] Draft pending 正确
- [ ] Incremental unanalyzed 刷新正确
- [ ] Progress 正确
- [ ] Export 正确
- [ ] Legacy/new Sidecar 正确

### Lazy Collapse

- [ ] initial batch
- [ ] normal expand
- [ ] huge region
- [ ] retry
- [ ] cancellation
- [ ] Expand All → Restore Default
- [ ] LRU eviction + re-expand
- [ ] Draft survive
- [ ] no duplicate DOM

### 性能

- [ ] A/B/C/D benchmark 已保存
- [ ] 无多倍 regression
- [ ] C/D 无 OOM/SIGKILL
- [ ] 请求数量符合预期
- [ ] Progress 新路径有实测收益
- [ ] Excel RSS 有实测收益

### 架构

- [ ] 目录 Manifest 完整
- [ ] 无双 runtime implementation
- [ ] production root 稳定
- [ ] configuration/persistence 已隔离

### 发布

- [ ] Release Identity 唯一
- [ ] Targeted tests 通过
- [ ] Python 3.6 compatibility 通过
- [ ] Real Browser E2E 通过
- [ ] Security Review 无阻断项
- [ ] Sidecar/Registry Audit 通过
- [ ] Path Mapping 不退化
- [ ] rollback dry-run 通过
- [ ] Evidence Manifest valid
- [ ] `UPGRADE_SUCCESS`

---

# 20. 明确不在本版本一起做的事项

## Virtual DOM / Virtual Scroll

暂不纳入本次 28 项联合升级。

原因：它会同时改变行锚点、滚动、Review Panel 生命周期、导航、DOM 查找、多行 block 高度和 Draft UI 挂载。它适合在本次后端/数据库/Sidecar/目录架构稳定后独立开发与验收。

---

# 21. 实施优先顺序摘要

## 第一优先级：必须完成

```text
18 Release Identity
19 Data Hash Gate
20 MySQL Backup
21 Migration Preflight
28 Evidence Manifest
15~17 Directory/Persistence Boundary
1 Overlay Cache
2 Chunk Separation
4 Connection Pool
6 Bounded Jobs
7~8 Progress Aggregate
14 Legacy Sidecar Compatibility
23 Browser E2E
24 Performance Gate
27 Upgrade/Rollback
```

## 第二优先级：高价值

```text
3 Chunk Concurrency
5 Region LRU
9 Excel Streaming
10 Parse Once
12 Path Index
13 Chunked Sidecar
22 Sidecar Audit
25 Path Audit
```

## 第三优先级：低风险补强

```text
11 Incremental Signature
26 Security automation hardening
```

目标仍是完整交付 28 项。

---

# 22. 最终交付物

开发完成后，下一大版本至少具备：

1. 重构后的代码目录。
2. 兼容旧入口的运行方式。
3. Code Detail Overlay Cache。
4. Chunk/Concurrency/LRU。
5. MySQL Connection Pool。
6. Bounded Job Executor。
7. `coverage_file_state` Schema + migration/backfill。
8. Progress fallback/reconciliation。
9. Streaming Export。
10. Parse Once。
11. Incremental Signature Manifest。
12. LCOV Path Index。
13. Legacy + Chunked Sidecar。
14. Release Identity。
15. Data Hash/Snapshot。
16. Migration Preflight。
17. Sidecar/Registry Audit。
18. Real Browser E2E。
19. A/B Benchmark Evidence。
20. Path Mapping Evidence。
21. Security Evidence。
22. Upgrade Manifest。
23. 自动回滚升级脚本。
24. Production Evidence Manifest。
25. 完整备份与 Hash 证据。
26. 定向测试结果。
27. pre/post 数据一致性结果。
28. 明确的 `UPGRADE_SUCCESS` 生产成功证据。

---

# 23. 最终开发原则

技术优先级固定为：

```text
数据正确性
    >
可回滚
    >
兼容旧生产数据
    >
功能正确
    >
性能
    >
代码整洁度
```

即使某项性能优化可以更快，只要它要求破坏历史事实数据、强制重建生产数据，或者让 v10 无法安全回滚，就不采用该实现。

最终版本应达到：

> 可以接受较长停机升级，但停机窗口中的任何操作都不能造成历史用户分析事实数据丢失；性能优化、目录重构、Schema 新增和 Sidecar 升级全部必须建立在可验证、可回滚、向后兼容的数据边界之上。
