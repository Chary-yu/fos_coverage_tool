# FOS Coverage Tool Gate A～Gate F 详细开发与验证总方案

> 文档版本：v1.2（Skill 联合审计闭环版）  
> 更新日期：2026-08-21  
> 适用项目：`Chary-yu/fos_coverage_tool`  
> 代码基线：`main`（具体 Candidate SHA 由本次 Evidence Manifest v2 的 `candidate_revision` 固定）
> 上游设计基线：`FOS_Coverage_数据库兼容与分析继承升级方案_v2.0.md`  
> 生产证据基线：`fos_full_inventory_20260820_135937`  
> 首要硬约束：**兼容旧库是数据库设计最高级硬约束。任何 Gate、DDL、Migration、Repository、Service、API、Job、Inheritance Engine、UI 或发布脚本均不得破坏 Legacy → VNext 的零损失迁移契约。**


## 0. v1.2 联合审计闭环声明

本版本不是在 v1.0 末尾追加整改说明，而是把审计发现直接合并进 Gate A～F 的正式工程契约。上一轮审计摘要的计数文字存在口径误差：实际逐项列出了 **P1-01～P1-26 共 26 项、P2-01～P2-34 共 34 项、Info-01～Info-04 共 4 项**。v1.2 按实际编号 **64 项全部纳入设计闭环**。

### 0.1 新增的全局强约束

1. **HC-01 旧库兼容**：Legacy authoritative facts 必须可无损迁入独立 Empty VNext Target DB；无法证明的历史 Git/Block 身份保持 unknown/unverified。
2. **HC-02 单一状态权威**：共享 `AnalysisRecord` 只保存分析内容；draft/confirmed/inherited/covered-carry 等当前状态只属于 `AnalysisLineLink`。
3. **HC-03 前驱唯一**：Candidate 创建时把当时 `current_scan_id` 固化为 `predecessor_scan_id`。后续所有 repository 只能在这一个前驱 Scan 内独立判定，禁止按 repo 向更老历史搜索。
4. **HC-04 物理仓库互斥**：锁定的是实际 Git clone/common-dir 对应的 physical repository resource，而不是 project-scoped logical repository。
5. **HC-05 Fencing 原子性**：fencing token 永久单调；token 校验必须与 checkpoint/业务写/发布处于同一 DB 原子操作，旧 worker 不得先查后写。
6. **HC-06 输入不可变**：Scan Import 接受后使用受控 staged artifact；恢复只读取经 SHA256 绑定且不可变的 staged `.info`，不重新依赖用户原始路径。
7. **HC-07 自动继承组织事实**：`AnalysisBlock` 只表示人工保存范围；自动继承使用独立 `InheritanceGroup`，不得伪造“用户选择过的新 Block”。
8. **HC-08 Progress 守恒**：`pending_total = ordinary_pending + inherited_pending + manual_draft_pending`，三类互斥且总量守恒。
9. **HC-09 CURRENT-only mutation**：人工分析、确认继承、拒绝、撤销等写操作只允许作用于当前 `current_scan_id`；历史 Scan 只读。
10. **HC-10 发布证据真实性**：所有 Gate/Release evidence 必须带 candidate revision、host、command、exit code、timestamp、artifact SHA256、evidence class；占位文件、synthetic success label 不得推进 Gate。
11. **HC-11 最终 Target 只读验收**：最终 production Candidate DB 在开流量前只做只读验证；写行为验证必须在 rehearsal DB 或明确隔离、可证明完全清理的测试域完成。
12. **HC-12 无 P0/P1 风险接受**：本项目 `READY_WITH_ACCEPTED_RISK` 只允许残余 P2/Info；任何 unresolved P0/P1 一律 `NOT_READY`。

### 0.2 64 项审计问题关闭映射

| 审计项 | v1.2 关闭位置 |
|---|---|
| P1-01 DB alias 绕过同库检查 | A2 runtime DB fingerprint |
| P1-02 Legacy 时间仅 side record | A4 target provenance table |
| P1-03 rehearsal 可只用构造数据 | A9 真实生产备份恢复演练 |
| P1-04 Record/Link draft 双权威 | B3/B5/14.2 单一状态权威 |
| P1-05 Legacy 无可信 Block | B4/B6/D7 `block_identity_verified=0` 禁止自动 Block 继承 |
| P1-06 scan_key 未重定义 | B2 `scan_identity_v2` |
| P1-07 partial split 缺 CAS | B7 revision CAS/row lock |
| P1-08 logical repo 锁错层 | B1/C3 physical resource identity |
| P1-09 fencing 不够原子 | C3/C5/C9 DB atomic fencing |
| P1-10 `.info` 不可恢复 | C4 staged immutable import artifact |
| P1-11 lock/job/enqueue 补偿不清 | C6 creation/compensation matrix |
| P1-12 checkpoint 可倒退 | C5 sequence CAS |
| P1-13 predecessor 可能 per-repo 回溯 | C2/D1 固化 predecessor_scan_id |
| P1-14 缺 InheritanceGroup | D7 独立 group/membership |
| P1-15 Decision 无幂等键 | D15 decision_run_id + unique key |
| P1-16 Progress 漏 draft | E3/14.3 三项守恒公式 |
| P1-17 API 只是建议 | E1 冻结 Target API Contract v1 |
| P1-18 历史 Scan 可否写不清 | E2 CURRENT-only mutation |
| P1-19 Gate E parity 范围太窄 | E13/E14 完整 VNext parity matrix |
| P1-20 optimistic revision 不完整 | E6-E8 Record/Relation/Rejection revision bundle |
| P1-21 Freeze 未物理阻断所有 writer | F6 process/service/worker DB writer fence |
| P1-22 最终 Target 写测试 | F8 最终 Target 只读验收 |
| P1-23 最终 SHA 未重做 source review | F1/F8 exact-SHA source/security/canonical review |
| P1-24 parser toolchain 未成为 release preflight | D5/F4 dependency/toolchain gate |
| P1-25 Evidence 无统一 provenance | 4/F14 Evidence Manifest v2 |
| P1-26 rollback 未证明回到 before release | F10 before/after identity equality gate |
| P2-01 DDL 非事务但无 migration ledger | A3 schema migration ledger |
| P2-02 anomaly 混入 semantic hash | A8 authoritative hash 与 anomaly ledger 分离 |
| P2-03 percent 单位假设 | A7 raw value + unit provenance |
| P2-04 legacy result_path 直接激活 | A7 provenance-only + revalidation |
| P2-05 physical path DB/config authority 不清 | B1 runtime config authority |
| P2-06 repository rename/alias/retire 不清 | B1 repository lifecycle contract |
| P2-07 verified/provenance 重叠 | B2 identity field retirement map |
| P2-08 冗余 identity 字段 | B5 DB consistency guards |
| P2-09 `initial_analysis_record_id` 不 immutable | B4 originating_record + initial_content_hash |
| P2-10 新表缺精确 FK/index/delete | B13 DDL freeze requirements |
| P2-11 Scan status 迁移不清 | C2 status migration matrix |
| P2-12 durable handler 恢复注册未定义 | C6 versioned recovery handler registry |
| P2-13 generic stale recovery 与 import recovery 顺序 | C10 bootstrap recovery ownership |
| P2-14 SEALED 后 publish 失败状态不清 | C2/C9 ABORTED immutable candidate |
| P2-15 Job transition 只文字 | C4 machine transition matrix |
| P2-16 FAILED/ROLLED_BACK 边界 | C2/F10 precise semantics |
| P2-17 Git rename detection 未显式关闭 | D3 deterministic git flags |
| P2-18 lexer 未定义 line splice/translation | D4 lexical contract |
| P2-19 function signature canonical form 不完整 | D6 exact canonical function identity |
| P2-20 preprocessor 漏 `#else` | D9 `#else` branch identity |
| P2-21 dependency 可能在 `.info` 外 | D10/D11 same-repo source universe |
| P2-22 parser unresolved rate 不可观测 | D16/E14 metrics |
| P2-23 technical failure DecisionLedger 口径不一 | D15 job/import failure ledger authority |
| P2-24 83 规则多源 | D20 machine-readable canonical contract |
| P2-25 pending 查询无 pagination | E1 cursor pagination hard limit |
| P2-26 角色权限不清 | E2 role/permission matrix |
| P2-27 性能无 PASS/FAIL budget | E15 relative regression budgets |
| P2-28 workload 规模不固定 | E15 fixed workload tiers |
| P2-29 asset cache invalidation 未验 | E14 shared asset identity test |
| P2-30 final DB identity 未入 release manifest | F1/F7 database runtime identity |
| P2-31 free disk 无安全公式 | F3 disk capacity formula |
| P2-32 acceptance window 无退出条件 | F11 48h + scans/restarts criteria |
| P2-33 risk acceptance 语义冲突 | 4/F15 只允许 P2/Info |
| P2-34 Nginx/auth trust boundary 未单列 | F8 production proxy/auth acceptance |
| Info-01 ROLLED_BACK 语义混用 | C2/F10 ABORTED vs ROLLED_BACK |
| Info-02 `origin=inherited` 容易误导 | B3/B5 content origin 与 relation origin 分离 |
| Info-03 task 无 owner Skill | 附录 B 增加 root_owner/secondary_owner |
| Info-04 evidence naming 无版本 | 4.4 Evidence Manifest v2/schema_version |

---

# 1. 文档目的

本文档把已经封板的数据库兼容与分析继承方案，进一步拆解为 **Gate A～Gate F 六个可直接实施、可验证、可验收的开发 Gate**。

目标不是形成概念性 Roadmap，而是给开发人员一份可以按任务逐项执行的工程实施基线。每个 Gate 都包含：

- 开发目标与边界；
- 当前代码可复用资产；
- 新增/修改文件建议；
- 数据库表、字段、约束与迁移策略；
- Service / Repository / Job / API / UI 设计；
- 正常路径、失败路径、恢复路径；
- 修改点相关测试；
- 运行时/数据库/浏览器验证；
- Evidence 交付物；
- Gate PASS / BLOCKED / INCOMPLETE 标准；
- 与后续 Gate 的接口契约。

本项目可以在一个 Candidate 开发线上连续高强度推进，但 **Gate 不能被跳过**。后续 Gate 可以提前做不依赖前序结果的准备工作，但不得用“后面会修”替代当前 Gate 的权威契约闭环。

---

# 2. 总体实施原则

## 2.1 一个 Candidate 开发线，六个内部 Gate

推荐开发结构：

```text
Candidate Development Line
  ├─ Gate A  Legacy Compatibility Contract
  ├─ Gate B  Repository + Analysis Canonical Domain
  ├─ Gate C  Scan Lifecycle + Durable Import + Atomic CURRENT
  ├─ Gate D  Deterministic Inheritance Engine
  ├─ Gate E  API / UI / Browser Acceptance
  └─ Gate F  Candidate Release / Cutover / Rollback / Skill Drift
```

所有 Gate 使用同一套 Canonical VNext 代码路径，不允许为了赶进度再创建第二套独立业务实现。

## 2.2 兼容旧库是最高硬约束

任何表重构必须满足：

```text
Legacy Source DB
      ↓
Empty VNext Target DB
      ↓
Legacy → VNext semantic migration
      ↓
VNext → Analysis Domain
      ↓
Inheritance Extension
```

禁止：

- Legacy DB 原地套 VNext Schema；
- 删除旧权威事实来简化新模型；
- 让历史人工分析重新填写；
- 根据当前 Git HEAD 伪造历史 commit；
- Current 与 Candidate 同时写一个数据库；
- 用派生统计替代权威事实迁移证明。

## 2.3 单一权威原则

| 业务事实 | 唯一权威 |
|---|---|
| 当前正式 Scan | `coverage_project_state.current_scan_id` |
| Repository 逻辑身份 | `coverage_repositories.id` |
| Scan 时仓库身份 | `coverage_scan_repositories` |
| 物理源代码行 | `coverage_lines` |
| 分析内容（结论类别/方法/原因/备注） | `coverage_analysis_records` |
| 物理行当前分析关系及 review state | `coverage_analysis_line_links` |
| 用户一次选中范围 | `coverage_analysis_blocks`（仅人工事实） |
| 拒绝继承事实 | `coverage_inheritance_rejections` |
| 自动继承组织事实 | `coverage_inheritance_groups` + membership via LineLink |
| 继承判定证据 | `coverage_inheritance_decisions` |
| Physical Git resource 任务互斥 | `coverage_repository_resource_locks` + monotonic fencing generation |
| Import 恢复点 | `coverage_import_checkpoints` |

禁止两个表同时拥有同一业务事实的当前权威。

## 2.4 不跑全量测试

开发阶段默认只执行修改点直接相关测试。

每个 Gate 的测试分为：

1. `unit`：模块内部确定性规则；
2. `db-integration`：真实 SQL/事务/Schema 行为；
3. `service/api-integration`：跨 Repository/Service/API；
4. `runtime`：真实 VNext runtime；
5. `browser`：只有 UI/DOM/network 生命周期相关 Gate 使用真实 Chromium/Playwright；
6. `release rehearsal`：Gate F 独立 Candidate/DB 演练。

Mock DOM、SQLite fixture、静态 helper 都只能证明对应证据层，不能替代 MariaDB、真实 HTTP 或真实浏览器验收。

## 2.5 失败关闭

所有不确定情况按以下优先级处理：

```text
可确定满足继承条件        → PASS / INHERIT
普通无法证明或不符合条件   → INELIGIBLE / ordinary pending
解析不可唯一              → UNRESOLVED / ordinary pending
Git/DB/事务/流程完整性失败 → TECHNICAL_FAILURE / Scan 不发布
```

不允许“为了提高继承率”把 `UNRESOLVED` 当成 PASS。

---

# 3. 当前代码基线与改造地图

当前 `main@e46b82d...` 已有可复用能力：

## 3.1 直接保留/扩展

### DB / Repository

现有：

- `app/db/repositories/project_repository.py`
- `app/db/repositories/project_state_repository.py`
- `app/db/repositories/line_index_repository.py`
- `app/db/repositories/analysis_repository.py`
- `app/db/repositories/file_state_repository.py`
- `app/db/repositories/job_repository.py`
- `app/db/repositories/incremental_repository.py`
- `app/db/transaction.py`
- `app/db/manager.py`

策略：保留已有 Project/Scan/File/Line/DataVersion 基础；新增 Repository Master、Analysis Domain、Inheritance Domain、Lock/Checkpoint Repository。

### Service

现有：

- `app/services/project_service.py`
- `app/services/analysis_service.py`
- `app/services/progress_service.py`
- `app/services/incremental_service.py`
- `app/inject/service.py`
- `app/jobs/service.py`

策略：

- `ProjectService` 去除 CURRENT 切换职责；
- `AnalysisService` 迁到新 Analysis Domain，并禁止切 CURRENT；
- `ScanImportService` 重构为 Durable Import Coordinator；
- `ProgressService` 增加 inherited 子统计；
- `VNextBackgroundJobService` 扩展为可重建 callback 的 durable import job 基础。

### API / UI

现有：

- `app/api/application.py`
- `app/api/endpoints/analysis.py`
- `app/api/endpoints/code_detail.py`
- `app/api/endpoints/progress.py`
- `app/api/endpoints/jobs.py`
- `web/assets/js/coverage_enhance.js`
- `web/assets/js/coverage_progress.js`

策略：在 canonical VNext API 上增加 inheritance review，不新增第二套 legacy 业务 API。

### Upgrade / Release

现有：

- `scripts/upgrade/migration_runner.py`
- `scripts/upgrade/vnext_schema.sql`
- `scripts/upgrade/vnext_domain_constraints.sql`
- `scripts/upgrade/schema_preflight.py`
- `scripts/upgrade/run_upgrade.py`
- `scripts/upgrade/cutover_controller.py`
- `scripts/upgrade/run_rollback_rehearsal.py`
- `scripts/upgrade/build_deployment_manifest.py`
- `scripts/upgrade/evidence_manifest.py`

策略：扩展现有升级框架，禁止重新创建第二套发布工具链。

## 3.2 建议新增目录/模块

```text
app/
  inheritance/
    __init__.py
    engine.py
    reason_codes.py
    predecessor.py
    git_snapshot.py
    git_line_map.py
    normalizer.py
    cpp_parser.py
    dependency_resolver.py
    control_context.py
    preprocessor_context.py
    decision_writer.py

  services/
    repository_service.py
    scan_import_service.py          # 可替换/重构现有 app/inject/service.py 中 orchestration
    scan_publication_service.py
    inheritance_review_service.py
    import_recovery_service.py

  db/repositories/
    repository_repository.py
    analysis_record_repository.py
    analysis_block_repository.py
    analysis_line_link_repository.py
    inheritance_repository.py
    repository_lock_repository.py
    import_checkpoint_repository.py
```

如现有项目命名风格更适合合并某些小 Repository，可以合并文件，但**职责必须保持以上边界**。

## 3.3 测试目录建议

```text
tests/vnext/
  test_legacy_migration_contract.py
  test_analysis_domain.py
  test_repository_identity.py
  test_scan_publication.py
  test_import_recovery.py
  test_repository_lock.py

  inheritance/
    test_predecessor.py
    test_git_line_map.py
    test_normalizer.py
    test_function_identity.py
    test_control_context.py
    test_preprocessor_context.py
    test_dependencies.py
    test_inheritance_engine.py
    test_covered_bridge.py
    test_rejection_chain.py

  api/
    test_inheritance_api.py
    test_progress_inherited.py

  browser/
    inheritance_review.spec.js
```

不要求一定按该目录机械拆分；目标是让每个 Gate 的测试可单独选择，不依赖全量 suite。

---

# 4. Gate 状态与全局退出规则

## 4.0 Evidence Manifest v2（所有 Gate 强制）

每个 Gate 的每份可用于 PASS/READY 的证据必须登记到 `evidence-manifest-v2.json`，并尽量复用现有 `scripts/upgrade/evidence_manifest.py` / authenticity validator 作为 canonical 实现，避免第二套 evidence 工具链。最少字段：

```text
evidence_schema_version = 2
gate
evidence_id
evidence_class            # static/db-integration/runtime/http/browser/release
candidate_revision
release_identity
host_identity
database_runtime_identity（涉及 DB 时）
command_or_action
started_at / finished_at
exit_code
artifact_path
artifact_sha256
source_inputs_sha256[]
status
synthetic                  # true 只能做辅助证据，不能推进 production gate
```

规则：

- 目录里“有文件”不等于有真实证据；
- `synthetic=true`、placeholder、手工伪造 PASS label 不得推进 Gate；
- 同一 Gate 的 evidence 必须绑定同一 candidate revision；
- Evidence Manifest 本身也计算 SHA256 并进入下一个 Gate 的输入；
- `READY_WITH_ACCEPTED_RISK` 只允许有已记录且被批准的 P2/Info；任何 unresolved P0/P1 → `NOT_READY`。


Gate 只允许三个状态：

- `PASSED`
- `BLOCKED`
- `INCOMPLETE`

## 4.1 PASSED

必须满足：

- 本 Gate 设计代码均已真实运行到 canonical 路径；
- 修改点相关测试通过；
- 对应真实证据层已验证；
- 没有未接受 P0/P1；
- 输出 Gate Evidence Bundle。

## 4.2 BLOCKED

出现任一情况即 BLOCKED：

- 数据可能静默丢失/错误转换；
- 双重权威仍存在；
- CURRENT/Repository/Analysis identity 不唯一；
- Gate 依赖的前序契约未冻结；
- 技术失败被降级成普通 pending；
- 关键路径仍由 intended-to-retire legacy 业务实现提供；
- 测试显示核心行为错误；
- 生产级验证所需证据明确失败。

## 4.3 INCOMPLETE

没有证明失败，但缺必要证据，例如：

- 只通过 SQLite，尚未 MariaDB 5.5-compatible rehearsal；
- 只通过 API mock，尚未真实 HTTP；
- 只通过 DOM mock，尚未 Chromium；
- Schema 正确但还没有 production-sized migration rehearsal。

---

# 5. Gate A — Legacy Compatibility Contract

## 5.1 Gate A 目标

将“兼容旧库”从设计原则落实成**可执行、可自动验证的零损失迁移合同**。

完成后必须能够从当前运行版本的 Legacy Source DB，确定性迁移到一个全新的 VNext Target DB，并证明权威业务语义守恒。

Gate A **不做** AnalysisRecord/LineLink 业务切换，也不做继承算法；只把 Legacy → VNext 兼容底座彻底做实。

## 5.2 Gate A 输入

- 生产 Inventory 中真实 4 表结构；
- `migration_runner.py`；
- `vnext_schema.sql`；
- `schema_preflight.py`；
- `tests/vnext/test_migration_runner.py`；
- 当前 MariaDB 5.5.64 兼容边界。

## 5.3 Gate A 产物

1. `Legacy Migration Contract v2`；
2. 完整字段级 Migration Matrix；
3. 可独立运行的 Source → Empty Target migration；
4. semantic snapshot/hash v2；
5. anomaly ledger；
6. 生产规模 rehearsal 数据证据；
7. MariaDB 5.5 DDL preflight 证据；
8. Gate A evidence JSON/Markdown。

## 5.4 A1：固定 Legacy Schema Fixture

### 开发

基于真实 Inventory 固化四张 Legacy 表 fixture：

```text
coverage_analysis
coverage_line_index
coverage_project_state
coverage_background_jobs
```

Fixture 必须覆盖真实字段，不允许只保留 migration 当前已经使用的字段。

推荐新增：

```text
tests/fixtures/legacy_schema_mariadb55.sql
```

以及 Python fixture builder，供 SQLite/MariaDB 测试共用业务数据。

### 验证

- 字段名、NULL/default、主键/唯一键与真实 Inventory 对照；
- migration code 对未知/可选字段行为明确；
- 旧表新增非关键列不得导致 migration 崩溃。

### PASS

真实旧 Schema 可以被完整 capture；没有通过“删掉不认识的字段”来伪造兼容。

## 5.5 A2：严格 Source / Target Database Separation

### 开发

保留 `validate_migration_database_separation()`，但**配置字符串比较只作为第一层预检**。必须对 source/target 建立真实连接并生成 runtime fingerprint：

```text
configured host / port / database（脱敏）
DATABASE()
@@hostname
@@port
@@datadir
server_uuid（若该 MariaDB 版本可用；不可用不作为失败）
```

判定规则：

1. `DATABASE()` 相同且 server runtime fingerprint 相同 → 无条件拒绝；
2. `localhost` / `127.0.0.1` / DNS alias / 不同 DB user 不能绕过同库保护；
3. Target 必须是**新建 Empty VNext Target DB**，或是 `coverage_schema_meta` 明确表明同一 migration id 已完整完成的幂等目标；
4. Target 发现 Legacy-only 同名异构表时立即拒绝；
5. `database_runtime_identity` 写入 Gate A/F Evidence Manifest。

### 验证

- 完全相同 DB → reject；
- 同 DB 不同 user → reject；
- `localhost` vs `127.0.0.1` 同实例同 DB → reject；
- DNS alias 同实例同 DB → reject；
- 同实例不同 database → allow；
- Target 已有不兼容同名旧表 → reject；
- Target 为相同 migration id 的已完成目标 → idempotent no-op；
- fingerprint 查询失败 → Gate A `INCOMPLETE`，不得猜测允许迁移。

## 5.6 A3：VNext Schema Versioning

### 开发

将 `coverage_schema_meta` 从“只有 coverage_vnext=1”升级成可表达多阶段 schema：

```text
coverage_vnext_core        version 1
coverage_analysis_domain   version 0/1
coverage_inheritance       version 0/1
```

Gate A 只要求 `coverage_vnext_core=1`。

Schema metadata 至少记录：

```text
schema_key
schema_version
applied_at
release_sha
migration_id
```

另新增 append-only `coverage_schema_migrations` ledger，适配 MariaDB 5.5 DDL 非事务化事实：

```text
migration_id PK
schema_key
from_version
to_version
ddl_sha256
state = STARTED|APPLIED|FAILED
started_at
finished_at
release_sha
error_class
```

每个 DDL step 先落 `STARTED`，全部执行并验证后才写 `APPLIED`；失败写 `FAILED`，不得仅靠最终 schema_meta 猜执行完整性。

MariaDB 5.5 不依赖 JSON 字段。

### 验证

- 重复 apply 不重复建表；
- 不允许 schema_version 倒退；
- release SHA 记录当前 Candidate；
- 部分 DDL 失败不允许 schema_meta 被错误标记成功。

## 5.7 A4：Legacy `coverage_analysis` 字段迁移

### 字段契约

至少保留：

```text
project_name
file_path
file_path_hash
source_file_name
line_number
reviewer
status
is_draft
coverage_method
uncovered_reason
comment/remark（存在时）
created_at（存在时）
updated_at（存在时）
```

### 时间字段策略

Gate A VNext Core 仍使用 `coverage_analyses` 时：

- 新系统 `created_at/updated_at` 表示 target row 生命周期；
- Legacy 时间必须进入可追溯 provenance，不能静默丢弃；
- **Gate A 必须立即把 Legacy 时间事实持久化进 Target DB**，不得仅放外部 anomaly/JSON 等待 Gate B。新增 `coverage_legacy_provenance`（或等价 target table），至少保存 source_table/source_pk/legacy_created_at/legacy_updated_at/raw_status/raw_payload_hash/migration_id。Gate B 只消费该 provenance，不负责补救 Gate A 丢失。

推荐 MariaDB 5.5 schema：

```text
coverage_legacy_provenance
  id BIGINT PK AUTO_INCREMENT
  migration_id VARCHAR(128) NOT NULL
  target_entity_type VARCHAR(32) NOT NULL
  target_entity_id BIGINT NOT NULL
  source_table VARCHAR(64) NOT NULL
  source_identity VARCHAR(512) NOT NULL
  provenance_key_hash CHAR(64) NOT NULL
  legacy_created_at DATETIME(6) NULL
  legacy_updated_at DATETIME(6) NULL
  legacy_raw_status VARCHAR(64) NULL
  legacy_raw_is_draft TINYINT NULL
  raw_payload_sha256 CHAR(64) NOT NULL
  created_at DATETIME NOT NULL
  UNIQUE(provenance_key_hash)
  KEY(source_table(30), source_identity(159))
```

`provenance_key_hash` 是
`SHA256(migration_id, target_entity_type, target_entity_id, source_table)` 的
规范化身份指纹。它保留完整业务身份，同时避免 `utf8mb4` 在 MariaDB 5.5
的 767-byte 索引上限；`source_table/source_identity` 查询继续做完整值过滤，
前缀索引只负责候选定位。raw payload 本体不必复制敏感内容；
`raw_payload_sha256` 用于证明映射输入未漂移。

### 验证

- confirmed/draft 状态数量守恒；
- reviewer/coverage_method/reason/comment 内容守恒；
- analysis-only line 明确补建 unknown line context，并记录 anomaly；
- 不允许凭空把 draft 改成 confirmed。

## 5.8 A5：Legacy `coverage_line_index` 迁移

必须守恒：

```text
project
file_path
file_path_hash
line_number
line_text
block_start_line
block_end_line
block_type
function_name
function_hash
code_line_hash
code_occurrence
```

### 冲突策略

如果相同 legacy hash + line 对应多个 path：

- 不猜；
- 建立可区分 namespace；
- 记录 `path_conflict` anomaly；
- 迁移必须保持各自事实，不合并。

### 验证

- line count 守恒；
- physical key 不丢；
- hash/path conflict fixture；
- analysis-only line；
- 空 line_text；
- 超长路径/中文路径/反斜杠归一化。

## 5.9 A6：Legacy `coverage_project_state` 迁移

必须保留：

```text
project_name
data_version
```

Gate A 迁移为：

```text
coverage_projects
coverage_project_state
```

legacy 项目只创建一个 `legacy_migrated` Scan 作为迁移容器。

### 关键约束

- `current_scan_id` 指向该 legacy_migrated Scan；
- 该 Scan 不具有可信 Git identity；
- repository snapshot `verified=0`；
- provenance=`legacy_migration`；
- 不从当前配置或 HEAD 猜 branch/commit。

## 5.10 A7：Legacy Job 完整 Migration Matrix

即使当前生产 Job 行数为 0，也必须实现长期兼容。

建议映射：

| Legacy | VNext | 策略 |
|---|---|---|
| job_id | job_id | exact |
| project_name | project_id | resolve project |
| kind | kind | exact/compat map |
| state | state | terminal exact；active → interrupted |
| percent | progress | 仅在 source unit 已由 fixture/Inventory 证明为 0～100 时转换；同时保留 `legacy_raw_percent` 与 `legacy_percent_unit` |
| stage | input_payload/provenance | 保留 |
| message | input_payload/provenance | 保留 |
| result_path | legacy provenance | **默认不得直接成为 active downloadable path**；保存 raw legacy path，只有通过 configured allowed root + realpath revalidation 后才写 active result_path |
| filename | input_payload/provenance | 保留 |
| row_count | input_payload/provenance | 保留 |
| heartbeat_at | heartbeat_at/provenance | 保留 |
| finished_at | finished_at | 保留 |
| data_version | data_version | 保留 |
| error_message | error_message | 保留 |

旧 `queued/running/interrupted`：统一转换为 `interrupted`，并记录 anomaly，禁止自动在新进程继续未知 callback。

## 5.11 A8：Semantic Snapshot v2

升级 `capture_legacy_semantic_snapshot()` / `capture_vnext_semantic_snapshot()`。

必须纳入：

- project identity；
- data_version；
- physical line facts；
- analysis content/status/reviewer；
- legacy timestamp provenance；
- Job semantic identity；
- **migration anomaly 不进入 authoritative semantic hash**；anomaly 作为独立 ledger/evidence，避免诊断实现变化改变业务事实 hash。

不纳入：

- surrogate DB id；
- derived `coverage_file_state`；
- table row physical order；
- target `created_at` 这种迁移过程时间。

输出：

```text
source_semantic_hash
target_semantic_hash
authoritative_semantic_match
```

## 5.12 A9：生产规模 Migration Rehearsal

Gate A PASS 必须至少执行一次 **verified production backup 的恢复副本 → Empty VNext rehearsal target** 的真实迁移。Synthetic/构造数据只用于边界和压力补充，不能单独推进 Gate A。

规模至少覆盖当前生产 Inventory 对应量级：

- 约 5.1 万 Analysis；
- 约 9 万 Line；
- 2 project；
- Job 0 条与额外 synthetic non-zero Job fixture 两种情形。

验证：

- 不 OOM；
- 无逐行 commit；
- SQL 批次合理；
- semantic hash 一致；
- 重跑幂等；
- derived state 重建后统计守恒。

性能只记录基线，不在 Gate A 硬编码绝对秒数；但明显出现 N+1 或数量级退化时 Gate A 不应 PASS。

## 5.13 A10：Gate A 修改文件建议

主要修改：

```text
scripts/upgrade/migration_runner.py
scripts/upgrade/vnext_schema.sql
scripts/upgrade/vnext_domain_constraints.sql
scripts/upgrade/schema_preflight.py
scripts/upgrade/evidence_manifest.py
app/db/repositories/project_repository.py
app/db/repositories/project_state_repository.py
app/db/repositories/line_index_repository.py
app/db/repositories/analysis_repository.py
app/db/repositories/job_repository.py
tests/vnext/test_migration_runner.py
```

新增：

```text
tests/vnext/test_legacy_migration_contract.py
tests/fixtures/legacy_schema_mariadb55.sql
```

## 5.14 Gate A 定向测试命令原则

不规定开发机器唯一命令，但 Test Selection 必须只包含：

```text
tests/vnext/test_migration_runner.py
tests/vnext/test_legacy_migration_contract.py
与 schema_preflight/migration_runner 直接相关测试
```

另执行真实 disposable MariaDB 迁移 rehearsal。

## 5.15 Gate A Evidence Bundle

至少输出：

```text
gate-a/
  source_schema.txt
  target_schema.txt
  migration_matrix.json
  source_semantic.json
  target_semantic.json
  semantic_hashes.json
  anomalies.json
  migration_run_1.json
  migration_run_2_idempotency.json
  mariadb55_preflight.json
  targeted_tests.txt
  evidence-manifest-v2.json
  gate_a_result.json
```

## 5.16 Gate A PASS 条件

全部满足：

- Legacy Source → Empty VNext Target；
- Source/Target 同库保护通过；
- 权威 semantic hash 一致；
- timestamp/Job 无静默丢失且 provenance 持久化在 Target DB；
- 至少一次 verified production backup restore → Empty Target rehearsal PASS；
- Evidence Manifest v2 authenticity PASS；
- legacy Git identity unknown/unverified；
- migration 幂等；
- MariaDB 5.5-compatible；
- 无 destructive DDL；
- 无 P0/P1。

Gate A 输出给 Gate B 的冻结接口：**VNext Core Schema + Legacy Migration Contract v2**。

---

# 6. Gate B — Repository Master + Analysis Canonical Domain

## 6.1 Gate B 目标

在不破坏 Gate A Legacy 兼容的前提下，建立两个新的长期权威域：

1. `Repository Master Identity`；
2. `AnalysisRecord / AnalysisBlock / AnalysisLineLink`。

Gate B 完成后，业务代码应开始以新 Analysis Domain 为 canonical owner；旧 `coverage_analyses` 仅作为兼容迁移来源/短期 shim，不再拥有独立业务语义。

## 6.2 B1：新增 `coverage_repositories`

建议 Schema：

```text
id BIGINT PK
project_id BIGINT NOT NULL
repository_name VARCHAR(128) NOT NULL
canonical_remote VARCHAR(1024) NULL
last_observed_physical_path VARCHAR(1024) NOT NULL DEFAULT ''
physical_resource_id BIGINT NULL
lifecycle_state VARCHAR(16) NOT NULL DEFAULT 'ACTIVE'  # ACTIVE|RETIRED
created_at DATETIME NOT NULL
updated_at DATETIME NOT NULL
UNIQUE(project_id, repository_name)
```

### 身份原则

- `repository_name` 是项目内稳定逻辑名；
- **runtime config 是当前物理路径绑定权威**；DB 的 `last_observed_physical_path` 只记录观察值/审计事实，不反向覆盖 config；
- path 改变不得创建新 logical `repository_id`；
- rename 通过显式 rename transaction 修改 `repository_name` 并保留 id；alias 使用独立 alias mapping；retire 只改 lifecycle_state；
- remote 变化必须显式审核，不能自动改变 identity；
- 同一物理 Git clone 由 `coverage_repository_resources` 独立建模，并允许多个 logical repository 在运行时解析到同一个 physical resource。

Repository rename/alias 生命周期：

```text
coverage_repository_aliases
  project_id
  repository_id
  alias_name
  created_at
  retired_at NULL
  UNIQUE(project_id, alias_name)
```

- rename：更新 canonical `repository_name`，旧名进入 aliases；
- alias：只做输入兼容解析，不改变 repository id；
- retire：`lifecycle_state=RETIRED`，历史 Scan 继续可读；
- 自动发现 remote/path 变化不得自动执行 rename/merge。
- 旧 legacy_migrated Scan 可绑定 unknown/unverified repository snapshot，但不能伪造历史 commit。



### Physical Repository Resource

新增 `coverage_repository_resources`，只表达运行时实际 Git clone/common-dir 资源：

```text
id BIGINT PK
resource_key CHAR(64) UNIQUE NOT NULL
resolved_git_common_dir VARCHAR(1024) NOT NULL
resolved_worktree_root VARCHAR(1024) NOT NULL
fs_device BIGINT NULL
fs_inode BIGINT NULL
next_fencing_token BIGINT NOT NULL DEFAULT 0
observed_at DATETIME NOT NULL
```

`resource_key` 由受信任 runtime resolver 根据 `git rev-parse --git-common-dir` 的 realpath + 可用的 filesystem identity 生成；用户输入路径不能直接作为 resource_key。**Gate C 的互斥锁以 physical resource id 为粒度。**

## 6.3 B2：升级 `coverage_scan_repositories`

新增：

```text
repository_id BIGINT NULL/NOT NULL（过渡后）
commit_sha CHAR(40) NULL
identity_verified TINYINT NOT NULL DEFAULT 0
identity_provenance VARCHAR(128) NOT NULL DEFAULT ''
```

兼容保留 `old_commit_sha/new_commit_sha` 只服务 Incremental Report，不参与 Scan 自身身份。旧 `verified/provenance` 定义为“incremental range/source capture verification”；新 `identity_verified/identity_provenance` 专门表示 Scan repository identity，迁移期二者不可混读。Inheritance 只使用：

字段退休矩阵：

| 字段 | 新含义 | Inheritance 可否读取 |
|---|---|---:|
| `verified` | legacy/incremental capture verification | 否 |
| `provenance` | legacy/incremental capture provenance | 否 |
| `identity_verified` | Scan repository identity verified | 是 |
| `identity_provenance` | Scan identity evidence source | 是 |
| `old_commit_sha/new_commit_sha` | Incremental Report range | 否 |
| `commit_sha` | Scan 本身 commit identity | 是 |


```text
repository_id + branch_name + commit_sha
```



### `scan_identity_v2` / `scan_key`

Gate B 起重新冻结 Scan identity：

```text
scan_key = SHA256(canonical_json({
  project_name,
  info_sha256,
  review_scope,
  repositories: sorted([
    repository_name,
    branch_name,
    commit_sha
  ]),
  report_source_signature,
  identity_contract_version: 2
}))
```

禁止把 `project_id/repository_id` 这类跨 DB 可能变化的 surrogate、`repository_path`、当前 HEAD、旧 `old_commit_sha/new_commit_sha` 写入 scan_key。DB FK 仍使用 id，但 scan_key 的 canonical JSON 使用稳定 project/repository logical names + branch/commit。已有 Scan 保留 legacy scan_key，不原地重算；新 Scan 使用 `identity_contract_version=2`。

### Backfill

- 对新的 verified VNext Scan：由上传/导入时明确写入；
- 对 legacy_migrated：`commit_sha=NULL`、`identity_verified=0`；
- 不从 `old_commit_sha/new_commit_sha` 自动猜哪一个是 Scan 自身 identity，除非已有明确 current contract 能证明。

## 6.4 B3：新增 `coverage_analysis_records`

建议字段：

```text
id BIGINT PK
conclusion_status VARCHAR(64) NOT NULL DEFAULT ''  # 可覆盖/无法覆盖/冗余代码/空内容等“内容”
coverage_method TEXT NULL
uncovered_reason TEXT NULL
comment TEXT NULL
content_revision BIGINT NOT NULL DEFAULT 1
content_hash CHAR(64) NOT NULL
content_origin VARCHAR(32) NOT NULL  # MANUAL|LEGACY_MIGRATED 等，只描述内容产生来源
legacy_source_analysis_id BIGINT NULL
legacy_source_created_at DATETIME NULL
legacy_source_updated_at DATETIME NULL
legacy_raw_status VARCHAR(64) NULL
legacy_raw_is_draft TINYINT NULL
created_at DATETIME NOT NULL
updated_at DATETIME NOT NULL
```

### 内容更新规则

- `content_revision` 每次真实内容变化 +1；
- 不保存完整修改历史；
- `content_origin` 只描述内容最初产生来源，**禁止使用 `origin=inherited` 表示当前关系状态**；
- **AnalysisRecord 不保存 `is_draft`、confirmed/inherited/carry 状态**；
- reviewer/time 和所有 review state 都只放 LineLink；
- Legacy `status/is_draft` 必须一方面转换为 `conclusion_status + LineLink.review_state`，另一方面以 raw provenance 保留以证明零损失。

## 6.5 B4：新增 `coverage_analysis_blocks`

建议字段：

```text
id BIGINT PK
scan_id BIGINT NOT NULL
repository_id BIGINT NULL
file_id BIGINT NOT NULL
start_line INT NOT NULL
end_line INT NOT NULL
origin VARCHAR(32) NOT NULL
block_identity_verified TINYINT NOT NULL DEFAULT 1
originating_record_id BIGINT NULL
initial_content_hash CHAR(64) NULL
created_by VARCHAR(255) NOT NULL DEFAULT ''
created_at DATETIME NOT NULL
```

关键约束：

- Block = 用户保存时实际选择范围；
- 不允许 AST 自动扩缩；
- `originating_record_id` 仅为 provenance，不是当前权威；`initial_content_hash` 固化操作发生时的内容快照指纹，避免可变 Record 被误称为“initial”；
- Block 内未来可以出现多个 AnalysisRecord；
- `block_identity_verified=1` 只允许由新系统真实用户保存操作产生；Legacy backfill 没有可证明的用户选择范围时不得设置为 1；
- 自动继承不会创建假的 AnalysisBlock，而由 Gate D 的 InheritanceGroup 表达。

## 6.6 B5：新增 `coverage_analysis_line_links`

建议字段：

```text
id BIGINT PK
scan_id BIGINT NOT NULL
line_id BIGINT NOT NULL
analysis_record_id BIGINT NOT NULL
analysis_block_id BIGINT NULL
review_state VARCHAR(32) NOT NULL  # MANUAL_DRAFT|MANUAL_CONFIRMED|INHERITED_PENDING|CARRIED_COVERED
relation_origin VARCHAR(32) NOT NULL  # MANUAL|INHERITANCE|COVERAGE_CARRY
inheritance_group_id BIGINT NULL
is_active TINYINT NOT NULL DEFAULT 1
reviewed_by VARCHAR(255) NOT NULL DEFAULT ''
reviewed_at DATETIME NULL
source_scan_id BIGINT NULL
source_line_id BIGINT NULL
source_relation_id BIGINT NULL
relation_revision BIGINT NOT NULL DEFAULT 1
created_at DATETIME NOT NULL
updated_at DATETIME NOT NULL
```

### 唯一权威约束

每个 Scan 物理行最多一条 relation row（`UNIQUE(scan_id,line_id)`）；`is_active=1` 才参与当前业务状态。`review_state` 是 draft/confirmed/inherited/carry 的唯一当前状态权威；AnalysisRecord 不重复保存这些状态。Reject 时保留原 inherited relation row 但置 `is_active=0`，从而支持安全 Undo。

MariaDB 5.5 不使用 partial unique index。推荐实现方式：

- 第一阶段固定为**每物理行一条当前 relation row**；`is_active` 控制 Reject 暂时失活，人工重分析在同一 row 上原子改写为新的 MANUAL relation；
- 不保留通用 relation 历史，历史链由 source_*、rejection ledger、decision ledger 提供；
- Reject 后若发生 manual analysis，原 rejection 进入 terminal/superseded 状态，旧 inherited provenance 仍保存在 rejection row，不能被自动恢复。

唯一键直接：

```text
UNIQUE(scan_id, line_id)
```

## 6.7 B6：Legacy/VNext Analysis Backfill

当前 VNext：

```text
coverage_analyses.line_id UNIQUE
```

Backfill 算法：

对每个现有 `coverage_analyses`：

1. 新建一个 AnalysisRecord；
2. 新建一条 AnalysisLineLink；
3. legacy/VNext 旧记录如果没有可证明的“用户一次选择范围”，不猜 Block；
4. 推荐**不创建 AnalysisBlock**；若为 UI 兼容必须创建 synthetic container，则 `block_identity_verified=0`，且 Gate D 必须拒绝把它作为自动 Block 继承源；
5. 相同内容的多行 **不自动 dedupe**。

这样零损失且不伪造历史共享关系。

## 6.8 B7：人工保存新模型

重构 `AnalysisService.save()`：

### 新流程

```text
resolve project + scan + physical lines
↓
validate selected exact line range
↓
create/update AnalysisRecord
↓
create AnalysisBlock for this user operation
↓
bind selected physical lines through AnalysisLineLink
↓
advance data_version
↓
commit
```

### 必须删除的行为

`AnalysisService.save()` **不得再调用 `set_current_scan()`**。

### Shared Record partial edit

如果用户修改共享 Record 的部分行：

1. 读取该 Record 当前所有 active line links；
2. 如果本次 selected lines == 全部当前引用行，可更新原 Record；
3. 否则 clone Record → R2；
4. 只把本次选中行改绑 R2；
5. `relation_revision` 和 `content_revision` 相应推进；
6. 所有 split/update 采用 optimistic CAS（`UPDATE ... WHERE content_revision=?` / `relation_revision=?`）或 `SELECT ... FOR UPDATE`；影响行数不符立即 `STALE_*_REVISION`；
7. Record CAS、selected LineLink CAS、data_version advance 在同一事务提交。

## 6.9 B8：Reviewer / Review Time

Reviewer/time 是**物理行当前复核关系属性**：

```text
AnalysisLineLink.reviewed_by
AnalysisLineLink.reviewed_at
```

原因：共享一个 AnalysisRecord 的不同物理行可能由不同用户在不同时间确认。

旧 `coverage_analyses.reviewer` migration：迁入对应 LineLink 的 `reviewed_by`；旧更新时间可按 provenance 保存，不伪装成新确认时间。

## 6.10 B9：旧 `coverage_analyses` 兼容策略

阶段 B 推荐：

- 表暂时保留；
- 新 canonical read/write 切到 Analysis Domain；
- Legacy compatibility 只允许由 adapter 从新模型投影旧 DTO；
- 禁止双写两套独立 truth；
- 如果旧前端/兼容路径仍需旧表，先改为 view/adapter 或只读兼容，再最终退休。

不建议在 Gate B 立即 DROP 表。

## 6.11 B10：Repository/Analysis Repository 类

新增：

```text
RepositoryRepository
AnalysisRecordRepository
AnalysisBlockRepository
AnalysisLineLinkRepository
```

要求：

- Repository 层只做事实持久化，不塞业务规则；
- split/shared record 规则放 AnalysisService/Domain Service；
- 所有 bulk read/write 提供 batch API，避免 N+1；
- 使用 MariaDB 5.5-compatible SQL。

## 6.12 B11：Canonical Read Switch

需要修改：

```text
app/services/analysis_service.py
app/services/progress_service.py
app/code_detail/*
app/api/application.py
```

Code Detail compatibility DTO 中的 `is_draft` **只能由 LineLink.review_state 派生**，不是数据库第二权威。DTO 字段：

```text
analysis_state
is_draft  # derived only
reviewer
coverage_method
uncovered_reason
```

全部由 AnalysisLineLink + AnalysisRecord join 得到。

## 6.13 B12：Gate B 数据一致性校验

至少检查：

```text
old analysis row count == migrated line link count
每个 active line link 都有 record
每个 record 至少有一个 link，除允许的临时 orphan cleanup 情形
scan_id 与 line.file.scan 一致
block.file/scan 与 link.line 一致
confirmed/draft/analysis content 语义 parity
data_version 不倒退
```

## 6.13.1 Gate B 精确 DDL Freeze

进入编码前必须把所有新表固化为 MariaDB 5.5 可执行 DDL，并明确：

- PK/FK；
- UNIQUE/普通索引；
- 字段 NULL/default；
- `ON DELETE` 策略（权威历史事实默认 RESTRICT，不允许 CASCADE 静默删除分析链）；
- 冗余 `scan_id/repository_id/file_id/line_id` 字段的一致性校验；
- bulk query 所需联合索引；
- schema migration id/checksum。
- MariaDB 5.5 `utf8mb4` 767-byte 索引上限；无法完整放入联合索引的业务身份
  必须使用规范化 SHA-256 identity key，前缀索引只能用于候选定位，不能承担
  业务唯一性。

应用写入必须验证：`link.scan_id == line.file.scan_id`、`block.scan/file/repository` 与 line 一致。冗余字段只做查询/证据加速，不可形成第二权威。

## 6.14 Gate B 定向测试

- 一旧 Analysis → 一 Record + Link；
- 相同内容两行不自动合并；
- 精确 Block 范围；
- Block 不拥有当前 Record authority；
- shared Record partial split；
- 全部引用行共同编辑可更新原 Record；
- 一行只允许一 current link；
- reviewer/time per line；
- ordinary pending 不创建空 Link；
- Analysis save 不切 CURRENT；
- Code Detail read parity；
- Progress parity；
- migration/backfill 幂等；
- legacy compatibility adapter read parity。

## 6.15 Gate B Evidence Bundle

```text
gate-b/
  schema_diff.sql
  repository_identity_matrix.json
  analysis_backfill_before.json
  analysis_backfill_after.json
  analysis_semantic_hashes.json
  orphan_checks.json
  canonical_read_write_audit.json
  targeted_tests.txt
  evidence-manifest-v2.json
  gate_b_result.json
```

## 6.16 Gate B PASS 条件

- Repository Master 稳定；
- ScanRepositorySnapshot 具备 `repository_id + branch + commit` contract；
- AnalysisRecord(content)/LineLink(review state)/AnalysisBlock(human range) 单一权威成立；Gate D 的 InheritanceGroup contract 已预留 FK 但不作为 Gate B 已实现能力；
- 旧 Analysis 无损 backfill；
- 人工保存 canonical 使用新模型；
- Analysis save 不再影响 CURRENT；
- 没有双写两套独立 truth；
- 无 orphan/跨 Scan relation；
- Gate A migration 仍能完整执行。

Gate B 输出给 Gate C/D：**Repository Identity Contract + Analysis Domain Contract**。

---

# 7. Gate C — Scan Lifecycle / Durable Import / Repository Lock / Atomic CURRENT

## 7.1 Gate C 目标

把当前同步“create + ingest + set current + seal”的 Scan Import，升级为：

```text
Repository Lock
→ Staged Scan
→ Durable Import Job
→ Checkpoint
→ Coverage Import
→ Git/Inheritance/Stats/Consistency
→ Short Atomic Publication
```

Gate C 先完成生命周期和恢复框架，即使 Gate D 的 inheritance engine 仍是 stub/no-op，也必须能够安全导入并原子发布。

## 7.2 C1：CURRENT 唯一写入口

新增：

```text
ScanPublicationService
```

只有它可以：

```text
UPDATE coverage_project_state.current_scan_id
```

必须移除以下隐式 CURRENT 切换：

- `ProjectService.create_scan()`；
- `ProjectService.create_scan_and_ingest()`；
- `ProjectService.ingest_files()`；
- `AnalysisService.save()`；
- 其他 compatibility helper。

### Guard

可以在 ProjectStateRepository 中：

- 将 `set_current_scan()` 变为 private/internal；
- 或要求明确 `publication_token`/专用方法；
- 静态审计确保只有 PublicationService 调用。

## 7.3 C2：Scan Lifecycle

建议生命周期：

```text
IMPORTING
VALIDATING
SEALED
ABORTED
FAILED
ROLLED_BACK
```

`SEALED` 表示物理 facts 不可再修改，但**不代表 CURRENT**。`ABORTED` 表示从未成为 CURRENT 的候选在 seal/publish 前后被终止；`ROLLED_BACK` 只用于“曾经成为 CURRENT，后来明确切回前一正式版本”的 Scan。

CURRENT 唯一来自 project state pointer。

### 旧状态迁移矩阵

现有 VNext `building/importing/constructing` 在 schema migration 中映射为 `IMPORTING`；现有已 seal 成功 Scan 映射为 `SEALED`；无法证明完成度的异常构造态保持 `ABORTED/UNKNOWN_MIGRATION` provenance，禁止自动变成 CURRENT。状态字符串大小写在 DB 内统一为大写，API 可做兼容映射。

### Predecessor 固化

新增 `coverage_scans.predecessor_scan_id`。Candidate 创建事务读取当时 `coverage_project_state.current_scan_id`，同时写入 `predecessor_scan_id` 与 checkpoint 的 `expected_current_scan_id`。之后无论旧 CURRENT 是否继续被编辑，**候选继承只允许参考这个 predecessor Scan；绝不重新选择历史 Scan。**

### 允许写入

只有：

```text
IMPORTING / VALIDATING
```

期间允许构造物理 facts。

`SEALED` 后：

- repository snapshot immutable；
- files/lines immutable；
- report identity immutable；
- Analysis 仍可作为独立业务事实更新。

## 7.4 C3：Physical Repository Resource Lock + Fencing

新增：

```text
coverage_repository_resource_locks
  physical_resource_id BIGINT PK
  job_id VARCHAR(64) NOT NULL
  owner_token VARCHAR(128) NOT NULL
  fencing_token BIGINT NOT NULL
  heartbeat_at DATETIME NOT NULL
  acquired_at DATETIME NOT NULL
  expires_at DATETIME NULL
```

锁粒度是 Gate B 的 `coverage_repository_resources.id`，不是 logical `repository_id`。

### 永久单调 fencing token

`coverage_repository_resources.next_fencing_token` 永不回退、删除 lock row 也不得重置。acquire/takeover 在 DB 原子事务中：

```text
UPDATE coverage_repository_resources
SET next_fencing_token = next_fencing_token + 1
WHERE id = ?;

SELECT next_fencing_token ... FOR UPDATE;
UPSERT lock(..., fencing_token=next_fencing_token, ...);
```

实现可使用 MariaDB 5.5 支持的行锁/事务方式，不依赖较新数据库特性。

### 获取规则

1. 请求先从受信任 config + Git common-dir resolver 得到所有 physical resource ids；
2. 按 physical_resource_id 升序获取全部锁；
3. 任一个失败 → 同事务/补偿释放已获取锁；
4. **全部锁成功后才允许创建 business Scan/coverage facts**；
5. busy → `409 REPOSITORY_BUSY`，无 candidate Scan residue；
6. 不排队、不抢占正在健康运行的任务；
7. stale takeover 必须产生更大的 fencing token。

### 原子 fence 规则

Checkpoint、candidate facts batch、decision batch、publication 等每一次可改变业务状态的写，都必须在**同一事务内**验证：

```text
lock.job_id == current_job
lock.owner_token == current_owner
lock.fencing_token == expected_token
lock.expires_at/heartbeat 仍有效
```

禁止“先 SELECT token，退出事务，再写业务表”。0 行 conditional update/验证失败即 `LOCK_FENCING_FAILED`，旧 worker 立即停止。

## 7.5 C4：Durable Import Job + Immutable Input Artifact

扩展 `coverage_background_jobs`：

建议 `kind=scan_import`。

接受请求后先把 `.info` 复制到 Candidate 受控 staging root，使用临时文件写入、`fsync`、原子 rename，再计算/验证 SHA256。`input_payload` **只引用 staged artifact id/path + SHA256**；用户原始路径仅作 provenance，恢复时不得重新读取它。

新增可选 `coverage_import_artifacts`：

```text
artifact_id PK
job_id
kind=LCOV_INFO
staged_path
sha256
size_bytes
created_at
immutable=1
```

`input_payload` 至少持久化：

```text
project_id
requested_by
info_path identity / info_sha256
repository_ids
branch/commit identities
report identity
review_scope
algorithm_version
```

不能只保存 Python callback；重启后必须能根据持久化 payload 重建执行步骤。

### Job machine transition matrix

合法状态固定为：

```text
QUEUED -> RUNNING -> COMPLETED
                 -> FAILED
                 -> INTERRUPTED
QUEUED -> CANCELLED（仅未开始且产品允许时；scan_import same-repo busy 不进入 queue）
INTERRUPTED -> RUNNING（仅专用 RecoveryService 完成 reclaim + fence 后）
```

其它转移一律拒绝并记录 `INVALID_JOB_TRANSITION`。

## 7.6 C5：Checkpoint 表

新增：

```text
coverage_import_checkpoints
```

建议字段：

```text
job_id VARCHAR(64)
scan_id BIGINT
phase VARCHAR(64)
phase_version INT
checkpoint_seq BIGINT
payload LONGTEXT
input_sha256 CHAR(64)
fencing_token BIGINT
expected_current_scan_id BIGINT NULL
created_at DATETIME
updated_at DATETIME
PRIMARY KEY(job_id)
```

Checkpoint 更新必须 CAS：`WHERE job_id=? AND checkpoint_seq=? AND fencing_token=?`，成功后 `checkpoint_seq+1`。phase 只能按 machine-defined DAG 前进；恢复可以重放当前 phase 的幂等步骤，但不得把 phase/seq 倒退。影响 0 行即 stale worker。

建议 phase：

```text
LOCKED
SCAN_CREATED
INFO_STAGED
COVERAGE_IMPORTED
GIT_VERIFIED
SOURCE_PREPARED
LINE_MAP_BUILT
INHERITANCE_COMPUTED
STATS_REBUILT
CONSISTENCY_VERIFIED
SEALED
PUBLISHED
DONE
```

Gate C 可在 Gate D 前让 inheritance 阶段返回 `NO_ENGINE/NO_PREDECESSOR` 的 deterministic no-op，但 checkpoint 结构先固定。

## 7.7 C6：Import Coordinator

建议重构/新增：

```text
ScanImportService / ScanImportCoordinator
```

核心职责与固定创建顺序：

```text
0. validate request / auth / paths
1. stage immutable .info artifact + SHA256
2. resolve logical repos -> physical resource ids
3. acquire all physical resource locks
   - busy: release acquired locks, return 409, NO Scan row
4. one DB transaction: create durable scan_import Job + Candidate Scan + predecessor_scan_id + initial checkpoint
5. enqueue job handler
   - enqueue fail: mark Job FAILED, Candidate ABORTED, cleanup staged business facts as policy, release locks
6. execute idempotent phases
7. persist checkpoint by CAS
8. resume/retry through versioned recovery handler
9. invoke inheritance engine
10. rebuild derived state / consistency
11. seal
12. short atomic publish
13. cleanup worktrees/artifacts according retention policy
14. release locks
```

新增 versioned registry：

```text
SCAN_IMPORT_HANDLER_V1 -> ScanImportRecoveryHandlerV1
```

持久 Job 保存 `handler_version`；恢复时找不到精确 handler 版本必须 `INCOMPLETE/FAILED`，不能调用“最接近”的新 callback。

它不自己实现 Git diff、C++ parser、Analysis business rule。

## 7.8 C7：工作目录与源码准备

Gate C 先建立 worktree provider contract：

```text
GitSnapshotProvider
```

要求：

- main working tree HEAD 永远不切；
- historical commit 使用 detached temp worktree；
- worktree 路径绑定 job/repository/commit；
- cleanup 可重复；
- 服务重启时可重验已有 worktree；
- 本地缺 commit 时允许 fetch；
- fetch 后仍缺必需 commit → technical failure。

## 7.9 C8：Read-set / Publish Consistency

Import 期间旧 CURRENT 继续服务，也允许人工分析。

Inheritance/Import 任务只记录**真正读取并影响 Candidate 的旧事实**：

```text
source_analysis_record_id + content_revision
source_line_link_id + relation_revision
active rejection identity/version
expected_current_scan_id
```

Publish 前重新检查：

- expected CURRENT 未变化；
- read-set revision 未变化；
- unrelated Analysis 修改不影响发布。

这实现 R58/R59。

## 7.10 C9：Atomic Publication

最终只使用短事务：

```text
BEGIN
  revalidate candidate SEALED
  revalidate expected CURRENT
  revalidate read-set
  revalidate physical-resource lock fencing IN SAME TRANSACTION
  revalidate candidate predecessor_scan_id == expected_current_scan_id
  UPDATE project_state.current_scan_id = candidate
  advance/mark derived version as required
COMMIT
```

失败：

- Candidate 不成为 CURRENT；
- 旧 CURRENT 不动；
- 从未成为 CURRENT 的 Candidate 标记 `ABORTED`（业务前置/一致性失效）或 `FAILED`（技术失败）；不得使用 `ROLLED_BACK`；
- 不执行“半发布”。

## 7.11 C10：Recovery Worker

新增：

```text
ImportRecoveryWorker / ImportRecoveryService
```

启动时 recovery ownership 固定：

1. bootstrap **先注册 ScanImportRecoveryService**，并从 generic stale-job reaper 排除 `kind=scan_import`；
2. 专用 service 查找 `scan_import` 的 queued/running/interrupted；
3. 不直接把 stale scan_import 宣告完成/失败；
4. 根据 handler_version + checkpoint 重建执行上下文；
5. 重新获取/认领 physical resource locks 并获得更大 fencing token；
6. 验证所有恢复前置条件；
7. 从最近安全 phase 幂等重放/继续；
8. 不安全则 fail closed；
9. generic Job recovery 仅在 ScanImportRecoveryService 完成 ownership claim 后处理其它 kind。

### Resume 必须验证

```text
job_id
scan_id/project_id
schema versions
algorithm_version
.info SHA256
repository_id
branch/commit
worktree commit
lock fencing
expected CURRENT
read-set revisions
staging facts/checksum
```

## 7.12 C11：技术失败 vs 普通失败

技术失败示例：

- required commit remote 也取不到；
- source/worktree 无法读取；
- DB transaction error；
- checkpoint corruption；
- lock fencing violation；
- read-set/CURRENT publish precondition 改变；
- inheritance engine exception；
- consistency conservation failure。

这些都不能把 Candidate 发布为 CURRENT。技术失败的持久诊断唯一写入 Job + `coverage_import_failures`，不写成 per-line ordinary decision：

```text
coverage_import_failures
  id BIGINT PK AUTO_INCREMENT
  job_id VARCHAR(64) NOT NULL
  scan_id BIGINT NULL
  phase VARCHAR(64) NOT NULL
  error_class VARCHAR(64) NOT NULL
  error_fingerprint CHAR(64) NOT NULL
  failure_key_hash CHAR(64) NOT NULL
  message_redacted TEXT NULL
  fencing_token BIGINT NULL
  occurred_at DATETIME NOT NULL
  UNIQUE(failure_key_hash)
  KEY(job_id(61), phase(64), error_fingerprint(64))
```

普通“不继承”不属于 Gate C failure。

## 7.13 Gate C 定向测试

- create staged Scan 不切 CURRENT；
- Analysis save 不切 CURRENT；
- same repo busy 时无 Scan residue；
- multi-repo lock 固定顺序无死锁；
- different repo 可并行；
- stale worker fencing 拒写；
- immutable staged `.info` 原路径被删除后仍可恢复；
- handler_version 不匹配 → fail closed；
- enqueue 失败 → Job terminal + Candidate ABORTED + locks released；
- checkpoint CAS stale seq/token 拒写；
- checkpoint phase 幂等；
- process restart resume；
- local missing commit fetch success；
- remote missing commit technical failure；
- worktree 不改变 main HEAD；
- old CURRENT 导入期间可读/可分析；
- unrelated old analysis change → publish allowed；
- read-set change → publish blocked；
- successful atomic publish；
- failure leaves old CURRENT；
- repeated recovery 不重复写 facts。

## 7.14 Gate C Evidence Bundle

```text
gate-c/
  scan_state_machine.json
  repository_lock_tests.json
  fencing_tests.json
  checkpoint_resume_tests.json
  current_pointer_audit.json
  worktree_head_before_after.txt
  atomic_publish_tests.json
  runtime_job_audit.json
  targeted_tests.txt
  evidence-manifest-v2.json
  gate_c_result.json
```

## 7.15 Gate C PASS 条件

- CURRENT 唯一写入口；
- staged Scan 不提前 CURRENT；
- Physical Repository Resource Lock 获取顺序与 busy zero-residue 正确；
- durable job 可从 immutable staged payload + handler_version + checkpoint CAS 恢复；
- stale worker 的 fencing write 被 DB 原子拒绝；
- worktree 不污染主 repo；
- publish 原子；
- technical failure 不切 CURRENT；
- read-set consistency 正确；
- 无 P0/P1。

Gate C 输出给 Gate D：**稳定的 Candidate Scan/Repo/Checkpoint/Publish 执行框架**。

---

# 8. Gate D — Deterministic Inheritance Engine

## 8.1 Gate D 目标

实现 `Deterministic Inheritance Contract v1` 的 R01～R83，建立一套：

- 不依赖相似度；
- 不依赖 AI 猜测；
- 不回溯历史；
- 可解释；
- 可复算；
- 可 partial inherit；
- fail-closed；

的 C/C++ Analysis Inheritance Engine。

## 8.2 Gate D 模块边界

推荐：

```text
InheritanceEngine
  ├─ PredecessorResolver
  ├─ GitSnapshotProvider
  ├─ GitLineMapEngine
  ├─ CppSourceAnalyzer
  ├─ FunctionIdentityResolver
  ├─ ControlContextResolver
  ├─ PreprocessorContextResolver
  ├─ DependencyResolver
  │    ├─ MacroConstantResolver
  │    └─ DirectCalleeResolver
  ├─ Normalizer
  ├─ RejectionPolicy
  └─ DecisionWriter
```

`app/incremental/git_diff.py` 继续服务“新增代码/LCOV/blame”；可以复用底层 Git subprocess helper，但不把 inheritance 逻辑塞回 `added_lines()`。

## 8.3 D1：Predecessor Resolver

实现 R02/R03/R31/R32/R33，但 **Resolver 不再搜索历史 Scan**：

1. Candidate 必须已经在 Gate C 固化 `predecessor_scan_id = candidate 创建时的 expected CURRENT`；
2. 若 `predecessor_scan_id IS NULL` → 全项目正常 `NO_PREDECESSOR`；
3. 对 Candidate 中每个 `repository_id`，只在该 predecessor Scan 的 repository snapshot 中查同 logical repository；
4. snapshot 不存在、branch mismatch、non-ancestor 都只令该 repo ordinary no-inheritance；
5. 其它 repo 可以继续判定；
6. **禁止 per-repo 查询“最近 same-branch Scan”或任何更老历史记录。**

所有结果记录 reason code，并把 `predecessor_scan_id` 写入 decision run identity。

## 8.4 D2：Git Ancestry / Commit Availability

实现：

```text
old == new
OR
git merge-base --is-ancestor old new
```

commit availability：

```text
local cat-file
→ missing: git fetch
→ still missing: TECHNICAL_FAILURE
```

所有 Git 命令必须：

- 参数数组调用；
- 禁止 shell 拼接；
- repo path 经过允许根校验；
- stderr 限量记录；
- timeout/cancellation 与 job 绑定。

## 8.5 D3：Git Line Map Engine

### 主证据

使用固定命令参数的 unified diff 建立 old→new physical mapping，例如 `git diff --no-ext-diff --no-renames --unified=0 <old> <new> -- <exact-path>`；禁止 Git rename detection 或外部 diff driver 影响结果。

要求：

- unchanged/context lines deterministic mapping；
- old delete/new add 默认断链；
- 1:1 才允许；
- old line 全局最多一个 new line；
- new line 不能接受多个 old line；
- rename/move 文件不继承。

### Same-hunk recovery

只在同一 hunk：

- raw mapping 因 whitespace/indent/comment 丢失；
- normalized token exact；
- 唯一 1:1；

才恢复。

禁止：

- 跨 hunk；
- 全文件搜索；
- similarity；
- rename inference。

## 8.6 D4：Lexical Normalizer

第一阶段规范化只忽略：

```text
space/tab/indent
trailing whitespace
comments
```

保留全部真实 Token：

```text
identifier
literal
operator
keyword
punctuation
call target/args
```

不能使用危险的简单 regex 删除注释。

必须正确处理：

- 字符串中的 `//`、`/* */`；
- char literal；
- escaped quotes；
- multi-line comments；
- raw/string variants；
- C/C++ translation phase 中的 `\` + newline line splicing；
- CRLF/LF normalization 不改变 token identity；
- comment 跨 physical line 时仍保持 physical-line mapping 证据，不用 regex 直接删文本。

## 8.7 D5：C/C++ Parser Strategy

第一阶段扩展名：

```text
.c .cc .cpp .cxx .h .hh .hpp .hxx
```

解析目标：

- 函数范围；
- namespace/class scope；
- parameter signature；
- control-flow ancestors；
- preprocessor ancestors；
- macro/constant references；
- direct same-repo call resolution。

### 运行环境约束

当前生产需要兼容既有 Python/OS/MariaDB 环境，因此 parser 方案必须先做 dependency preflight。

推荐抽象：

```text
CppParserAdapter
```

允许后端实现：

- 系统 clang CLI/helper process；或
- 已验证兼容生产 Python 的 parser binding；

业务代码不得绑定某个未经生产验证的 Python 新包。

### Parser 不确定

源码可读但结构无法可靠解析：

```text
UNRESOLVED → ordinary pending
```

Parser infrastructure crash / source required but unreadable：

```text
TECHNICAL_FAILURE
```

## 8.8 D6：完整 Function Identity

Canonical Function Identity v1 固定为 token-level tuple：

```text
repository-relative path
namespace/class scope
function/operator name
template parameter list（如存在且可唯一解析）
parameter type/name-significant token signature
cv qualifiers
ref qualifiers
noexcept specification
trailing return type（如存在）
```

不把 source line number 当 identity；默认参数表达式是否纳入必须由 parser adapter 统一规范，并在 corpus 中固定。

函数 identity 任何必需部分无法唯一解析：不继承。

不以只有 `function_name` 作为充分条件。

## 8.9 D7：Analysis Block Mapping + InheritanceGroup

### Source AnalysisBlock eligibility

Block = 用户历史保存范围。只有 `block_identity_verified=1` 的真实人工 Block 才能参与自动 Block inheritance。Legacy/VNext backfill 无法证明用户原始选择范围时，该 relation 可查看/人工继续编辑，但**不能自动成为继承源**。

继承条件：

- source Block 的 candidate mapping 必须能证明 1:1；
- N:1 merge 不继承；
- 1:N fork 不继承；
- 允许合法 1:1 Block 内 partial physical-line inherit；
- 同 Block 不同 line 可指向不同 AnalysisRecord；
- Block body 其他无关代码变化不做 whole-block veto。

### 自动结果不伪造 AnalysisBlock

新增：

```text
coverage_inheritance_groups
  id BIGINT PK
  decision_run_id CHAR(64) NOT NULL
  candidate_scan_id BIGINT NOT NULL
  source_scan_id BIGINT NOT NULL
  source_analysis_block_id BIGINT NOT NULL
  repository_id BIGINT NOT NULL
  candidate_file_id BIGINT NOT NULL
  mapping_fingerprint CHAR(64) NOT NULL
  created_at DATETIME NOT NULL
  UNIQUE(decision_run_id, source_analysis_block_id, candidate_file_id, mapping_fingerprint)
```

`AnalysisLineLink.inheritance_group_id` 表达 group membership。group 可以是非连续 physical lines；因此不要只保存 `start_line/end_line` 作为成员集合。`AnalysisBlock` 永远只表示人工操作事实。

## 8.10 D8：Control-flow Context

对每条 candidate physical line：

提取从 line 到 function body 的完整 ancestor chain：

```text
if/else
switch/case/default
for
while/do
catch（如支持）
```

每个 ancestor 条件表达式进行 normalized token exact compare。

不做逻辑等价：

```text
A && B != B && A
x == y != y == x
```

对 candidate 不在祖先链上的其他分支变化，不阻断。

## 8.11 D9：Preprocessor Context

完整记录 Git-visible：

```text
#if
#ifdef
#ifndef
#elif
#else
branch relationship
#endif nesting
```

候选行完整 preprocessor ancestor chain 必须 normalized exact。

外部 build-time `-D` 不纳入第一阶段证明。

## 8.12 D10：Macro / Compile-time Constant

只检查候选 line 实际依赖。Dependency source universe 是**同 repository、对应 commit 可读取的全部相关 source/header**，不限于 `.info` 覆盖文件；`.info` 只决定 candidate coverage lines。

只检查候选 line 实际依赖：

- line 自身；
- control ancestor expressions；
- preprocessor ancestor expressions。

支持：

```text
#define object-like
#define function-like
constexpr
可唯一解析的 compile-time const
```

definition normalized exact 才 PASS。

以下变化阻断：

- value；
- expression；
- params；
- object/function-like 类型；
- resolved definition；
- const/constexpr value。

无法唯一 resolve → ordinary pending。

## 8.13 D11：Direct Same-repo Callee

只检查一层直接调用，不递归。

Direct callee resolver 可以加载同 repository commit 中未出现在 `.info` 的源/头文件；跨 repo 仍不追踪。

每个实际依赖 direct callee：

1. 必须唯一 resolve；
2. 必须属于同 repository；
3. old/new 完整函数体 normalized exact；
4. 任一真实 Token 变化阻断 caller candidate。

以下默认 UNRESOLVED：

- function pointer；
- virtual dispatch；
- complex template；
- macro-wrapped call；
- ambiguous overload；
- 无唯一 symbol。

跨 repo call 不追踪。

## 8.14 D12：Covered Bridge / Delete Chain

### Covered Bridge

```text
V4 uncovered + R
V5 covered（代码身份持续）
V6 uncovered
```

V5 保存静默关系：

```text
CARRIED_COVERED
```

V6 可以恢复 R，但状态是：

```text
INHERITED_PENDING
```

### Delete/Re-add

```text
V4 line exists + R
V5 line deleted
V6 same text re-add
```

永久断链，V6 ordinary pending。

## 8.15 D13：Reject / Undo Policy

`coverage_inheritance_rejections` 是拒绝事实唯一权威。建议表：

```text
id BIGINT PK
scan_id BIGINT NOT NULL
line_id BIGINT NOT NULL
rejected_relation_id BIGINT NOT NULL
rejected_relation_revision BIGINT NOT NULL
rejected_analysis_record_id BIGINT NOT NULL
rejected_source_scan_id BIGINT NULL
rejected_source_line_id BIGINT NULL
rejected_source_relation_id BIGINT NULL
rejection_revision BIGINT NOT NULL DEFAULT 1
is_active TINYINT NOT NULL DEFAULT 1
terminal_reason VARCHAR(32) NULL   # UNDONE|MANUAL_REANALYSIS 等
rejected_by VARCHAR(255) NOT NULL
rejected_at DATETIME NOT NULL
resolved_at DATETIME NULL
```

Reject 单事务：

1. 要求目标 Scan 是 CURRENT；
2. CAS 校验 `LineLink.relation_revision`；
3. 写 rejection snapshot；
4. 将原 inherited LineLink `is_active=0`，保留 `review_state=INHERITED_PENDING` 与 source provenance；
5. advance data_version；
6. 行变为 ordinary pending；
7. 后续 Scan 不得回溯更老结论绕过该 rejection。

Undo 单事务：

- 仅当前 Scan；
- `rejection.is_active=1`；
- LineLink 仍是同一 rejected relation/revision lineage，且尚未重新人工分析；
- CAS 校验 `expected_rejection_revision + expected_relation_revision`；
- 显式将 LineLink `is_active=1`，恢复 `INHERITED_PENDING`；
- rejection 标记 `is_active=0, terminal_reason=UNDONE`，记录 resolved_at；
- 系统不得自动 undo；
- 已经计算完成的后续 Scan 不追溯重写。

如果拒绝后保存新的人工分析：同一 LineLink row 被原子改写为 `MANUAL_DRAFT/MANUAL_CONFIRMED`、revision 增加；对应 rejection 标记 `terminal_reason=MANUAL_REANALYSIS`。此后 Undo 永久禁止，但 rejection 事实仍保留用于证明旧链已断。

## 8.16 D14：继承状态

LineLink 当前状态字段统一为 `review_state`：

```text
MANUAL_DRAFT
MANUAL_CONFIRMED
INHERITED_PENDING
CARRIED_COVERED
```

Reject 不作为第二个 LineLink review state；由 active rejection ledger + relation active flag/可恢复 provenance 解释。

ordinary pending = uncovered 且没有当前有效 manual/inherited analysis relation。

## 8.17 D15：Decision Reason Codes

`coverage_inheritance_decisions` 只记录业务可判定结果；技术失败以 `coverage_import_failures`/Job failure ledger 为唯一 authority，不伪造成某个 candidate line 的普通 Decision。Decision 至少记录：

```text
decision_run_id CHAR(64)
candidate_scan_id
candidate_line_id
source_scan_id
source_line_id
source_relation_id
decision
reason_code
algorithm_version
old_commit_sha
new_commit_sha
line_mapping_fingerprint
function_identity_fingerprint
control_context_fingerprint
preprocessor_context_fingerprint
dependency_fingerprint
evaluated_at
```

幂等键必须冻结，例如：

```text
UNIQUE(decision_run_id, candidate_line_id)
decision_run_id = SHA256(candidate_scan_id + predecessor_scan_id + algorithm_version + input/readset fingerprint)
```

恢复重跑同一 run 必须 UPSERT/compare，不产生重复 Decision/LineLink。

建议 reason code：

```text
INHERITED
CARRIED_COVERED
NO_PREDECESSOR
BRANCH_MISMATCH
NON_ANCESTOR
UNSUPPORTED_LANGUAGE
PATH_CHANGED
BLOCK_AMBIGUOUS
BLOCK_FORK_OR_MERGE
LINE_AMBIGUOUS
LINE_DELETED
LINE_DELETED_REINTRODUCED
LINE_CODE_CHANGED
FUNCTION_ID_UNRESOLVED
FUNCTION_CHANGED
CONTROL_CONTEXT_CHANGED
PP_CONTEXT_CHANGED
MACRO_CHANGED
CONST_CHANGED
DEPENDENCY_UNRESOLVED
CALLEE_CHANGED
CALLEE_UNRESOLVED
REJECTION_ACTIVE
PARSER_UNRELIABLE
```

技术失败记录在 Job/import failure ledger，不伪装成 ordinary reason；发生 technical failure 的 Candidate 不允许凭部分 decisions 进入 publication。

## 8.18 D16：缓存与批处理

为了支撑生产量级：

缓存 identity：

```text
(repository_id, commit_sha, file_path/blob_sha)
```

可缓存：

- tokenized lines；
- function identity；
- AST/context indexes；
- macro definitions；
- function normalized body hash；
- file blob SHA。

要求：

- cache bounded；
- 不把 cache 当权威；
- key 包含 commit/blob；
- batch DB read/write；
- 禁止每 candidate line 独立查询 DB/Git/parser；
- 记录 `parser_candidate_total / parser_unresolved_total{reason}`、callee/macro unresolved rate，不设继承率 KPI，但必须知道保守退化规模。

## 8.19 D17：Inheritance Engine Pipeline

建议固定执行顺序：

```text
1. load fixed candidate.predecessor_scan_id; evaluate repositories only inside that one Scan
2. verify branch/ancestry/commits
3. prepare old/new snapshots
4. select supported files from .info
5. build deterministic Git line map
6. load source analysis relations/rejections
7. for each candidate source line:
   a. exact path
   b. block mapping
   c. unique line mapping
   d. normalized line tokens
   e. full function identity
   f. control ancestors
   g. preprocessor ancestors
   h. actual macro/constant dependencies
   i. actual direct callee dependencies
   j. rejection chain
8. write decision
9. create INHERITED_PENDING / CARRIED_COVERED relation
10. rebuild stats
11. conservation/integrity checks
```

任何 gate fail 后不要继续做昂贵后续 gate，以减少成本；但必须记录最终 reason code。

## 8.20 Gate D R01～R83 Traceability

为了避免遗漏，机器权威唯一固定为：

```text
contracts/inheritance_rules_v1.json
```

Markdown 中的 R01～R83 只是由 JSON 生成/校验的可读镜像。CI 计算 JSON SHA256，并校验本文附录中的 `rules_contract_sha256`；禁止手工维护三份互相漂移的规则。

每条规则至少包含：

```text
rule_id
owner_module
test_ids
reason_codes
status
```

分组责任：

| 规则 | 主要模块 |
|---|---|
| R01-R18 | predecessor/git/function/dependency |
| R19-R27 | bridge/line/block/record split |
| R28-R36 | import/failure/multi-repo |
| R37-R50 | review/rejection/progress |
| R51-R60 | repo/worktree/lock/current/read-set |
| R61-R70 | parser/language/normalizer/callee/macro |
| R71-R83 | control/preprocessor/hunk/delete/recovery/header/undo |

Gate D 不允许 `status=TODO` 的 R rule 进入 PASSED。

## 8.21 Gate D 定向测试矩阵

### Git / predecessor

- same commit；
- direct ancestor；
- merge ancestor；
- non-ancestor；
- no predecessor；
- same branch / different branch；
- local missing commit fetch；
- remote missing commit technical failure。

### Line map

- unchanged line；
- line shifted by insertion；
- whitespace-only；
- comment-only；
- same-hunk normalized recovery；
- duplicate identical lines ambiguity；
- 1→N；
- N→1；
- move across hunk；
- file rename/move；
- delete/re-add。

### Parser/function

- C function；
- C++ namespace；
- class method；
- overloaded method；
- parameter signature change；
- constructor/destructor/operator（在 parser 支持范围）；
- parser uncertainty。

### Token

- identifier rename；
- literal change；
- operator change；
- comment/indent change；
- strings containing comment markers。

### Control/preprocessor

- nested if；
- outer if change；
- unrelated branch change；
- switch/case；
- loop condition；
- nested #if；
- #ifdef symbol change；
- #elif branch change。

### Dependencies

- macro unchanged/changed；
- constexpr unchanged/changed；
- direct callee unchanged/changed；
- unrelated callee changed；
- function pointer；
- virtual dispatch；
- ambiguous overload；
- cross repo direct call。

### Analysis/review

- source draft inheritance；
- source confirmed inheritance；
- inherited_pending re-inherit；
- partial block inherit；
- shared record split；
- covered bridge；
- reject chain；
- undo reject current Scan；
- manual re-analysis after reject starts new chain；
- no successor retroactive rewrite。

### Failure

- parser uncertainty ordinary pending；
- DB exception technical failure；
- worktree unreadable technical failure；
- one repo technical failure rolls whole Scan；
- one repo ordinary no-inherit does not fail other repos。

## 8.22 Gate D Correctness Acceptance

硬要求：

- deterministic fixture corpus 中 0 个已知 false-positive inheritance；
- 相同输入重复运行 decision/result 完全一致；
- 每个不继承 candidate 都有明确 reason；
- `INHERITED_PENDING` 不被当 confirmed；
- R01-R83 traceability 全部 PASS；
- ordinary no-inherit 不导致 Scan technical failure；
- technical failure 不发布 Candidate。

## 8.23 Gate D Evidence Bundle

```text
gate-d/
  rule_traceability.json
  reason_code_catalog.json
  deterministic_fixture_manifest.json
  decisions_run_1.json
  decisions_run_2.json
  determinism_diff.json
  false_positive_check.json
  parser_uncertainty_report.json
  dependency_resolution_report.json
  targeted_tests.txt
  evidence-manifest-v2.json
  gate_d_result.json
```

## 8.24 Gate D PASS 条件

- R01～R83 machine-readable traceability 无缺项；
- deterministic fixture corpus 中无已知 false-positive inheritance；
- 相同输入重复运行结果与 decision ledger 一致；
- Git line mapping / function / control / preprocessor / dependency hard gates 全部按 fail-closed 工作；
- covered bridge、delete/re-add、partial Block、reject/undo 行为符合契约；
- parser/callee unresolved 被归为 ordinary pending，而技术异常阻断 Scan publication；
- Inheritance Engine 已真实接入 Gate C durable import pipeline，不是仅存在 provider/test 文件；
- progress rebuild 后 ordinary/inherited_pending/manual_draft 三类 pending 互斥守恒；
- 无 P0/P1。

Gate D 输出给 Gate E：**稳定的 inheritance DTO / relation state / reason code / review contract**。

---

# 9. Gate E — API / UI / Progress / Browser Acceptance

## 9.1 Gate E 目标

把 Gate B-D 的新数据与状态完整接入 canonical VNext API 和用户界面，使“继承”成为可理解、可复核、可拒绝、可撤销、可统计的正常业务流程。

Gate E 必须证明行为由 VNext canonical owner 提供，不能依赖 intended-to-retire legacy fallback 假通过。

## 9.2 E1：Target API Contract v1（Gate E 开发前冻结）

以下是本功能唯一目标契约；实现阶段不得再以“建议 endpoint”形式保留多套可能性。若现有 canonical API 需要兼容旧调用，兼容层只能转发到这些 owner，不得形成第二业务实现。

### Scan Import / Job

```text
POST /api/coverage/scans
GET  /api/coverage/jobs/{job_id}
```

Import 成功受理返回 `202`；physical repo busy 返回 `409 REPOSITORY_BUSY`，且无 Candidate Scan。

### Inheritance query

```text
GET /api/coverage/scans/{scan_id}/inheritance/pending?cursor=&limit=
GET /api/coverage/scans/{scan_id}/inheritance/decisions?cursor=&limit=&reason_code=
```

- `limit` 默认 100、最大 500；
- cursor opaque、稳定绑定 scan/data_version/filter；
- 禁止一次返回全部 5 万+ inherited lines。

### Inheritance mutation

```text
POST /api/coverage/scans/{scan_id}/inheritance/confirm
POST /api/coverage/scans/{scan_id}/inheritance/edit-confirm
POST /api/coverage/scans/{scan_id}/inheritance/reject
POST /api/coverage/scans/{scan_id}/inheritance/rejections/{rejection_id}/undo
```

Mutation request 的 concurrency bundle：

```text
expected_relation_revision
expected_record_revision        # 编辑内容时必需
expected_rejection_revision     # undo / 修改 rejection 时必需
selected_line_ids[]
```

### Stable error codes

至少冻结：

```text
REPOSITORY_BUSY
SCAN_NOT_CURRENT_FOR_MUTATION
STALE_RELATION_REVISION
STALE_RECORD_REVISION
STALE_REJECTION_REVISION
UNDO_NOT_ALLOWED
INVALID_SCAN_IDENTITY
PAGINATION_CURSOR_STALE
```

## 9.3 E2：API Identity / Authorization Contract

角色矩阵：

| 能力 | viewer | reviewer | importer | admin |
|---|---:|---:|---:|---:|
| 读取项目/Scan/decision | ✓ | ✓ | ✓ | ✓ |
| confirm/edit/reject/undo | - | ✓ | - | ✓ |
| 创建 Scan/import | - | - | ✓ | ✓ |
| debug decision raw fingerprints | - | - | - | ✓ |
| release/cutover operations | - | - | - | 运维/发布专用身份 |

所有 line/review mutation 必须同时满足 `scan_id == coverage_project_state.current_scan_id`，历史 Scan 只读。

所有 line/review mutation 必须显式绑定：

```text
project
scan_id
repository identity
file identity
line_id / line_number
expected relation revision（需要并发保护时）
```

禁止：

- 只按 file_path 跨 Scan fallback；
- 缺 Scan 时自动用 CURRENT 进行 mutation；
- source cache miss 时 fallback 到当前 Git HEAD。

## 9.4 E3：Progress Contract

必须满足：

```text
pending_total
= ordinary_pending_total
+ inherited_pending_total
+ manual_draft_pending_total
```

其中三类严格互斥：

- ordinary pending：uncovered 且无 active AnalysisLineLink；
- inherited pending：active `review_state=INHERITED_PENDING`；
- manual draft pending：active `review_state=MANUAL_DRAFT`；
- `CARRIED_COVERED` 不计 pending；
- `MANUAL_CONFIRMED` 不计 pending。

Progress DTO 增加：

```text
pending_total
ordinary_pending_total
inherited_pending_total
manual_draft_pending_total
confirmed_total
draft_total
```

文件级、项目级聚合都必须守恒。

## 9.5 E4：Code Detail Line DTO

建议增加：

```text
analysis_relation_state
is_inherited
inheritance_source_scan_id
inheritance_source_line
inheritance_reason
can_confirm_inheritance
can_reject_inheritance
can_undo_rejection
relation_revision
```

默认 UI 只暴露用户需要的信息；debug reason 可通过展开详情/管理员模式查看。

## 9.6 E5：继承视觉状态

建议保持当前 iOS 风格简洁：

### 行级

- `继承` Badge；
- `待复核` 状态；
- 不用强烈警告色制造误解；
- confirmed 后变成普通已分析视觉，不持续强调历史来源，来源可在详情查看。

### Block 工具栏

- `确认继承`；
- `编辑并确认`；
- `拒绝继承`；
- 每行 checkbox/opt-out；
- 被拒绝且可撤销时显示 `撤销拒绝`。

## 9.7 E6：Block 批量确认

默认行为：

1. 打开待复核 Block；
2. 默认选中全部 inherited lines；
3. 用户可以取消某些 line；
4. Confirm 只作用于当前选中 line；
5. reviewer/time 写当前用户/当前时间；
6. 未选中 line 保持 inherited pending。

必须防止重复提交：

- relation_revision optimistic check；
- 如果操作改变 AnalysisRecord 内容，同时要求 expected_record_revision；
- 重复相同 confirm 幂等或给明确 conflict；
- stale UI 不静默覆盖新分析。

## 9.8 E7：编辑并确认

如果 selected lines 只占共享 Record 部分：

- UI 不需要理解 split 细节；
- 后端自动 R27 split；
- request 同时携带 expected_relation_revision + expected_record_revision；response 返回新的 relation/record revision；
- 页面只更新受影响 line。

## 9.9 E8：Reject / Undo UX

Reject 前建议轻量确认：

```text
“拒绝后，该旧结论不会继续自动传递到后续版本。”
```

Undo：

- 仅当前 Scan 且没有新人工分析时展示；
- 点击时同时校验 expected_rejection_revision + expected_relation_revision；通过后恢复继承内容和待复核状态；
- 如果已经人工重分析，按钮不可用，API 也必须拒绝。

## 9.10 E9：继承 Decision Explainability

管理员/高级用户可以查看：

```text
Inherited from Scan X / commit Y
reason: INHERITED
line mapping: exact / same-hunk-format-only
function identity: matched
dependency proof: matched
```

不需要把所有 hash 展示给普通用户；但 API/日志必须可追溯。

对不继承行，普通用户无需看到大量 reason；管理员可以查询 decision reason 进行排障。

## 9.11 E10：Lazy Collapse 集成

继承状态必须与已有 Code Detail Lazy Collapse 一起工作：

- `INHERITED_PENDING` 属于“待分析/待复核”，默认展开策略应与待分析一致；
- covered carried line 不应强制展开；
- 请求取消后 loading state 清理；
- re-expand 不重复 DOM；
- chunk response reordering 不打乱 line；
- 未保存编辑不能被 cache eviction 清除。

## 9.12 E11：搜索/筛选

至少：

```text
全部待分析
普通待分析
待复核继承
已确认
草稿
```

搜索仍遵守当前产品约定：仅搜索已加载内容，除非后续单独升级 server-side search。

## 9.13 E12：真实 HTTP 验证

必须启动真实 VNext application/runtime，检查：

- routes；
- authentication/authorization；
- Scan identity；
- DTO；
- mutation；
- revision conflict；
- progress freshness；
- job polling；
- no legacy fallback。

## 9.14 E13：完整 VNext Modification-related Parity

由于 Gate B/C 改动 Analysis/Scan/Job canonical ownership，Gate E 不能只验证 inheritance 页面。必须按受影响路径覆盖：

```text
projects / scans
report registry + report identity
code-detail layout + lines
analysis read/write
progress freshness
incremental report（继续使用自己的 oldgit/newgit contract）
export + download
jobs + restart recovery
inheritance query/mutation
```

每个路径分别证明：canonical VNext route 被真实 runtime 使用、没有 intended-to-retire legacy fallback、Scan/report/repository/file identity fail closed。

Shared JS/CSS 发生变化时必须产生新的 asset identity，并执行“浏览器携带旧缓存 → 加载新页面”的 cache invalidation regression；不得依赖用户手工清缓存。

## 9.15 E14：真实 Chromium / Playwright 验收

必须覆盖：

1. 页面首次加载 inherited pending；
2. 筛选 inherited；
3. Block 批量确认；
4. 逐行 opt-out；
5. edit + confirm；
6. reject；
7. undo reject；
8. reject 后 manual analysis，undo 消失；
9. 页面刷新后状态正确；
10. 切换 tab/前后台后统计刷新；
11. Lazy Collapse expand/collapse；
12. 快速重复展开/取消；
13. 大文件分批加载；
14. 无 duplicate DOM lines；
15. 无非 allowlist Vue/runtime warning（若当前前端框架涉及）；
16. console 无新增 error；
17. network 不出现 legacy endpoint fallback；
18. pending total 守恒。

Mock DOM 测试保留，但不能替代以上真实浏览器证据。

## 9.16 E15：性能验收与硬预算

按层记录：

```text
server latency
DB query count / rows
network request count / payload
Sidecar decode/cache（如涉及）
browser parse/render
DOM node count
```

使用至少三档 workload：

- 小文件；
- 常规大文件；
- 超大源文件/大量 inherited pending。

不混用不同 cache state 做错误对比。

目标：新增 inheritance UI 不破坏现有 Lazy Collapse 性能设计，不制造 per-line N+1 API/DB 请求。

### 固定 workload tiers

至少使用：

| Tier | total source lines | uncovered/inherited candidate lines | 用途 |
|---|---:|---:|---|
| S | 1,000 | 50 | 常规交互 |
| M | 10,000 | 500 | 大文件 |
| L | 50,000 | 5,000 | 超大文件 |
| XL | 100,000 | 20,000 | 容量/退化边界 |

如真实生产 P95/P99 文件规模更大，必须增加 production-derived tier，不能用较小 synthetic workload 替代。

### PASS/FAIL regression budget

在相同数据集、相同 cache state、相同 Candidate host 下比较当前受支持 baseline：

```text
server p95 latency        <= baseline * 1.15
browser action p95        <= baseline * 1.15
network request count     不允许出现 per-line N+1；同动作额外固定请求 <= 2
DB query count            不允许与 selected line 数线性增长
payload bytes             <= baseline * 1.20（除明确新增 inheritance DTO 字段的可解释增量）
DOM nodes after settle    <= baseline * 1.20
```

任何超过 budget 的结果默认 Gate E `BLOCKED`；若性能指标受新增必要信息影响，需要先做根因拆分和书面 P2 风险接受，不能直接把阈值改宽。

`scripts/diagnostics/synthetic_dom_microbenchmark.js` 只输出同一版本内的
DOM 微基准和浏览器功能数据，不能作为发布 A/B。发布性能证据必须先在两个
隔离且精确绑定 commit 的 checkout 中分别产生
`release_performance_revision`，再使用：

```text
npm run perf:browser-ab -- \
  --baseline-artifact <before.json> \
  --candidate-artifact <candidate.json> \
  --baseline-commit <before-sha> \
  --candidate-commit <candidate-sha> \
  --workload-hash <固定 workload hash> \
  --output <release-performance-ab.json>
```

合并器会校验两个源产物的 SHA256、workload/environment identity、A-D 固定
规模和 100k 虚拟滚动预算；`run_upgrade.py` 只接受该
`release_performance_ab` 产物。


## 9.17 Gate E 主要修改文件

```text
app/api/application.py
app/api/endpoints/analysis.py
app/api/endpoints/progress.py
app/api/endpoints/jobs.py
新增 app/api/endpoints/inheritance.py
app/services/analysis_service.py
app/services/progress_service.py
app/services/inheritance_review_service.py
app/code_detail/*
web/assets/js/coverage_enhance.js
web/assets/js/coverage_progress.js
相关 HTML/template/asset identity
```

## 9.18 Gate E 定向测试

只跑修改点相关测试，但 Gate B/C 的 canonical ownership 变化要求本 Gate 同时覆盖 parity：

- projects/scans/report identity；
- code detail layout/lines；
- analysis/progress；
- incremental/export/download/jobs；
- inheritance API；
- progress inherited；
- analysis mutation；
- code detail DTO；
- job polling；
- VNext API contract；
- inheritance Playwright；
- Lazy Collapse 相关 regression。

不跑无关全量 suite。

## 9.19 Gate E Evidence Bundle

```text
gate-e/
  api_contract.json
  route_inventory.json
  progress_conservation.json
  browser_scenarios.json
  playwright_report/
  console_errors.json
  network_trace_summary.json
  performance_metrics.json
  legacy_fallback_audit.json
  targeted_tests.txt
  evidence-manifest-v2.json
  gate_e_result.json
```

## 9.20 Gate E PASS 条件

- 用户可完整确认/编辑/拒绝/撤销继承；
- progress 三类 pending 守恒；
- mutation 强制 CURRENT-only；
- inherited/decision query 分页上限生效；
- stale Record/Relation/Rejection revision 均有并发保护；
- Code Detail/Lazy Collapse 正确；
- 真实 VNext HTTP PASS；
- 真实 Chromium PASS；
-完整 VNext modification-related parity PASS；
- 性能 regression budget PASS；
- shared asset identity/cache invalidation PASS；
- 无 intended-to-retire legacy fallback；
- 无 P0/P1。

---

# 10. Gate F — Candidate Rehearsal / Release / Cutover / Rollback / Skill Drift

## 10.1 Gate F 目标

将 A～E 已完成的 Candidate，在与 Current 完全隔离的环境中完成生产级迁移、验证和可回滚切换。

Gate F 是发布治理 Gate，不重复拥有 A～E 的源代码根问题；任何上游 P0/P1 未关闭时直接 BLOCKED。

## 10.2 F1：固定 Candidate Identity

记录：

```text
repository
branch
commit SHA
release SHA/build identity
static asset identity
schema versions
inheritance algorithm_version
config hash
database_runtime_identity
evidence_manifest_v2_sha256
```

所有测试/证据必须引用同一个 Candidate identity，禁止“代码测试一个 commit、部署另一个 commit”。

F1 创建 release identity 后立即触发 exact-SHA Change Review plan；任何 F1 之后的代码变化都会使之前的 source/security/canonical review 失效并要求重跑。

## 10.3 F2：双环境布局

按既定同服务器策略：

```text
Current:   /home/zcyu/coverage
Candidate: /home/zcyu/coverage_candidate
```

但正式执行前必须通过 fresh inventory 再确认实际路径。

Candidate 独立：

- service/process identity；
- port；
- config；
- logs；
- runtime-state；
- cache；
- report staging；
- database；
- temp worktrees。

禁止 Candidate 写 Current DB。

## 10.4 F3：Fresh Production Inventory

虽然当前已有 `fos_full_inventory_20260820_135937`，正式 cutover 前仍需要足够新鲜的 inventory，用于确认：

- DB schema/table counts/data_version；
- active processes；
- repository paths/HEAD；
- service port/Nginx；
- Current/Candidate roots；
- writable persistent roots；
- jobs；
- free disk；
- backup location；
- Nginx/reverse-proxy/auth trust boundary；
- Candidate/Current DB runtime fingerprint。

磁盘必须满足：

```text
free_bytes >= current_release_bytes
             + candidate_release_bytes
             + final_target_db_estimate
             + verified_backup_bytes
             + max_temp_worktree_bytes
             + migration_temp_bytes
             + max(20% * preceding_sum, 10 GiB safety margin)
```

旧 inventory 作为设计证据，不作为未来 cutover 时永久事实。

## 10.5 F4：Candidate Rehearsal + Production Toolchain Preflight

正式 release 前在目标主机验证 Gate D 实际选择的 parser/toolchain：

```text
executable/path
version
binary SHA256（可获取时）
permissions
Python/runtime compatibility
Git version
required filesystem behavior
```

缺少实际 parser/clang/helper、版本不兼容或执行权限不足 → Gate F `BLOCKED`。

Candidate migration rehearsal 必须使用 **verified production backup 恢复出的 Legacy source copy** 作为主证据；synthetic corpus 只补充边界。

演练必须严格使用：

```text
Legacy rehearsal source
→ Empty VNext rehearsal target
→ Gate A migration
→ Gate B backfill
→ Inheritance schema
→ derived rebuild
→ Candidate runtime
```

不允许把 Current production DB 直接给 Candidate 写。

### Rehearsal 验证

- semantic zero-loss；
- schema versions；
- Analysis parity；
- identity chain；
- progress；
- jobs；
- API；
- browser；
- sample Scan import/inheritance；
- restart/recovery；
- rollback rehearsal。

## 10.6 F5：Verified Recoverable Backup

正式切换前：

1. freeze/drain Current writes；
2. 创建完整 MySQL dump；
3. backup root 在 Current/Candidate deploy root 之外；
4. 验证 dump 结构和恢复可信度；
5. 保存 SHA256；
6. 保存 schema + authoritative semantic snapshot；
7. 禁止只有一个非空文件/hash 就声称 backup PASS。

## 10.7 F6：Freeze / Drain

需要明确阻断：

- 人工 analysis write；
- 新 Scan import；
- 仍可恢复的 background workers；
- 任何可能改变 authoritative DB 的 legacy task。

最终 authoritative dump 前必须：

1. reverse proxy/入口拒绝所有 write route；
2. 停止 Current API 中所有 write-capable worker/service；
3. 停止/完成所有 background jobs；
4. 检查 OS process/service identity，确认没有第二个 legacy writer；
5. 检查 MariaDB active sessions/transactions，识别应用 writer；
6. 连续两次间隔采集 `data_version + authoritative semantic snapshot` 保持稳定；
7. 然后才创建 final authoritative dump。

```text
active jobs = 0
write-capable app processes = 0
active app write transactions = 0
data_version stable across freeze interval
write endpoints closed
```

不建议在共享 MariaDB 上粗暴设置 global read_only；如果使用数据库账号 revoke/fence，必须证明不会影响无关业务并有恢复步骤。

## 10.8 F7：Final Migration

正式流程：

```text
1. Final Legacy source snapshot/dump
2. Fresh Empty VNext Candidate Target DB
3. Apply VNext Core Schema
4. Legacy → VNext
5. semantic zero-loss
6. Analysis Domain migration/backfill
7. Analysis parity
8. Repository Master + Inheritance Schema
9. derived state rebuild
10. target consistency checks
11. 写入 release manifest：final_target_database_runtime_identity + schema/migration hashes
12. Candidate config 显式绑定该 final target DB，并在启动前 assert runtime fingerprint
13. Candidate start with traffic closed
```

任何一步失败：不开流量。

## 10.9 F8：Traffic-closed Verification

### Exact-SHA Source/Canonical/Security Gate

Traffic-closed runtime verification前，必须对**最终将部署的 exact Candidate SHA**重新获得：

- Change Review source correctness；
- runtime participation/canonical ownership；
- targeted security/trust-boundary review；
- targeted tests selection/compatibility；
- unresolved P0/P1 = 0。

### 最终 Target DB 只读原则

最终 production Candidate Target DB 在开流量前原则上只做只读验证。analysis/reject/undo/import 的写场景必须已经在 rehearsal DB 通过；禁止为了 smoke test 污染最终权威数据。若必须执行写测试，只能使用事先设计的 isolated acceptance project，并要求事务/清理后 authoritative semantic hash 回到测试前完全一致，否则 `DATA_SAFETY_HOLD`。

至少：

### DB

- source/target semantic hashes；
- project/data_version；
- line/analysis counts；
- Analysis Domain orphan checks；
- CURRENT pointer；
- schema versions。

### Runtime

- health/release identity；
- project/scan list；
- progress；
- code detail；
- analysis read-only projection；写路径证据引用 rehearsal exact-SHA 结果；
- jobs/recovery；
- repository identity；
- no cross-fallback。

### Browser

- E Gate 核心 smoke 场景；
- static asset identity；
- no legacy API fallback；
- Nginx/reverse-proxy `X-Remote-User` 信任来源、trusted proxy addresses、CORS/origin、external endpoint auth 均与 Candidate config 一致。

## 10.10 F9：Traffic Cutover

只切流量入口，不修改旧环境文件树。

步骤：

1. Candidate verification PASS；
2. 保存 pre-cutover evidence；
3. 切 Nginx/entrypoint/service binding；
4. 验证 external endpoint；
5. 记录 cutover timestamp；
6. Current 保持停止/只读待回滚，不删除。

## 10.11 F10：Rollback Boundary / Exact Previous Identity

Rollback rehearsal 与正式 rollback 都必须证明：

```text
rollback_release_identity == pre_cutover_before_release_identity
rollback_database_identity == pre_cutover_authoritative_db_identity（在尚无 Candidate 新写时）
```

当 `target_release_identity != before_release_identity` 时，禁止把 target 自己或任意“previous-looking”目录冒充 rollback target。`run_rollback_rehearsal.py` / manifest validator 必须机器校验。


### Candidate 尚未接受新权威写入

可以验证后直接把流量切回 preserved Current + its authoritative DB。

### Candidate 已接受新权威写入

禁止直接切回旧 DB，因为会丢 Candidate 新写入。

必须：

- 分析 reverse migration / incremental reconciliation；
- 无法证明零损失 → 关闭流量；
- 进入 `DATA_SAFETY_HOLD`；
- 保留 Current/Candidate/backup/evidence；
- 人工决定 lossless recovery。

## 10.12 F11：Acceptance Window

在验收窗口内保留：

- Current root；
- Current DB recovery material；
- verified backup；
- Candidate migration evidence；
- logs；
- release manifest。

不要立即删除旧环境。默认安全验收窗口 **至少 48 小时**，并且必须同时满足退出条件：

```text
>= 3 次成功的新 Scan import（至少包含 1 次有继承、1 次无继承/普通 pending）
>= 1 次服务正常重启后的 durable recovery 验证
>= 1 次大文件 Code Detail/复核流程
0 个 P0/P1
关键 error/technical_failure 指标无异常持续上升
DB authoritative semantic/integrity checks 持续通过
```

时间满但退出条件未满足，继续保留旧环境，不自动结束窗口。

验收关注：

- 新 Scan import；
- inherited pending；
- analysis save/progress refresh；
- background job persistence；
- restart recovery；
- large file code detail；
- DB growth/query behavior。

## 10.13 F12：Skill Drift Audit

这次架构会改变：

- persistence model；
- Repository identity；
- Scan lifecycle；
- Analysis ownership；
- Job/recovery；
- Code Detail DTO；
- UI state；
- release migration。

因此完成后必须审计：

```text
fos-coverage-maintainer
fos-coverage-change-review
fos-coverage-release-governance
fos-coverage-runtime-reliability
fos-coverage-performance-ui
```

检查：

- routing 是否仍正确；
- helper 是否认识新表/新路径；
- test selector 是否覆盖新模块；
- audit scripts 是否误判；
- ownership 是否出现重复/缺失；
- stable invariants 是否需要更新。

只有 Skill Drift Audit 关闭后，才能宣称“Skill suite current”。

## 10.14 Gate F Evidence Bundle

```text
gate-f/
  evidence-manifest-v2.json
  final_source_review.json
  final_security_review.json
  legacy_retirement.json
  release_identity.json
  database_runtime_identity.json
  fresh_inventory/
  candidate_layout.json
  candidate_config_audit.json
  verified_backup.json
  pre_freeze_semantic.json
  final_migration.json
  final_semantic_reconciliation.json
  runtime_verification.json
  browser_smoke.json
  cutover_record.json
  rollback_rehearsal.json
  acceptance_window_checks.json
  skill_drift_audit.json
  gate_f_result.json
```

## 10.15 Gate F READY 条件

### Gate F 决策矩阵

```text
P0/P1 unresolved or required evidence missing -> NOT_READY
All P0/P1 closed, P2/Info exist without approval -> NOT_READY
All P0/P1 closed, approved P2/Info only -> READY_WITH_ACCEPTED_RISK
All blocking findings closed and no accepted residual risk -> READY
```


全部成立：

- A～E `PASSED`；
- exact Candidate identity 固定且 exact-SHA source/security/canonical review PASS；
- fresh inventory + disk capacity formula PASS；
- parser/toolchain production preflight PASS；
- Candidate/Current/DB 隔离；
- verified recoverable backup；
- final zero-loss migration；
- traffic-closed verification（final Target read-only）PASS；
- rollback boundary 明确；
- 无 unresolved P0/P1；
- browser/runtime/data evidence 一致；
- Skill Drift PASS；
- Evidence Manifest v2 authenticity PASS；
- rollback rehearsal 回到 exact before-release identity；
- production proxy/auth trust-boundary PASS。

最终发布状态：

```text
READY
READY_WITH_ACCEPTED_RISK
NOT_READY
```

`READY_WITH_ACCEPTED_RISK` 只允许有经过 owner 批准并登记 evidence 的 P2/Info；任何 unresolved P0/P1 或缺失关键 P0/P1 证据均为 `NOT_READY`。

---

# 11. Gate A～F 并行开发策略

为了发挥高生成力和高生产效率，可以并行，但必须守住接口依赖。

## 11.1 可以并行的工作流

### Workstream 1 — Gate A

Migration Contract / Schema / semantic hash。

### Workstream 2 — Gate B Schema 设计准备

在 Gate A 业务语义字段冻结后，可并行写 Analysis/Repository DDL 和 Repository 类；但正式 backfill 必须使用 Gate A frozen contract。

### Workstream 3 — Gate C 基础设施

Repository Lock/Checkpoint/PublicationService 可以与 Gate B 后半段并行，只要 repository_id/current_scan_id contract 已冻结。

### Workstream 4 — Gate D parser/line-map fixtures

GitLineMap、Normalizer、Parser fixture 可提前开发，不依赖 DB 最终实现；真正 Engine wiring 要等 Gate B/C contract 固定。

### Workstream 5 — Gate E UI prototype

可基于冻结 DTO contract 开发，但不能以 mock 结果宣称 PASS；真实 API wiring 要等 D。

### Workstream 6 — Gate F release tooling

Manifest/backup/rehearsal scripts 可以提前适配新 schema keys，但最终 Gate F 只能在 A-E PASS 后进行。

## 11.2 不能并行越过的强依赖

```text
Gate A semantic contract
   ↓
Gate B migration/backfill
   ↓
Gate C stable repository/current/import contract
   ↓
Gate D engine production wiring
   ↓
Gate E final API/browser acceptance
   ↓
Gate F production READY
```

---

# 12. 推荐 PR / Commit 切分

即使一次性高强度开发，也建议保持可审计提交边界。

## Gate A

- `db: harden legacy-to-vnext migration contract`
- `test: add production-shape legacy migration fixtures`

## Gate B

- `db: add repository and canonical analysis domain`
- `refactor: switch analysis reads and writes to canonical relations`
- `test: verify analysis-domain backfill and split semantics`

## Gate C

- `refactor: isolate current scan publication`
- `feat: add repository locks and durable import checkpoints`
- `feat: add scan import recovery and atomic publication`

## Gate D

- `feat: add deterministic git line mapping`
- `feat: add c cpp inheritance context analysis`
- `feat: add deterministic analysis inheritance engine`
- `test: cover inheritance rules r01-r83`

## Gate E

- `feat: add inheritance review api and progress states`
- `feat: add inherited-pending code detail workflow`
- `test: add real-browser inheritance review acceptance`

## Gate F

- `release: add candidate migration and cutover evidence gates`
- `audit: update skill capability evidence after inheritance rollout`

不要求提交名完全一致，但每个 commit 应保持单一工程意图，方便回溯与 review。

---

# 13. CI / Workflow 设计

不建议为 A～F 新建六套大型 Workflow。

推荐保留少量 canonical workflow，通过 path/filter/test-plan 控制：

```text
VNext Targeted Verification
  ├─ migration/schema job
  ├─ analysis-domain job
  ├─ runtime/job job
  ├─ inheritance job
  └─ ui-contract job（非真实 production browser）
```

真实 Chromium 可以在已有 browser workflow 中增加 inheritance profile。

Release rehearsal 不应该伪装成普通 CI；它属于 Gate F Candidate evidence。

### Test Selection

根据 changed files 自动选择：

- `scripts/upgrade/**` → Gate A/F tests；
- `app/db/repositories/*analysis*` / `repository*` → Gate B；
- `app/jobs/**` / import/publication → Gate C；
- `app/inheritance/**` → Gate D；
- `app/api/**` + `web/assets/**` → Gate E。

任何“减少测试数量”的优化都必须保持等价 acceptance coverage，不允许通过删掉失败场景制造绿灯。

---

# 14. 跨 Gate 数据守恒公式

## 14.1 Legacy → VNext

```text
Legacy Project facts == VNext Project semantic facts
Legacy Line facts == VNext physical line semantic facts
Legacy Analysis facts == VNext analysis semantic facts
Legacy data_version == target authoritative version semantic
```

## 14.2 VNext → Analysis Domain

```text
old coverage_analyses count
== migrated current AnalysisLineLink count
```

内容语义：

```text
conclusion_status + review_state + reviewer/method/reason/comment + legacy raw provenance
```

必须等价。

## 14.3 Progress

```text
pending_total
= ordinary_pending_total
+ inherited_pending_total
+ manual_draft_pending_total
```

```text
uncovered_total
= pending_total
+ confirmed_uncovered_total
```

三组必须互斥：ordinary = uncovered 且无 active relation；inherited = active `INHERITED_PENDING`；manual_draft = active `MANUAL_DRAFT`。`CARRIED_COVERED` 与 `MANUAL_CONFIRMED` 不计 pending。

## 14.4 Publication

任意时刻每 project：

```text
0 或 1 个 current_scan_id
```

不得存在两个 CURRENT authority。

## 14.5 Inheritance Mapping

```text
old physical line -> at most 1 new line
new candidate line <- at most 1 old source line
```

所有自动 inherited relation 都必须可追到：

```text
source scan
source physical line
source relation
source analysis record
algorithm version
decision evidence
```

---

# 15. 统一错误分类

## 15.1 Business Ineligible

HTTP/Job 不报技术失败，Scan 可继续：

```text
NO_PREDECESSOR
BRANCH_MISMATCH
NON_ANCESTOR
UNSUPPORTED_LANGUAGE
PATH_CHANGED
LINE_AMBIGUOUS
LINE_CODE_CHANGED
FUNCTION_CHANGED
CONTROL_CONTEXT_CHANGED
PP_CONTEXT_CHANGED
MACRO_CHANGED
CALLEE_CHANGED
CALLEE_UNRESOLVED
PARSER_UNRELIABLE
REJECTION_ACTIVE
```

## 15.2 Technical Failure

Candidate 不发布：

```text
REQUIRED_COMMIT_UNAVAILABLE
SOURCE_UNREADABLE
DB_WRITE_FAILED
LOCK_FENCING_FAILED
CHECKPOINT_CORRUPT
READ_SET_CHANGED
EXPECTED_CURRENT_CHANGED
WORKTREE_INVALID
ENGINE_EXCEPTION
CONSISTENCY_FAILED
```

## 15.3 API Conflict

例如：

```text
REPOSITORY_BUSY
STALE_RELATION_REVISION
UNDO_NOT_ALLOWED
SCAN_NOT_CURRENT_FOR_MUTATION
STALE_RECORD_REVISION
STALE_REJECTION_REVISION
PAGINATION_CURSOR_STALE
```

必须给稳定 code，不依赖中文 message 做程序判断。

---

# 16. 统一 Observability

建议 metrics：

```text
scan_import_total{state}
scan_import_phase_duration
repository_lock_busy_total
repository_lock_stale_recovery_total
repository_fencing_reject_total
physical_repository_resource_count
inheritance_candidate_total
inheritance_success_total
inheritance_ineligible_total{reason}
inheritance_unresolved_total{reason}
inheritance_technical_failure_total{reason}
inherited_pending_total
covered_carried_total
parser_cache_hit_ratio
parser_candidate_total
parser_unresolved_total{reason}
source_cache_hit_ratio
worktree_create_total
job_resume_total
job_resume_failed_total
atomic_publish_total{result}
```

日志必须携带：

```text
project_id
scan_id
job_id
repository_id
physical_resource_id
candidate commit
phase
```

禁止日志直接输出用户敏感分析内容或完整源码片段作为默认 debug。

---

# 17. 安全与信任边界

本次新增 Git/worktree/parser/subprocess 后必须专项检查：

- repo path 必须受 configured allowed root 限制；
- branch/commit 当作数据，不拼 shell；
- worktree 路径不可由用户任意穿越；
- fetch remote 使用已配置 repo，不接受任意 URL 注入；
- API mutation 按 E2 role matrix 授权并强制 CURRENT-only；
- export/decision debug 不泄露 server path；
- temp worktree 权限最小化；
- cleanup 不允许递归删除未验证根路径；
- migration credentials 不写 evidence 明文；
- shared JS/CSS 变更必须更新 asset identity，并验证旧缓存客户端不会继续调用旧 DTO/route。

如发现 source-level 安全问题，由 Change Review 作为 root owner；Release Gate 只消费其 severity。

---

# 18. Gate A～F 总体验收矩阵

| 能力 | A | B | C | D | E | F |
|---|---|---|---|---|---|---|
| Legacy DB zero-loss | **主责** | 回归 | 回归 | - | - | 最终验证 |
| Repository Master | - | **主责** | 使用 | 使用 | 展示 | 生产验证 |
| Analysis canonical | - | **主责** | 使用 | 使用 | 使用 | 生产验证 |
| CURRENT single owner | - | 约束 | **主责** | 使用 | 展示 | 生产验证 |
| Durable import | - | - | **主责** | 集成 | Job UI | 生产验证 |
| R01-R83 | - | 基础 | 基础 | **主责** | 复核 | 生产样本 |
| Progress inherited | - | 数据基础 | rebuild | 状态来源 | **主责** | 生产验证 |
| Browser workflow | - | - | - | - | **主责** | smoke |
| Cutover/rollback | rehearsal input | schema input | recovery input | correctness input | parity input | **主责** |
| Skill drift | - | - | - | - | - | **主责** |

---

# 19. 总体 Definition of Done

只有以下全部成立，这次项目才算真正完成：

1. **兼容旧库 Hard Constraint 有真实迁移证据，不只是设计声明。**
2. Legacy Source → Empty VNext Target 零损失。
3. Repository Master / Scan Snapshot / File / Line identity 无歧义。
4. AnalysisRecord 只拥有内容；LineLink 单独拥有 review state；AnalysisBlock 只拥有人类选择事实；InheritanceGroup 只拥有自动继承组织事实，四者无双重权威。
5. Analysis save 不会切 CURRENT。
6. Current pointer 只有 PublicationService 能修改。
7. Physical Repository Resource Lock 在 Scan residue 前获取；fencing token 永久单调且所有关键写在同事务验证。
8. Scan Import 使用 immutable staged input，可 checkpoint CAS/resume，handler_version 可重建。
9. 主 Git working tree HEAD 不被历史比较切换。
10. R01～R83 全部有代码 owner + targeted tests + result。
11. deterministic corpus 没有已知 false-positive inheritance。
12. `pending_total = ordinary + inherited_pending + manual_draft_pending` 严格守恒。
13. covered bridge/delete chain/reject/undo 符合契约。
14. API 显式 Scan/Repository/File/Line identity，fail closed。
15. 真实 Chromium 完成继承复核 workflow。
16. Candidate 与 Current 数据库/路径/端口隔离。
17. verified recoverable backup。
18. final production migration semantic reconciliation PASS。
19. rollback boundary 明确且演练能够回到 exact before-release release/db identity。
20. Skill Drift Audit 完成。
21. 所有 P0/P1 必须关闭；`READY_WITH_ACCEPTED_RISK` 只允许 P2/Info。缺证据不能伪装 READY。
22. 最终 Candidate exact SHA 已重新完成 source/security/canonical ownership review。
23. Gate D parser/toolchain 已在生产目标主机通过 dependency preflight。
24. Evidence Manifest v2 完整且所有 production-advancing artifact provenance 可验证。

---

# 20. 实施完成后的最终交付物

```text
docs/
  FOS_Coverage_Gate_A-F_详细开发与验证总方案.md
  deterministic_inheritance_contract_v1.md/json
  migration_matrix.md/json
  api_contract.md
  evidence_manifest_v2.schema.json
  release_identity_contract_v2.md

schema/
  vnext_core
  analysis_domain
  inheritance_domain

app/
  canonical repository / analysis / scan import / inheritance modules

scripts/upgrade/
  migration / preflight / rehearsal / cutover / rollback evidence

tests/
  gate-specific targeted suites
  real-browser inheritance suite

evidence/
  gate-a/
  gate-b/
  gate-c/
  gate-d/
  gate-e/
  gate-f/
```

每个 Gate 的结果 JSON 建议统一：

```json
{
  "gate": "GATE_A",
  "candidate_revision": "<sha>",
  "status": "PASSED|BLOCKED|INCOMPLETE",
  "executed": [],
  "passed": [],
  "failed": [],
  "skipped": [],
  "missing_evidence": [],
  "blocking_findings": [],
  "artifacts": []
}
```

---

# 21. 附录 A — R01～R83 实现不得删减声明

Gate D 的机器权威来源为 `contracts/inheritance_rules_v1.json`。发布时文档写入：

```text
rules_contract_version = 1
rules_contract_sha256 = f85a441535fd67ba13eb21eeaf2cba4acbfb1fe906b7ee36161d02c31e7f8975
```

上游 v2.0《Deterministic Inheritance Contract v1》和本附录是该 JSON 的人类可读镜像。

开发时必须把 83 条规则复制为 machine-readable traceability，不允许只在代码注释中零散表达。Gate D PASS 前必须自动检查：

```text
expected_rules = R01..R83
implemented_rules = traceability.rule_id
missing = expected - implemented
```

`missing` 非空时 Gate D 必须 `INCOMPLETE/BLOCKED`，不能 PASSED。

规则按责任域归档：

- R01-R18：Git/前序 Scan/Repository/Function/Dependency；
- R19-R27：Covered Bridge/Physical Line/Block/Record Split；
- R28-R36：Import/Failure Classification/Multi-repo；
- R37-R50：Review/Reject/Inheritance Chain/Progress；
- R51-R60：Repository/Worktree/Lock/CURRENT/Read-set；
- R61-R70：Parser/Language/Token/Macro/Callee；
- R71-R83：Control Flow/Preprocessor/Hunk/Delete/Recovery/Header/Undo。

`contracts/inheritance_rules_v1.json` 是唯一机器权威；本 Markdown 和上游设计文档的规则章节由它生成或通过 SHA256 校验。若规则变更，先修改 canonical JSON、提升 contract version，再生成文档并重新执行联合审计。

---

# 22. 附录 B — 建议开发任务编号

为了支持 AI/多人高并发开发，可使用以下任务 ID：

## Gate A

```text
A-01 Legacy schema fixture
A-02 DB runtime fingerprint + separation guard
A-03 schema versioning + migration ledger
A-04 analysis migration
A-05 line migration
A-06 project-state migration
A-07 job migration
A-08 semantic snapshot v2
A-09 restored-production-backup rehearsal
A-10 Gate A evidence
```

## Gate B

```text
B-01 repository master + physical resource schema
B-02 scan repository identity upgrade
B-03 content-only analysis record schema
B-04 verified human analysis block schema
B-05 line link schema
B-06 backfill
B-07 canonical analysis write
B-08 shared-record split
B-09 canonical analysis read
B-10 old-table compatibility shim
B-11 parity/integrity audit
B-12 Gate B evidence
```

## Gate C

```text
C-01 publication service
C-02 remove implicit current switching
C-03 physical repository resource lock
C-04 monotonic atomic fencing
C-05 durable import job + immutable input artifact
C-06 checkpoint
C-07 import coordinator
C-08 worktree provider
C-09 read-set tracking
C-10 resume/recovery
C-11 atomic publish
C-12 Gate C evidence
```

## Gate D

```text
D-01 predecessor resolver
D-02 ancestry/fetch
D-03 line map
D-04 normalizer
D-05 parser adapter
D-06 function identity
D-07 verified block mapping + inheritance group
D-08 control context
D-09 preprocessor context
D-10 macro/constant dependency
D-11 direct callee dependency
D-12 covered bridge/delete chain
D-13 rejection policy
D-14 idempotent decision run/ledger/reason codes
D-15 engine orchestration
D-16 cache/batching
D-17 R01-R83 traceability
D-18 deterministic corpus
D-19 Gate D evidence
```

## Gate E

```text
E-01 frozen Target API Contract v1
E-02 inheritance query API
E-03 confirm/edit API
E-04 reject/undo API
E-05 progress ordinary/inherited/manual-draft conservation
E-06 code detail DTO
E-07 block review UI
E-08 filters/search
E-09 lazy-collapse integration
E-10 concurrency/stale revision UI
E-11 real HTTP acceptance
E-12 real-browser acceptance
E-13 performance evidence
E-14 Gate E evidence
```

## Gate F

```text
F-01 release identity
F-02 dual-environment validation
F-03 fresh inventory
F-04 candidate rehearsal
F-05 verified backup
F-06 freeze/drain
F-07 final migration
F-08 exact-SHA source review + read-only traffic-closed verification
F-09 cutover
F-10 rollback/data-safety-hold
F-11 >=48h acceptance window + exit criteria
F-12 skill drift audit
F-13 Gate F evidence/final decision
```

---



### Task root-owner 约束

所有任务在 issue/PR/task manifest 中必须增加：

```text
root_owner_skill
secondary_owner_skill（可空）
required_evidence_class
upstream_gate_dependencies
```

默认 root owner：

| Gate | Root owner |
|---|---|
| A | `fos-coverage-release-governance` |
| B | schema/migration=`fos-coverage-release-governance`；source canonical wiring=`fos-coverage-change-review` |
| C | runtime/job=`fos-coverage-runtime-reliability`；source refactor=`fos-coverage-change-review` |
| D | source implementation=`fos-coverage-change-review`；runtime correctness=`fos-coverage-runtime-reliability` |
| E | browser/performance=`fos-coverage-performance-ui`；source API contract=`fos-coverage-change-review` |
| F | `fos-coverage-release-governance`；总体编排=`fos-coverage-maintainer` |

# 23. 最终结论

> v1.2 已按实际审计清单关闭 P1-01～P1-26、P2-01～P2-34、Info-01～Info-04。任何后续实现若偏离本版新增的 HC-01～HC-12，必须重新进入联合设计审计。


本方案允许团队或 AI 以高并行、高生成速度一次性推进 Gate A～F，但把**数据正确性、单一权威、确定性继承、可恢复导入和零损失发布**作为不可跨越的工程约束。

最核心的实施顺序固定为：

```text
Gate A：证明旧库能无损进入 VNext
      ↓
Gate B：建立 Repository 与 Analysis 的长期 Canonical Owner
      ↓
Gate C：让 Scan Import 可持久、可恢复、可原子发布
      ↓
Gate D：在 R01-R83 下实现确定性自动继承
      ↓
Gate E：把继承状态完整交付给 API/UI/真实浏览器
      ↓
Gate F：用隔离 Candidate 完成迁移、验证、切换、回滚和 Skill 漂移闭环
```

最终目标不是“代码完成”，而是：

> **旧数据可无损承接、新 Scan 可安全生成、历史分析可在严格证据下自动继承、用户可清晰复核、服务可在故障后恢复、生产可安全切换并保留零损失回滚边界。**

---

# 24. 附录 C — Deterministic Inheritance Contract v1 完整规则文本

# 10. Deterministic Inheritance Contract v1（83 条正式规则）

以下规则全部是自动继承 Hard Gate。除明确标注为技术失败外，任何无法证明的条件都按普通“不继承”处理。

## 10.1 Git/前序 Scan/Repository Identity

**R01** Git 身份是自动继承必需证据；旧/新 Scan 需要可验证 commit，禁止数据库猜测语义等价。  
**R02** 只比较当前 Scan 与“立即前序合格 Scan”，不搜索更老历史。  
**R03** 前序 Scan 必须与当前 Scan 同 branch 且已成功完成。  
**R04** 历史分析内容可以携带，但当前 Scan 继承后必须是 `INHERITED_PENDING`，不能直接算已分析。  
**R05** 多行允许共享 AnalysisRecord，当前 Scan 通过 LineLink 引用。  
**R06** AnalysisRecord 内容可在当前模型中直接更新；部分行编辑必须按 R27 拆分。  
**R07** 不维护完整内容修改历史；权威内容只保留当前最终结论及必要 provenance/revision。  
**R08** 仅允许忽略非语义格式变化；identifier/variable rename 受 R66 限制。  
**R09** Analysis Block 继承映射必须 1:1。  
**R10** Analysis Block 是语义审查组织单位，但最终分析状态权威落到物理行。  
**R11** 多个旧 Block 合并成一个新 Block（N:1）不继承。  
**R12** 文件 identity 使用 repository-relative exact path；rename/move 不继承。  
**R13** Git ancestry 必须满足 `old==new` 或 `merge-base --is-ancestor old new`。  
**R14** 候选行所属函数 identity 必须匹配。  
**R15** 完整函数 identity 至少包含 repo-relative path + namespace/class scope + function name + parameter signature。  
**R16** 候选行实际依赖的直接宏/编译期常量必须检查变化。  
**R17** 候选行实际依赖的一层同仓库直接调用函数必须检查；不递归，不跨仓库。  
**R18** direct callee 的“等价”最终以 R68 的规范化完整函数体 exact 为准。  

## 10.2 覆盖桥接、物理行与 Block 映射

**R19** Covered bridge 允许：V4 未覆盖且有结论 R → V5 covered → V6 再未覆盖，只要代码身份链持续成立，V6 可恢复 R 为 `INHERITED_PENDING`；V5 不要求复核。  
**R20** 只有旧 Scan 中实际有分析关系的物理行可以继承；新出现/新未覆盖行不能凭 Block 结果自动获得结论。  
**R21** 物理行映射必须 1:1；1→many 或 many→1 格式映射不继承。  
**R22** 物理行映射必须唯一；歧义即不继承。  
**R23** 全局约束：一个 old physical line 最多映射到一个 new physical line。  
**R24** Block 1→N 复制/分叉不继承。  
**R25** 合法 1:1 Block 内允许“部分物理行继承”。  
**R26** 同一 Block 内不同物理行可以引用不同 AnalysisRecord。  
**R27** 共享 Record 的部分编辑必须 split：只修改的行改绑新 Record；只有全部当前引用行明确一起编辑时才允许更新原 Record。  

## 10.3 Import、失败分类与多仓库

**R28** `.info` 导入后自动执行继承计算，不需要额外人工启动。  
**R29** 继承过程发生技术失败时，整个新 Scan Import 回滚，旧 CURRENT 不变。  
**R30** 只有技术/完整性错误触发 Scan 回滚；普通不符合继承条件只让对应行 ordinary pending。  
**R31** 没有任何前序合格 Scan 属于正常情况：Scan 成功，无继承。  
**R32** 最近同 branch Scan 若不是 ancestor，不再向更老 Scan 搜索；当前 Scan 成功但无该继承链。  
**R33** 多 Repository 项目按 Repository 独立判定继承资格。  
**R34** 任一 Repository 发生技术失败，整个 Scan 回滚。  
**R35** 不追踪跨 Repository direct callee。  
**R36** 第一阶段不追踪普通 enum/struct/typedef/global variable 数据依赖；只检查候选行实际依赖的宏/编译期常量和同仓库 direct callee。  

## 10.4 人工分析、复核、拒绝与链路

**R37** 旧 Scan 的 manual draft 和 confirmed 内容均可作为内容来源，但新 Scan 一律进入 `INHERITED_PENDING`。  
**R38** 所有未人工确认的继承项在当前 Scan 都计入未分析/待分析。  
**R39** 人工复核默认支持 Block 批量确认，并允许逐行 opt-out。  
**R40** reviewer 可以编辑内容并确认；部分编辑遵守 R27。  
**R41** 下一 Scan 继承最近人工复核形成的当前有效结论。  
**R42** `INHERITED_PENDING` 未复核也可以继续传递到下一 Scan，但下一 Scan 仍为 `INHERITED_PENDING`。  
**R43** 用户可以直接复核最新 Scan，不要求逐个复核中间 Scan。  
**R44** 活跃“拒绝继承”会阻断继承链；后续 Scan 不允许回溯更老结论绕过拒绝。  
**R45** 拒绝事实按 Scan + commit + physical line + source relation 持久化；重算不得自动重新挂回旧 Record。  
**R46** 两个不同 Scan 即使 commit 相同，只要其他硬条件全部满足，仍允许继承。  
**R47** 外部 build 参数/环境（如编译命令注入 `-D`）不进入第一阶段依赖证明；只使用 Git 可读源码事实。  
**R48** 旧 Scan/旧关系不因失去 CURRENT 而删除；保留 identity、line relation、rejection 等继承所需事实。  
**R49** `INHERITED_PENDING` 全量计入 `pending_total`。  
**R50** Progress/UI 必须提供 `inherited_pending` 子统计、筛选和批量复核。  

## 10.5 Repository、任务串行、CURRENT 与一致性

**R51** 文件 path identity 是 repo-relative；服务器物理仓库目录迁移不改变 Repository/File identity。  
**R52** 一个 Project Repository 对应一个逻辑 Repository Master 和一份活动物理 Git repo；历史 commit 使用临时只读 worktree，不为每个 Scan clone 一份，也绝不切生产 working tree HEAD。  
**R53** 本地缺少历史 commit 时允许自动 `git fetch`；远端仍取不到必需 commit 属于 technical failure。  
**R54** 同一 repository_id 的 Import/Inheritance 串行；不同 repository_id 可以并行。  
**R55** 同 Repository 忙时立即拒绝新 Scan，不排队、不取消当前任务。  
**R56** Busy rejection 必须发生在创建 Scan/写 coverage 之前，不允许 DB 残留。  
**R57** 自动发布：全部技术处理成功后最终短事务原子更新 `current_scan_id`；无管理员发布确认环节。  
**R58** Import 期间旧 CURRENT 允许继续人工分析；继承 Job 必须记录实际依赖的旧 Analysis/Relation revision，并在 Publish 前重检。变化则候选失败/回滚。  
**R59** 一致性检查只锁定和比较本次真正读取/继承的数据，不因无关旧 Analysis 修改而失败。  
**R60** 不存在相似度分数；确定性硬条件是 AND，无法证明即不继承。  

## 10.6 解析、语言、Token、函数依赖

**R61** 源码可读但 parser 无法可靠解析完整函数 identity 时，只让受影响文件/行 ordinary pending；源码不可读取、DB/流程异常属于 technical failure。  
**R62** 当前 covered 的继承关系静默 `CARRIED_COVERED`，不要求人工复核；后续再 uncovered 时恢复为 `INHERITED_PENDING`。  
**R63** 人工确认继承时 reviewer/time 更新为当前 reviewer/当前时间；不伪装为旧分析人的新确认。  
**R64** 第一阶段正式自动继承只支持 C/C++；其他语言继续导入、展示，但不自动继承。  
**R65** Analysis Block 精确定义为用户保存分析时实际选择范围，AST 不得自动扩缩。  
**R66** variable/identifier rename 属于有效代码变化，受影响行不继承。  
**R67** 行规范化只忽略空格、Tab、缩进、行尾空白和注释；真实 Token（identifier/literal/operator/keyword/call）必须完全一致。  
**R68** direct same-repo callee 的完整函数体规范化后必须 exact；只忽略空白/缩进/注释，任何真实 Token 变化都阻断相关 caller candidate。  
**R69** macro/compile-time constant definition 规范化后必须 exact；value/expression/params/object-vs-function/constexpr/const-value/resolved definition 变化都阻断；无法唯一解析也不继承。  
**R70** Block 内其他与候选行无关的有效代码可以增删改，不要求整个 Block body exact；继续按物理行独立判定。  

## 10.7 Control Flow、Preprocessor、Git hunk 与删除链

**R71** 每条候选物理行的完整控制流上下文必须保持一致；相关 if/else/switch-case/for/while 等条件或分支变化阻断该行。  
**R72** 比较候选行到 function body 的完整 control-flow ancestor chain；Block 内其他不在祖先链上的分支变化不影响该行。  
**R73** 每个祖先 condition expression 使用 exact normalized token；不做逻辑等价推导。  
**R74** Git diff 是物理行映射主要证据；Git 明确 old delete + new add 时默认不继承，除 R75 的同 hunk 纯格式恢复。  
**R75** 只允许在同一 Git hunk 内，对仅因 whitespace/indent/comment 导致原始映射丢失的 normalized identical line 做唯一 1:1 恢复；禁止跨 hunk 搜索/相似度/猜测。  
**R76** Source-line deletion 永久断链：V4 有 R → V5 line deleted → V6 同文本 re-add，V6 视为新代码 ordinary pending；与 R19 covered bridge 区分。  
**R77** Scan Import/Inheritance 必须持久可恢复：重启从安全 checkpoint 继续，恢复前重新验证 Scan/repo/commit/worktree/lock fencing/read-set/expected CURRENT；不一致则 fail closed。  
**R78** C/C++ 支持扩展名：`.c .cc .cpp .cxx .h .hh .hpp .hxx`。Header 仅在 `.info` 实际覆盖、Git 源可读且函数/行/控制上下文可可靠解析时参与；解析不确定时对应行普通 pending。  
**R79** Git-visible preprocessor control 属于 line context：完整 `#if/#ifdef/#ifndef/#elif` ancestor chain 与 branch relationship 必须 exact normalized；外部 build `-D` 仍按 R47 不纳入。  
**R80** direct call 无法唯一静态解析（function pointer、virtual dispatch、复杂 templates、macro-wrapped call、ambiguous overload 等）时，受影响候选行不自动继承，Scan 正常继续。  
**R81** 宏/编译期常量/direct callee 依赖按“候选物理行实际依赖”检查：候选行自身 + 完整 control-flow ancestor + 完整 preprocessor ancestor；同 Block 其他无关依赖变化不阻断该行。  
**R82** 新 Scan 完成 `.info`、Git、继承、统计和一致性全部处理后自动原子切换 CURRENT；任一技术失败不切换。  
**R83** 当前 Scan 的拒绝继承，在尚未形成新的人工分析结果前允许人工显式撤销；撤销只恢复当前 Scan 的 `INHERITED_PENDING`，系统不得自动撤销；已经计算完成的后续 Scan 不做追溯重写。

---
