# FOS Coverage Tool 全量问题修改与验证总方案

> 基于 `FOS_Coverage_Performance_Compatibility_Audit_b07d7ee_20260822.md` 的 30 个审计 Issue 制定。本文件是后续开发、逐项关闭 Issue、复检和 Release/Gate 决策的执行基线；不重新定义上一轮问题，不把 Strong Risk 当成已实测瓶颈。

## 0. 文档控制

| 项目 | 内容 |
|---|---|
| Repository | `Chary-yu/fos_coverage_tool` |
| Audit Source Commit | `b07d7ee346a5c09169bc125145c0d5bf2547ed30` |
| Audit Source Report | `FOS_Coverage_Performance_Compatibility_Audit_b07d7ee_20260822.md` |
| Previous Production Baseline | `v10`；精确 SHA/生产 schema/真实 DB 副本仍需取得 |
| Plan Date | `2026-08-22` |
| Issues In Scope | `30` |
| Full Test Suite Planned | **NO**；仅逐修改点 targeted tests / benchmark / integration / browser / rehearsal |
| Primary Orchestrator | `fos-coverage-maintainer` |

### 0.1 Skill 职责

- `fos-coverage-change-review`：源码重构、算法复杂度、inheritance 语义、兼容 CLI、canonical owner。
- `fos-coverage-runtime-reliability`：Scan/Job/Derived State/DB pool/恢复/并发/指标。
- `fos-coverage-performance-ui`：API Payload、Code Detail、Lazy Collapse、DOM、cache、真实浏览器性能。
- `fos-coverage-release-governance`：Empty Target、v10 迁移、release identity、配置升级、历史报告、cutover/rollback。

## 1. 总体结论与实施原则

本轮不建议把 30 个问题做成一个大提交。推荐拆成 **19 个可独立验证/回滚的 Change Package**，按依赖推进。所有 P0/P1 的关闭必须同时满足“源码修复 + targeted correctness + 对应性能/兼容证据”；只有静态代码修改不能把 Issue 标为 Verified。

硬原则：

1. **正确性优先于性能。** AnalysisRecord/LineLink/Block/InheritanceGroup/CURRENT 权威边界不能因优化改变。
2. **旧库不原地升级为 VNext。** 正式合同维持 `Legacy Source(read-only) → Empty VNext Target → verified migration`。
3. **Candidate 可分阶段提交，但不可被误认为 CURRENT。** 最终 publication 必须保持短事务原子 CAS。
4. **Confirmed 直接修，Strong Risk 先测量。** Strong Risk 未形成 fixed workload 证据前，不做高复杂度架构改写。
5. **不跑全量单元测试。** 每个 Package 只运行相关 test files；需要系统级最终确认时记录 `需要后续 Full Verification`。
6. **一包一个主要根因族。** 每个 Package 应可单独 revert；不要同时重构 schema、API、UI 和 legacy owner。
7. **所有 performance evidence 绑定 exact SHA/workload/cache state。** 不比较不同数据集或冷/热缓存混合结果。

## 2. 实施阶段与依赖

### Phase 0：安全门禁与性能证据基础

先关闭 P0、修正 release artifact identity，并建立后续所有性能验证需要的指标。

推荐顺序：`OBS-001 → MIGRATION-001 → COMPAT-002`

### Phase 1：低风险确定性修复

先收敛明确 bug/低风险算法与数据库放大，为后续重构降低噪音。

推荐顺序：`API-002 → PERF-001 → DB-002 → DB-003 → COMPAT-003`

### Phase 2：Scan / Derived State / Job 主链

处理长事务、重复 I/O、stale job、进度全量回退和并发预算。

推荐顺序：`DB-001 → JOB-001 → JOB-002 → JOB-004 → JOB-003`

### Phase 3：继承与服务端规模化

按文件重构继承主链、流式 decision、Job/DB 热查询与连接池。

推荐顺序：`INHERIT-001 → INHERIT-003 → API-003 → DB-004 → RUNTIME-001 → INHERIT-002`

### Phase 4：API / Code Detail / 交互性能

修复 unbounded pending、继承精确查询、batch cap、cache lock/bytes。

推荐顺序：`API-001 → UI-001 → UI-002 → UI-004 → CACHE-001`

### Phase 5：生产迁移与历史兼容

完成大库迁移、v10→VNext 接管、历史 CLI/静态报告兼容和 DOM 扩展。

推荐顺序：`MIGRATION-002 → COMPAT-001 → COMPAT-004 → COMPAT-005 → UI-003`

### Phase 6：兼容层收敛与最终 Gate

逐能力退休第二业务实现，并执行 targeted + release rehearsal。

推荐顺序：`MAINT-001`

### 2.1 总依赖链

```text
OBS-001 ─┬─> PERF-001 ─> INHERIT-001 ─> INHERIT-003 ─> INHERIT-002
         ├─> DB-001 ─> API-001 ─> UI-001 / UI-003
         ├─> JOB-001 ─> JOB-002 / JOB-004 ─> MIGRATION-002 ─> COMPAT-001
         ├─> UI-004 ─> CACHE-001
         └─> API-003 ─> DB-004
MIGRATION-001 ────────────────────────────────────────┘
COMPAT-002 ─> COMPAT-003 ─> COMPAT-001
API-002 ─> UI-002 / COMPAT-005
COMPAT-001 ─> COMPAT-004
COMPAT-004 + COMPAT-005 + API-003 ─> MAINT-001
```

## 3. Change Package 规划

| Package | 内容 | Issue | 可独立回滚 |
|---|---|---|---|
| `PKG-00` | 观测指标契约（OBS-001） | `OBS-001` | YES |
| `PKG-01` | Empty Target 硬门禁（MIGRATION-001） | `MIGRATION-001` | YES |
| `PKG-02` | Release Artifact Identity（COMPAT-002） | `COMPAT-002` | YES |
| `PKG-03` | VNext DTO + Config Preflight（API-002/COMPAT-003） | `API-002`, `COMPAT-003` | YES |
| `PKG-04` | 低风险 CPU/SQL 快速优化（PERF-001/DB-002/DB-003） | `DB-002`, `DB-003`, `PERF-001` | YES |
| `PKG-05` | Derived State 原子增量刷新（DB-001） | `DB-001` | YES |
| `PKG-06` | Scan Import durability + artifact + job fence（JOB-001/002/004） | `JOB-001`, `JOB-002`, `JOB-004` | YES |
| `PKG-07` | Inheritance 文件级 work unit（INHERIT-001） | `INHERIT-001` | YES |
| `PKG-08` | 全局 worker budget（JOB-003） | `JOB-003` | YES |
| `PKG-09` | Inheritance 流式化（INHERIT-003） | `INHERIT-003` | YES |
| `PKG-10` | Dependency index 资源边界（INHERIT-002） | `INHERIT-002` | YES |
| `PKG-11` | Job/DB Pool 热路径（API-003/DB-004/RUNTIME-001） | `API-003`, `DB-004`, `RUNTIME-001` | YES |
| `PKG-12` | Progress pending 分页（API-001） | `API-001` | YES |
| `PKG-13` | Inheritance UI + Code Detail batch cap（UI-001/UI-002） | `UI-001`, `UI-002` | YES |
| `PKG-14` | Code Detail lock/cache budget（UI-004/CACHE-001） | `CACHE-001`, `UI-004` | YES |
| `PKG-15` | Progress DOM 有界化（UI-003） | `UI-003` | YES |
| `PKG-16` | Legacy→VNext 可恢复迁移与接管（MIGRATION-002/COMPAT-001） | `COMPAT-001`, `MIGRATION-002` | YES |
| `PKG-17` | Legacy CLI/历史报告兼容（COMPAT-004/005） | `COMPAT-004`, `COMPAT-005` | YES |
| `PKG-18` | Legacy owner 逐项退休（MAINT-001） | `MAINT-001` | YES |

建议每个 Package 完成后立即提交并记录 commit SHA、targeted test 命令、结果、benchmark/evidence path；不要等全部问题一起提交。

## 4. 全量 Issue 汇总与关闭策略

| Issue | Severity | Evidence | Owner | Action | Phase | Package | Dependencies | Close Gate |
|---|---|---|---|---|---|---|---|---|
| `MIGRATION-001` | **P0** | Confirmed | `fos-coverage-release-governance` | `FIX_BLOCKER` | Phase 0 | `PKG-01` | - | 空库与白名单 bootstrap 库通过；已有 project、未知表、legacy/vnext 混合、source=target alias 全部拒绝。 |
| `COMPAT-002` | P1 | Confirmed | `fos-coverage-release-governance` | `FIX_BLOCKER` | Phase 0 | `PKG-02` | - | 无 `.git` 正确 release artifact 可以启动并报告 exact audited SHA。 |
| `OBS-001` | P3 | Confirmed | `fos-coverage-runtime-reliability` | `FIX_FOUNDATION` | Phase 0 | `PKG-00` | - | 固定 workload 下 counters 与实际操作数量守恒。 |
| `PERF-001` | P1 | Confirmed | `fos-coverage-change-review` | `FIX` | Phase 1 | `PKG-04` | OBS-001 | 所有现有 fixture 的 function identity/line ownership 逐行完全一致。 |
| `API-002` | P2 | Confirmed | `fos-coverage-performance-ui` | `FIX` | Phase 1 | `PKG-03` | - | 真实 VNext server 上 Progress Details 能渲染当前页，上一页/下一页正常且 console 无 contract error。 |
| `COMPAT-003` | P2 | Strong Risk | `fos-coverage-release-governance` | `MEASURE_THEN_FIX` | Phase 1 | `PKG-03` | COMPAT-002 | 真实 v10 config 副本经过 preflight 后得到完整 diff 和候选 config。 |
| `DB-002` | P2 | Confirmed | `fos-coverage-change-review` | `FIX` | Phase 1 | `PKG-04` | - | 默认 ingest 路径不再执行写后 `list_lines(file_id)`。 |
| `DB-003` | P2 | Confirmed | `fos-coverage-change-review` | `FIX` | Phase 1 | `PKG-04` | - | SQLite 999 bind-limit 环境 1/500/5000 identity lookup 均成功。 |
| `DB-001` | P1 | Confirmed | `fos-coverage-runtime-reliability` | `FIX` | Phase 2 | `PKG-05` | OBS-001 | 每次成功保存返回后 `file_state_version == data_version`。 |
| `INHERIT-001` | P1 | Confirmed | `fos-coverage-change-review` | `FIX` | Phase 3 | `PKG-07` | OBS-001, PERF-001 | 同一输入下 old/new 实现的 decision、reason_code、mapping/function/control/preprocessor/dependency fingerprint 语义哈希 100% 一致。 |
| `JOB-001` | P1 | Confirmed | `fos-coverage-runtime-reliability` | `FIX` | Phase 2 | `PKG-06` | MIGRATION-001, OBS-001 | 在每个 phase 前/中/后 kill 后重启，恢复从最后 durable checkpoint 继续，不从 STAGED 全量重做。 |
| `JOB-002` | P1 | Confirmed | `fos-coverage-runtime-reliability` | `FIX` | Phase 2 | `PKG-06` | JOB-001, OBS-001 | 无中断正常路径对同一 staged artifact 只做一次完整 hash/read；恢复最多增加一次完整 verify。 |
| `JOB-003` | P2 | Strong Risk | `fos-coverage-runtime-reliability` | `MEASURE_THEN_FIX` | Phase 2 | `PKG-08` | OBS-001 | 任意时刻进程 active job 总数不得超过 global budget。 |
| `JOB-004` | P2 | Confirmed | `fos-coverage-runtime-reliability` | `FIX` | Phase 2 | `PKG-06` | JOB-001 | 提交 job 后推进 CURRENT/data_version，再释放 worker，过期 callback 不产生任何新业务写。 |
| `INHERIT-002` | P1 | Strong Risk | `fos-coverage-change-review` | `MEASURE_THEN_FIX` | Phase 3 | `PKG-10` | INHERIT-001, OBS-001 | 预算耗尽时只能增加 unresolved/NO_INHERIT，不能增加错误 INHERITED。 |
| `API-003` | P2 | Strong Risk | `fos-coverage-runtime-reliability` | `FIX_AFTER_MEASURE` | Phase 3 | `PKG-11` | OBS-001 | 10k/1M job rows 时 API payload/内存受 page size 约束。 |
| `DB-004` | P2 | Strong Risk | `fos-coverage-runtime-reliability` | `MEASURE_THEN_FIX` | Phase 3 | `PKG-11` | OBS-001, API-003 | EXPLAIN 显示热查询使用目标索引且 rows examined 明显下降。 |
| `INHERIT-003` | P2 | Strong Risk | `fos-coverage-change-review` | `FIX_AFTER_MEASURE` | Phase 3 | `PKG-09` | INHERIT-001, JOB-001, OBS-001 | 10k/100k/1M candidate fixture 中 peak resident Python rows 由 batch size 决定，而不是由总行数决定。 |
| `RUNTIME-001` | P2 | Strong Risk | `fos-coverage-runtime-reliability` | `MEASURE_THEN_FIX` | Phase 3 | `PKG-11` | OBS-001 | 任何网络 connect 都不在 pool 全局锁内执行。 |
| `API-001` | P1 | Confirmed | `fos-coverage-performance-ui` | `FIX` | Phase 4 | `PKG-12` | DB-001, OBS-001 | 1M pending lines 时首页 response bytes 仍受 page_size 上限约束，不随总行数线性增长。 |
| `CACHE-001` | P1 | Strong Risk | `fos-coverage-performance-ui` | `MEASURE_THEN_FIX` | Phase 4 | `PKG-14` | OBS-001, UI-004 | steady-state cache bytes 受配置预算约束；单 oversize 对象不会挤爆总预算。 |
| `UI-001` | P1 | Confirmed | `fos-coverage-performance-ui` | `FIX` | Phase 4 | `PKG-13` | API-001 | 目标 relation 位于旧列表第 501 条之后仍可精确命中。 |
| `UI-002` | P1 | Confirmed | `fos-coverage-performance-ui` | `FIX` | Phase 4 | `PKG-13` | API-002, OBS-001 | 1001 regions、>20k logical lines 时不存在超服务端 cap 的请求。 |
| `UI-004` | P2 | Confirmed | `fos-coverage-performance-ui` | `FIX` | Phase 4 | `PKG-14` | OBS-001 | 注入 100ms DB 延迟时不同 key 请求可以并行，不被全局锁串行。 |
| `COMPAT-001` | P1 | Confirmed | `fos-coverage-release-governance` | `CONTRACT_AND_MIGRATION` | Phase 5 | `PKG-16` | MIGRATION-001, MIGRATION-002, COMPAT-003 | 旧库直接绑定 VNext 时得到明确拒绝而非半启动。 |
| `MIGRATION-002` | P1 | Confirmed | `fos-coverage-release-governance` | `FIX_BLOCKER` | Phase 5 | `PKG-16` | MIGRATION-001, JOB-001, JOB-002, OBS-001 | 脱敏生产规模副本迁移 semantic hash/关键 counts/provenance 100% 守恒。 |
| `COMPAT-004` | P2 | Confirmed | `fos-coverage-change-review` | `DECIDE_THEN_FIX` | Phase 5 | `PKG-17` | COMPAT-001 | 旧命令不会绕开 VNext fixed predecessor/branch/identity/fencing。 |
| `COMPAT-005` | P2 | Strong Risk | `fos-coverage-release-governance` | `MEASURE_THEN_FIX` | Phase 5 | `PKG-17` | API-002, COMPAT-002 | 抽样历史报告在其声明支持路径中真实浏览器可用。 |
| `UI-003` | P2 | Strong Risk | `fos-coverage-performance-ui` | `MEASURE_THEN_FIX` | Phase 5 | `PKG-15` | API-001, OBS-001 | 10k 文件项目首次 DOM 节点数受 page/window 上限约束。 |
| `MAINT-001` | P2 | Strong Risk | `fos-coverage-change-review` | `INCREMENTAL_REFACTOR` | Phase 6 | `PKG-18` | COMPAT-004, COMPAT-005, API-003 | 每个持久化业务语义只有一个 authoritative writer/owner。 |

## 5. 通用开发与验证流程

每个 Issue/Package 固定使用以下流程：

1. **重新 pin 修改前 SHA。** 记录 `git rev-parse HEAD`、branch、working tree status。若 HEAD 已不是审计 SHA，先确认 Issue 在新 HEAD 仍存在。
2. **建立修改前 targeted baseline。** 仅跑该 Package 相关测试/benchmark，保存命令、exit code、关键 metrics。
3. **最小修改。** 优先局部数据结构/批处理/合同修复；不顺手重构无关模块。
4. **静态与单元验证。** 只执行受影响 test files；涉及 SQL 必须同时覆盖 SQLite/MariaDB 能力边界。
5. **专项集成。** 涉及 Scan/Job 用 kill/restart/fencing；涉及 browser 用真实 VNext HTTP + Playwright；涉及 migration 用 disposable target。
6. **A/B 或 fixed workload。** 性能类 Issue 必须比较 before/after 相同 workload、相同 cache state。
7. **兼容与 rollback。** previous 配置/DB/制品不修改；验证 candidate 失败能独立回退。
8. **关闭 Issue。** 只有所有 Acceptance Criteria 满足后，状态从 Open/Measurement Needed → Verified；否则继续保留。

### 5.1 通用禁止事项

- 不允许把“减少代码行数”当成性能修复证据。
- 不允许为了降低 CPU/RSS 放宽 inheritance 规则造成错误继承。
- 不允许通过把 derived state 变 stale 来降低 DB 查询。
- 不允许把旧生产 DB 直接原地执行 VNext DDL。
- 不允许为了旧静态页面永久保留第二套业务 writer。
- 不允许用 mock browser 代替真实浏览器验收 DOM/network/lifecycle。
- 不允许执行全量 test suite；若最终 Release Gate 要求则记录“需要后续 Full Verification”。

## 6. 逐 Issue 详细修改与验证方案

### MIGRATION-001：未执行 Empty VNext Target 硬门禁

| 字段 | 内容 |
|---|---|
| Severity | **P0** |
| Evidence | Confirmed |
| Owner Skill | `fos-coverage-release-governance` |
| Layer | Migration / Data Safety / CURRENT |
| File | `scripts/upgrade/migration_runner.py`；`scripts/upgrade/vnext_schema.sql` |
| Function/Class | `validate_migration_database_separation`、`apply_schema`、`migrate_legacy` |
| Code Location | preflight 只拒绝 source/target 同一身份；未枚举并拒绝目标业务表/数据 |
| Root Cause | “数据库身份分离”被当作“目标为空”的替代条件；语义一致性校验发生在迁移事务提交之后。 |
| Current Impact | 错误配置可把 legacy snapshot 合并进已有 Candidate，更新同名 project 的 CURRENT/file state，写入迁移 provenance；最终 mismatch 不能自动还原提交前状态。 |
| Scale Impact | 与规模无关，一次错误执行即可污染目标；大库只会扩大恢复成本。 |
| Compatibility | 违反 Release Governance 的 `Legacy Source DB -> Empty VNext Target DB` 硬合同；也破坏可证明 rollback。 |
| Action Type | `FIX_BLOCKER` |
| Phase / Package | `Phase 0` / `PKG-01` |
| Dependencies | 无 |

#### 修改目标

关闭 `MIGRATION-001` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：在任何 DDL/数据写入前，连接目标并检查。仅允许完全空库，或只含经过精确定义的 bootstrap ledger/meta 且无业务行。发现任意未知表/业务行立即 fail closed。记录 target fingerprint、emptiness manifest 和 migration ID。**

#### 详细修改步骤

1. 新增独立 `assert_empty_vnext_target()`，必须在 `apply_schema()` 任何业务 DDL 之前执行。
2. 目标判定允许两种状态：完全空库；或仅存在白名单 bootstrap ledger/meta 且业务表 0 行。白名单写死在 migration contract，不用前缀模糊匹配。
3. 检查未知表、VNext business table 任意行、旧 legacy business table 混入、已有 CURRENT/project/scan；任一命中立即 fail closed。
4. 把 target runtime fingerprint、database name、table inventory hash、emptiness result、migration_id 写入 evidence/ledger。
5. `run_upgrade.py`、直接 `migration_runner.py`、自动化脚本都必须复用同一 preflight，不允许绕过。
6. 拒绝路径必须保证首个业务 DDL/DML 尚未执行；增加 transaction/SQL spy test。

#### 数据、兼容与回滚约束

- **原兼容结论：** 违反 Release Governance 的 `Legacy Source DB -> Empty VNext Target DB` 硬合同；也破坏可证明 rollback。
- **Migration：** YES：修复迁移工具本身；不需要修改生产旧源库。已污染 Candidate 必须废弃并从空目标重建。
- **Rollback：** 仅新增前置门禁，可独立 revert；不要为了兼容而允许非空 target。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/vnext/test_migration_runner.py -q`
- `python -m pytest tests/vnext/test_legacy_migration_contract.py -q`
- `python -m pytest tests/release/test_release_governance_tools.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/upgrade/schema_preflight.py`
- `scripts/diagnostics/mysql_vnext_integration.py`

#### 专项验证设计

审计报告原始验证要求：准备空库、仅 meta 库、已有 project 库、未知表库、source=target alias 五类 fixture；只有合规空目标通过，且拒绝发生在首个业务 DDL/DML 前。

1. **验收门槛 1：** 空库与白名单 bootstrap 库通过；已有 project、未知表、legacy/vnext 混合、source=target alias 全部拒绝。
2. **验收门槛 2：** 拒绝发生在首个 business DDL/DML 前。
3. **验收门槛 3：** preflight 结果进入可审计 evidence，且第二次执行幂等。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### COMPAT-002：Release Identity 对 manifest 与本地 Git HEAD 的依赖不兼容旧部署方式

| 字段 | 内容 |
|---|---|
| Severity | P1 |
| Evidence | Confirmed |
| Owner Skill | `fos-coverage-release-governance` |
| Layer | Runtime / Packaging / Release Identity |
| File | `app/release_identity.py`；`app/bootstrap.py` |
| Function/Class | `_get_git_commit_sha`、`get_current_release_identity`、runtime bootstrap |
| Code Location | manifest 缺失直接抛错；expected identity 每次通过 `git rev-parse HEAD` 重新计算 |
| Root Cause | 把 build-time source identity 与 runtime filesystem Git repository 混为一个权威。 |
| Current Impact | 传统“复制代码目录/ZIP 到服务器”部署、精简 release 包和 rollback 目录可能无法启动。 |
| Scale Impact | 非规模型；会直接造成服务不可用。 |
| Compatibility | 改变上一版本可能存在的直接启动方式；也影响可重复 rollback。 |
| Action Type | `FIX_BLOCKER` |
| Phase / Package | `Phase 0` / `PKG-02` |
| Dependencies | 无 |

#### 修改目标

关闭 `COMPAT-002` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：构建阶段注入 immutable commit SHA、asset hash、schema version；运行时只校验 manifest 与实际资产 hash/允许的 release target，不要求 `.git`。构建系统另行证明 manifest 来源。**

#### 详细修改步骤

1. 把 `release_manifest.json` 定义为构建产物而不是仓库必须跟踪文件；`scripts/release/build_release.py` 负责写入 exact commit SHA、asset hash、schema version、build id。
2. runtime `get_current_release_identity()` 不再用本地 `.git` 作为唯一 expected SHA；无 `.git` 制品以 manifest 中 build-provenance + 实际 asset hash 验证。
3. 源码 checkout/dev 模式可以额外用 Git HEAD 检查，但 production artifact mode 不依赖 `.git`。
4. manifest 缺失、asset drift、声明 SHA 与目标 release 不符继续 fail closed，不能为了兼容自动重建 manifest。
5. previous/candidate 两套目录各自携带 immutable manifest，cutover controller 校验 exact target。

#### 数据、兼容与回滚约束

- **原兼容结论：** 改变上一版本可能存在的直接启动方式；也影响可重复 rollback。
- **Migration：** 无数据库迁移；需要 release packaging 改造。
- **Rollback：** previous release 保留其 manifest/assets；仅切路径/服务，不需数据库修改。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/release/test_upgrade_manifest.py -q`
- `python -m pytest tests/release/test_release_governance_tools.py -q`
- `python -m pytest tests/release/test_release_readiness.py -q`
- `python -m pytest tests/vnext/test_runtime_config.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/release/build_release.py`
- `scripts/diagnostics/release_readiness.py`

#### 专项验证设计

审计报告原始验证要求：源码 checkout、无 `.git` release ZIP、资产被改动、manifest 缺失、SHA 不符五种场景；只有正确制品启动。

1. **验收门槛 1：** 无 `.git` 正确 release artifact 可以启动并报告 exact audited SHA。
2. **验收门槛 2：** manifest missing、asset tamper、target SHA mismatch 均拒绝启动。
3. **验收门槛 3：** runtime 不会自动生成/覆盖 manifest。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### OBS-001：关键资源指标不足，无法建立生产性能 Gate

| 字段 | 内容 |
|---|---|
| Severity | P3 |
| Evidence | Confirmed |
| Owner Skill | `fos-coverage-runtime-reliability` |
| Layer | Observability |
| File | `app/db/connection_pool.py`；`app/api/application.py`；Code Detail/Job metrics |
| Function/Class | pool `metrics`、runtime `/metrics`、cache/job telemetry |
| Code Location | pool `waiters` 固定为 0；缺少 transaction/phase/query/cache byte 维度 |
| Root Cause | 指标按组件功能增加，没有统一性能 evidence contract。 |
| Current Impact | 故障时难区分 DB pool、Git、parser、Sidecar、JSON 或 DOM；源码风险无法快速在生产验证。 |
| Scale Impact | 数据规模扩大后，缺少趋势和阈值会延迟发现非线性退化。 |
| Compatibility | 不影响业务语义；注意指标不得暴露路径、代码或凭据。 |
| Action Type | `FIX_FOUNDATION` |
| Phase / Package | `Phase 0` / `PKG-00` |
| Dependencies | 无 |

#### 修改目标

关闭 `OBS-001` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：增加真实 waiter/connecting gauge、request query count/time、Scan phase time/RSS、Git subprocess count、cache bytes/evictions、Payload/DOM telemetry，并绑定 release/scan/workload identity。**

#### 详细修改步骤

1. 定义统一 performance evidence fields：release/build SHA、project/scan、operation、elapsed、CPU/RSS high-water、DB queries/rows/time、Git subprocess、bytes read/written、cache bytes/hits/evictions、payload bytes。
2. 连接池真实维护 waiters/connecting；不再固定 `waiters=0`。
3. Scan Import 每 phase 记录 duration/rows/RSS；Inheritance 记录 file/relation/Git/parser/SQL counters。
4. Code Detail 记录 Sidecar decode/cache bytes/query count；API 记录 response bytes；浏览器 diagnostic 记录 request count/DOM/long tasks/heap。
5. 指标不得含源码内容、密码、绝对敏感路径；高基数字段使用 stable hash/有限 label。
6. 先在 benchmark/test 环境验证观测开销，再默认开启低成本 counters。

#### 数据、兼容与回滚约束

- **原兼容结论：** 不影响业务语义；注意指标不得暴露路径、代码或凭据。
- **Migration：** 无。
- **Rollback：** metrics 可独立关闭，不改变业务数据；保留核心 release identity 字段。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/release/test_performance_ab.py -q`
- `python -m pytest tests/vnext/test_vnext_runtime.py -q`
- `python -m pytest test_concurrency.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/performance_evidence_audit.py`
- `scripts/diagnostics/release_performance_ab.js`

#### 专项验证设计

审计报告原始验证要求：固定 workload 下核对指标守恒；确认关闭指标与开启指标的开销可接受。

1. **验收门槛 1：** 固定 workload 下 counters 与实际操作数量守恒。
2. **验收门槛 2：** 启用低成本 metrics 不改变业务结果；观测开销在基准中记录且可接受。
3. **验收门槛 3：** 所有后续性能 Issue 的验证报告能绑定 exact release/scan/workload identity。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### PERF-001：LCOV 行到函数范围映射为 O(lines × functions)

| 字段 | 内容 |
|---|---|
| Severity | P1 |
| Evidence | Confirmed |
| Owner Skill | `fos-coverage-change-review` |
| Layer | Python / Scan Import |
| File | `app/inject/service.py` |
| Function/Class | `_function_for_line`、`build_files` |
| Code Location | 遍历 coverage lines 时，对每个 line_number 再线性遍历 function ranges |
| Root Cause | 函数范围虽有顺序，但查找未利用单调递增的 line_number，也没有区间索引。 |
| Current Impact | 解析/构建阶段 CPU 消耗随代码行和函数数共同增长。 |
| Scale Impact | 最坏 `O(L×F)`；例如 100k 行、10k 函数会产生数量级过高的 Python 比较。 |
| Compatibility | 优化只允许改变查找方式，不得改变边界行、嵌套/重叠异常时的保守处理。 |
| Action Type | `FIX` |
| Phase / Package | `Phase 1` / `PKG-04` |
| Dependencies | `OBS-001` |

#### 修改目标

关闭 `PERF-001` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：按 start/end 排序，用 sweep pointer 随 line_number 单调推进，复杂度 `O(L+F)`。**

#### 详细修改步骤

1. 在 `app/inject/service.py` 中将函数 ranges 预先按 `(start_line, end_line, identity)` 排序并验证基本合法性。
2. 对按 line_number 递增的 LCOV lines 使用 sweep pointer；指针只前进不回退。
3. 若函数 range 存在重叠、乱序或一个 physical line 命中多个候选，保留当前保守语义：明确 ambiguous，不任意选择。
4. 保留旧 `_function_for_line()` 作为测试 oracle，直到所有 fixture 新旧输出一致后再退休。
5. 增加微基准 fixture：1k/10k/100k lines × 不同函数密度，记录 comparison count 与 wall time。

#### 数据、兼容与回滚约束

- **原兼容结论：** 优化只允许改变查找方式，不得改变边界行、嵌套/重叠异常时的保守处理。
- **Migration：** 无。
- **Rollback：** 单文件级算法开关回退到旧线性查找，无 schema/data 影响。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/incremental/test_line_ownership_and_lcov_ranges.py -q`
- `python -m pytest tests/incremental/test_phase5_inject_path.py -q`
- `python -m pytest test_enhance_coverage.py -q`
- `python -m pytest test_incremental_coverage.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/perf_benchmark.py`

#### 专项验证设计

审计报告原始验证要求：生成 1k/10k/100k 行与不同函数密度 fixture；测 CPU，并逐行比较 old/new function identity。

1. **验收门槛 1：** 所有现有 fixture 的 function identity/line ownership 逐行完全一致。
2. **验收门槛 2：** 正常有序输入的比较次数接近 O(L+F)，不再出现每行从头扫描所有函数。
3. **验收门槛 3：** 大文件 benchmark 不得出现 CPU 回退；异常重叠输入继续 fail closed。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### API-002：Progress Details 的响应 envelope 前后端不一致

| 字段 | 内容 |
|---|---|
| Severity | P2 |
| Evidence | Confirmed |
| Owner Skill | `fos-coverage-performance-ui` |
| Layer | API / Frontend / Behavior Compatibility |
| File | `app/api/application.py`；`web/assets/js/coverage_progress.js` |
| Function/Class | `VNextApplication.progress_details`、`loadFileDetails`、`renderDetailTable` |
| Code Location | API 返回顶层 `{rows,page,...}`；JS 调用 `renderDetailTable(payload.data |
| Root Cause | API envelope 迁移没有由单一 contract test 约束所有页面。 |
| Current Impact | 请求可成功，但详情表接收空对象，用户看到空页或 0 页。 |
| Scale Impact | 非规模型性能问题，但会诱发重复点击/请求并掩盖真实数据。 |
| Compatibility | 属于旧/新 envelope 行为不兼容。 |
| Action Type | `FIX` |
| Phase / Package | `Phase 1` / `PKG-03` |
| Dependencies | 无 |

#### 修改目标

关闭 `API-002` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：canonical JS 直接消费顶层 payload。** 同时建立真实 VNext HTTP + canonical asset contract test。

#### 详细修改步骤

1. 确定 VNext canonical contract 为顶层 DTO；`coverage_progress.js::loadFileDetails()` 改为 `renderDetailTable(payload || {})`。
2. 如历史静态资源仍需旧 wrapper，在 compatibility adapter 层短期包一层 `data`，canonical asset 不再依赖。
3. 把 `docs/api_contract.*` 更新为唯一合同。
4. 新增真实 VNext HTTP fixture + canonical asset 测试，禁止只 mock legacy envelope。

#### 数据、兼容与回滚约束

- **原兼容结论：** 属于旧/新 envelope 行为不兼容。
- **Migration：** 无。
- **Rollback：** 仅前端消费层修改，可独立 revert；若有兼容 wrapper 继续保留。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/vnext/test_registry_and_api_contract.py -q`
- `python -m pytest tests/vnext/test_vnext_runtime.py -q`
- `python -m pytest tests/browser/vnext_http_integration.spec.js -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/frontend_vnext_api_contract_audit.py`

#### 专项验证设计

审计报告原始验证要求：启动 exact-SHA VNext server，浏览器打开详情，断言 200、行数、分页、上一/下一页以及无 console error。

1. **验收门槛 1：** 真实 VNext server 上 Progress Details 能渲染当前页，上一页/下一页正常且 console 无 contract error。
2. **验收门槛 2：** canonical JS 不再读取不存在的 `payload.data`。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### COMPAT-003：生产配置新增强制字段，旧配置直接使用会 fail closed

| 字段 | 内容 |
|---|---|
| Severity | P2 |
| Evidence | Strong Risk |
| Owner Skill | `fos-coverage-release-governance` |
| Layer | Configuration Compatibility |
| File | `app/config/runtime_config.py`；`config/coverage_config.staging.example.json` |
| Function/Class | `validate_production_config`、path normalization |
| Code Location | `COVERAGE_ENV=production` 时要求 auth/trusted proxies、六个 lifecycle commands、release endpoints、previous_release |
| Root Cause | 采用严格 fail-closed，却缺少版本化 config schema 与 upgrader。 |
| Current Impact | 是否命中取决于 v10 真实配置；本轮未取得配置文件，不能确认已经阻塞。 |
| Scale Impact | 非规模型；部署切换时一次性暴露。 |
| Compatibility | 属于明确的配置兼容风险。 |
| Action Type | `MEASURE_THEN_FIX` |
| Phase / Package | `Phase 1` / `PKG-03` |
| Dependencies | `COMPAT-002` |

#### 修改目标

关闭 `COMPAT-003` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：增加 `config preflight/upgrade` 命令，读取旧配置，输出差异、建议值和新文件；生产启动仍保持严格。支持旧 key alias 一次 release 并告警。**

#### 详细修改步骤

1. 为配置增加显式 schema/version 字段与 `config preflight/upgrade` 工具。
2. preflight 读取旧 v10 config，分类：可自动继承（DB host/path/port 等）、需人工填写（auth proxy、release endpoints、lifecycle commands、secrets）。
3. 工具输出新文件到独立路径，不原地改旧配置；所有推断值标记 provenance。
4. 生产 startup 继续严格 fail closed，不允许用默认空值掩盖缺失安全字段。
5. 支持已知旧 key alias 一个受控 release，并输出 deprecation warning。

#### 数据、兼容与回滚约束

- **原兼容结论：** 属于明确的配置兼容风险。
- **Migration：** 配置迁移 YES；数据库 NO。
- **Rollback：** 直接继续使用 previous config + previous release；候选 config 是新增文件。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/vnext/test_runtime_config.py -q`
- `python -m pytest tests/release/test_release_governance_tools.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/configured_runtime_audit.py`

#### 专项验证设计

审计报告原始验证要求：用真实 v10 config 副本运行 preflight；断言新旧路径、DB、ports、sidecar/registry、auth 和 lifecycle 命令完整。

1. **验收门槛 1：** 真实 v10 config 副本经过 preflight 后得到完整 diff 和候选 config。
2. **验收门槛 2：** 任何安全/生命周期必填项缺失时仍不能 production 启动。
3. **验收门槛 3：** 旧配置文件保持字节不变，可供 rollback 使用。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### DB-002：批量 upsert 后执行调用者未使用的全文件 reread

| 字段 | 内容 |
|---|---|
| Severity | P2 |
| Evidence | Confirmed |
| Owner Skill | `fos-coverage-change-review` |
| Layer | DB / Python |
| File | `app/db/repositories/line_index_repository.py`；`app/services/project_service.py` |
| Function/Class | `LineIndexRepository.upsert_lines`、`ProjectService._ingest_files` |
| Code Location | `executemany` 后 `return self.list_lines(connection, file_id)`；调用点不消费返回值 |
| Root Cause | Repository API 同时承担“写入”和“返回完整新状态”，但当前 ingest 调用只需要写入成功。 |
| Current Impact | 每导入一个文件多一次全量查询、传输和 Python dict/list 分配。 |
| Scale Impact | 总额外读取与本次所有文件行数同量级；大文件数时增加 DB round trip。 |
| Compatibility | 无外部行为依赖；需确认其他调用者是否使用返回行。 |
| Action Type | `FIX` |
| Phase / Package | `Phase 1` / `PKG-04` |
| Dependencies | 无 |

#### 修改目标

关闭 `DB-002` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：增加 `return_rows=False`，默认写入路径只返回 inserted/updated count。**

#### 详细修改步骤

1. 给 `LineIndexRepository.upsert_lines()` 增加显式返回策略，写入主路径默认 `return_rows=False`。
2. 调用者需要 rows 的少数路径显式 opt-in；禁止为了保持旧返回签名无条件 `list_lines(file_id)`。
3. 返回最小写入统计：attempted/inserted/updated（若底层不能准确区分则只返回 affected/count）。
4. 针对 Scan Import 统计 SQL 次数，确保取消一次全文件 reread。

#### 数据、兼容与回滚约束

- **原兼容结论：** 无外部行为依赖；需确认其他调用者是否使用返回行。
- **Migration：** 无。
- **Rollback：** 恢复旧 return_rows 默认值即可，无 schema 影响。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/vnext/test_scan_import_lifecycle.py -q`
- `python -m pytest tests/incremental/test_phase5_inject_path.py -q`

#### 专项验证设计

审计报告原始验证要求：针对 import fixture 统计 SQL 次数和 fetched rows；确保文件/行数量、hash、状态完全一致。

1. **验收门槛 1：** 默认 ingest 路径不再执行写后 `list_lines(file_id)`。
2. **验收门槛 2：** files/lines identity、hash、coverage_state 与旧实现完全一致。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### DB-003：大 OR 条件与 bind 参数数量越过 SQLite 安全边界

| 字段 | 内容 |
|---|---|
| Severity | P2 |
| Evidence | Confirmed |
| Owner Skill | `fos-coverage-change-review` |
| Layer | DB / Compatibility / Query Planning |
| File | `app/db/repositories/line_index_repository.py`；`app/db/repositories/project_repository.py`；`app/db/repositories/analysis_domain_repository.py` |
| Function/Class | `get_by_file_numbers`、file identity lookup、range read methods |
| Code Location | 单批 500 pair 的 OR 组，以及包含 scan_id/其他参数的查询构造 |
| Root Cause | chunk 上限按“业务 pair 数”设置，未按不同数据库的实际 bind-variable 上限和 SQL planner 特性设置。 |
| Current Impact | SQLite 常见默认 999 variable limit 下，大批次测试/工具路径可直接报错；MySQL/MariaDB 虽允许更多参数，但长 OR 解析和执行计划成本较高。 |
| Scale Impact | 批次越大，SQL 文本、解析成本、optimizer work 与网络参数编码同步增长。 |
| Compatibility | 影响当前声明支持的 SQLite 测试/迁移路径；MySQL 方案不能反向破坏 SQLite。 |
| Action Type | `FIX` |
| Phase / Package | `Phase 1` / `PKG-04` |
| Dependencies | 无 |

#### 修改目标

关闭 `DB-003` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：建立 database capability abstraction。** SQLite pair chunk 控制在安全上限内；MySQL/MariaDB 使用临时表或有界 derived table 后 JOIN，避免巨大 OR。

#### 详细修改步骤

1. 抽象 DB capability：SQLite bind limit、MariaDB/MySQL 临时表/derived-table 能力，不再共享固定 500-pair OR 策略。
2. SQLite pair 查询把每批最大 bind 数控制在可配置且低于 999 的安全边界，并考虑固定参数占用。
3. MySQL/MariaDB 对大 identity set 优先使用有界临时表/批量 insert + join，或更小的 chunked IN/OR；禁止生成超长单 SQL。
4. 所有 chunk 合并结果必须保持确定性、去重和输入 identity 对应关系。
5. 加入 MariaDB 5.5 compatibility fixture。

#### 数据、兼容与回滚约束

- **原兼容结论：** 影响当前声明支持的 SQLite 测试/迁移路径；MySQL 方案不能反向破坏 SQLite。
- **Migration：** 无。
- **Rollback：** 按数据库 backend 回退旧策略；无业务数据迁移。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/database/test_mariadb_compatibility_contract.py -q`
- `python -m pytest tests/database/test_phase0_baseline.py -q`
- `python -m pytest tests/vnext/test_vnext_runtime.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/mysql_vnext_integration.py`

#### 专项验证设计

审计报告原始验证要求：SQLite 999 limit、MariaDB 5.5 和当前 MySQL 版本分别运行 1/500/5000 identity lookup；比较结果守恒、EXPLAIN、SQL 时间。

1. **验收门槛 1：** SQLite 999 bind-limit 环境 1/500/5000 identity lookup 均成功。
2. **验收门槛 2：** MariaDB 5.5 与当前 MySQL 结果集合完全一致。
3. **验收门槛 3：** 单条 SQL bind 参数永不超过 capability limit。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### DB-001：分析保存后 Derived State 失效，进展查询反复全 Scan 回退

| 字段 | 内容 |
|---|---|
| Severity | P1 |
| Evidence | Confirmed |
| Owner Skill | `fos-coverage-runtime-reliability` |
| Layer | DB / Service / API |
| File | `app/services/analysis_service.py`；`app/db/repositories/project_state_repository.py`；`app/services/progress_service.py`；`app/db/repositories/file_state_repository.py` |
| Function/Class | analysis save path、`advance`、`summary`、`scan_summary_from_facts` |
| Code Location | save 后 `data_version + 1, file_state_version=0`；summary 在版本不等时执行跨 Scan JOIN/SUM |
| Root Cause | freshness 只有项目级版本，没有受影响文件的同步维护策略；正确性回退路径被暴露为常态路径。 |
| Current Impact | 人工连续保存、多个浏览器页轮询时，数据库会反复执行相同全量 JOIN/COUNT/SUM。 |
| Scale Impact | 单次查询成本随 Scan 行数增长，请求次数再线性乘法；可能形成典型 request amplification。 |
| Compatibility | 不能返回 stale 数字，也不能把 file_state_version 提前标 ready。 |
| Action Type | `FIX` |
| Phase / Package | `Phase 2` / `PKG-05` |
| Dependencies | `OBS-001` |

#### 修改目标

关闭 `DB-001` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：在分析保存事务中重建所有受影响 file_id 的 file state；待全部成功后把项目 file_state_version 原子推进到新 data_version。** 多文件 block 必须一次处理完整集合。

#### 详细修改步骤

1. 分析保存前计算所有受影响 `file_id` 集合，包括 multi-line block 与 relation/link 变更覆盖的文件。
2. 在同一 analysis write transaction 中先写 authoritative AnalysisRecord/LineLink，再仅对受影响 file 调用增量 `rebuild_file`。
3. 所有受影响文件状态成功后，才把 `file_state_version` 原子推进到新的 `data_version`；任一步失败整体回滚。
4. 如果一次保存跨多个文件，必须一次性处理完整集合，禁止部分 ready。
5. `ProgressService.summary()` 仅在真正异常/迁移状态下回退 authoritative full Scan 聚合；增加 fallback counter 和 reason。

#### 数据、兼容与回滚约束

- **原兼容结论：** 不能返回 stale 数字，也不能把 file_state_version 提前标 ready。
- **Migration：** 无；可增加 dirty-file queue/表时需迁移。
- **Rollback：** 可通过配置恢复异步/全量 rebuild，但不允许返回 stale derived state；authoritative facts 不受影响。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/progress/test_phase4_progress.py -q`
- `python -m pytest tests/database/test_file_state_transaction.py -q`
- `python -m pytest tests/vnext/test_analysis_domain.py -q`
- `python -m pytest tests/vnext/test_vnext_runtime.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/perf_benchmark.py`

#### 专项验证设计

审计报告原始验证要求：10k/1M 行 Scan 下连续保存 100 次并轮询 progress；统计每次保存/查询 SQL 行扫描、p95、版本守恒和 pending conservation。

1. **验收门槛 1：** 每次成功保存返回后 `file_state_version == data_version`。
2. **验收门槛 2：** 随后 progress 请求走 `coverage_file_state` 聚合，不触发 full Scan facts fallback。
3. **验收门槛 3：** pending conservation 始终 PASSED，失败事务不留下半更新 derived state。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### INHERIT-001：继承主循环按分析关系重复 Git、Diff、解析和 SQL

| 字段 | 内容 |
|---|---|
| Severity | P1 |
| Evidence | Confirmed |
| Owner Skill | `fos-coverage-change-review` |
| Layer | Git / Analyzer / DB / Service |
| File | `app/inheritance/engine.py`；`app/inheritance/git_snapshot.py`；`app/inheritance/line_map.py`；`app/inheritance/cpp_parser.py` |
| Function/Class | `InheritanceEngine.run`、`_snapshot_for_relation`、`_repository_snapshot`、`_write_decision` |
| Code Location | `for relation in source_relations` 主循环及其内部 snapshot/mapping/parser/decision 路径 |
| Root Cause | 业务处理按“分析行关系”组织，昂贵资源却天然属于“Repository + old/new Commit + file path”粒度；缺少先分组、后复用的 file work unit。 |
| Current Impact | 同一文件有 N 条已分析关系时，相同 Git/解析工作可重复 N 次；Git subprocess、磁盘读取、tokenization 和 DB round trip 明显放大。 |
| Scale Impact | 近似 `O(R × (GitFileRead + Diff + ParseFile + SQL))`，R 为 predecessor active relations。大文件、多分析行、大提交会共同放大。 |
| Compatibility | 任何缓存/批处理都必须保持固定 predecessor、不跨分支、删除即断链、Block 唯一匹配以及逐行 decision evidence。 |
| Action Type | `FIX` |
| Phase / Package | `Phase 3` / `PKG-07` |
| Dependencies | `OBS-001`, `PERF-001` |

#### 修改目标

关闭 `INHERIT-001` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：按 repository/file 分组。** 每个文件只创建一次 provider，预取两侧 snapshot，一次读取 old/new，一次 diff/mapping，一次解析；随后在内存中处理该文件的关系，并批量查询/写入 decision、record/link。

#### 详细修改步骤

1. 在 `InheritanceEngine.run()` 进入 relation 主循环前，按 `(repository_name, file_path)` 对 predecessor active relations 分组；保持原有排序以确保审计结果确定性。
2. 新增文件级 work context，例如 `InheritanceFileContext`：一次解析 candidate/predecessor repository snapshot、一次构造 `GitSnapshotProvider`、一次读取 old/new file、一次生成 Git line mapping、一次解析 old/new C/C++ analysis。
3. 把 `_snapshot_for_relation()` 拆成“文件级 snapshot 准备”和“relation 级映射消费”两层；relation 级代码不得再启动 Git subprocess、重复读取同一文件或重复 parse 整文件。
4. 批量预取 existing decisions、AnalysisRecord、active links、source repository_id，避免 relation 循环中的 `SELECT` N+1；decision/link/record 写入按有界 batch 提交给 repository 层。
5. 保留所有既定语义：固定 predecessor、branch mismatch、repository identity verification、NON_ANCESTOR、删除断链、Block identity、control/preprocessor/dependency fingerprint、逐 candidate line decision。
6. 为 Git read/diff/parser/SQL 增加 per-run counter，作为回归门禁；计数应随“唯一文件/Commit 对”增长，而不是随 relation 数增长。

#### 数据、兼容与回滚约束

- **原兼容结论：** 任何缓存/批处理都必须保持固定 predecessor、不跨分支、删除即断链、Block 唯一匹配以及逐行 decision evidence。
- **Migration：** 不需要业务数据迁移；若增加持久化 artifact 才需要新增可重建表。
- **Rollback：** 保留旧 relation-loop 实现为短期 feature flag/oracle；出现语义 hash 差异立即回退，不迁移业务数据。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/vnext/test_inheritance_engine.py -q`
- `python -m pytest tests/vnext/test_deterministic_inheritance_corpus.py -q`
- `python -m pytest tests/vnext/test_parser_toolchain.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/deterministic_inheritance_corpus.py`
- `scripts/diagnostics/inheritance_rules_audit.py`

#### 专项验证设计

审计报告原始验证要求：构造 1/100/1000 relation 同文件 workload；记录 Git subprocess 次数、parser invocation、SQL 次数、CPU、RSS、结果语义哈希。目标：昂贵操作随文件数增长，而非随 relation 数增长。

1. **验收门槛 1：** 同一输入下 old/new 实现的 decision、reason_code、mapping/function/control/preprocessor/dependency fingerprint 语义哈希 100% 一致。
2. **验收门槛 2：** 同一文件 1/100/1000 relations 时，old/new file read、Git mapping、old/new parse 的次数保持文件级 O(1)。
3. **验收门槛 3：** 不存在跨 branch、跨 predecessor 或删除后重新继承的语义回退。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### JOB-001：Scan Import 主链单大事务，checkpoint 不能形成阶段耐久恢复点

| 字段 | 内容 |
|---|---|
| Severity | P1 |
| Evidence | Confirmed |
| Owner Skill | `fos-coverage-runtime-reliability` |
| Layer | Background Job / Transaction / DB |
| File | `app/scan_import/coordinator.py`；`app/scan_import/recovery.py` |
| Function/Class | `ScanImportCoordinator.execute`、phase/checkpoint advancement、publication service |
| Code Location | target transaction 覆盖 ingest、inheritance、file-state rebuild、consistency audit；最终才 commit/publish |
| Root Cause | 把“Candidate 对外不可见”错误等同于“所有 Candidate 构建必须在一个事务”；没有利用 unpublished Candidate + fencing 来分阶段提交。 |
| Current Impact | 大 Scan 长时间占用数据库连接和 database resource worker；失败时 CPU/I/O/SQL 全部重做。 |
| Scale Impact | 事务时长随文件、行、继承关系增长；可能增加 InnoDB undo、锁等待、连接池饥饿和 shutdown drain 时间。 |
| Compatibility | CURRENT publication 与 predecessor/read-set CAS 必须保持短事务原子；阶段提交不能让半成品进入生产读视图。 |
| Action Type | `FIX` |
| Phase / Package | `Phase 2` / `PKG-06` |
| Dependencies | `MIGRATION-001`, `OBS-001` |

#### 修改目标

关闭 `JOB-001` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：Candidate 隐藏态分阶段幂等提交。** `STAGED/PARSED/INGESTED/INHERITED/REBUILT/AUDITED` 各阶段有独立 checkpoint 与输入 hash；最终仅用短事务校验 fence/read set 并切换 CURRENT。

#### 详细修改步骤

1. 把 Scan Import lifecycle 明确为 durable phases：`STAGED → PARSED → INGESTED → INHERITED → REBUILT → AUDITED → PUBLISHED`，阶段值和版本写入 checkpoint。
2. Candidate 从创建起保持 unpublished/hidden；允许 INGEST/INHERIT/REBUILD 等阶段在独立短事务中幂等提交，但任何 read path 不得把未发布 Candidate 当 CURRENT。
3. checkpoint 与每个阶段业务写使用同一阶段事务提交；checkpoint 必须记录 input SHA、fencing token、expected predecessor/current、batch cursor。
4. 恢复时只从最后已提交 phase/cursor 继续；进入阶段前重验 staged artifact、physical-resource fence、Candidate identity。
5. 最终 publication 仍由单一 `ScanPublicationService` 在短事务中完成：验证 read set、fence、expected CURRENT/predecessor 后原子切 CURRENT。
6. 每个 phase 增加可注入故障点，确保 kill/restart 测试可重复。

#### 数据、兼容与回滚约束

- **原兼容结论：** CURRENT publication 与 predecessor/read-set CAS 必须保持短事务原子；阶段提交不能让半成品进入生产读视图。
- **Migration：** 可能需增加 checkpoint payload/version 或 batch cursor 字段；均为可重建运行状态。
- **Rollback：** 废弃 unpublished Candidate 和其 staging/checkpoint；旧 CURRENT 不变。已发布版本 rollback 仍走 release governance，不做逆向数据库降级。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/vnext/test_scan_import_lifecycle.py -q`
- `python -m pytest tests/vnext/test_jobs.py -q`
- `python -m pytest tests/database/test_file_state_transaction.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/job_lifecycle_audit.py`
- `scripts/diagnostics/mysql_vnext_integration.py`

#### 专项验证设计

审计报告原始验证要求：强制在每个阶段注入 kill；重启后确认从最后 durable checkpoint 继续；校验无重复行、无重复 link、CURRENT 未提前变化。

1. **验收门槛 1：** 在每个 phase 前/中/后 kill 后重启，恢复从最后 durable checkpoint 继续，不从 STAGED 全量重做。
2. **验收门槛 2：** 失败/恢复期间 CURRENT 始终指向旧 published Scan，直到最终 publication 成功。
3. **验收门槛 3：** 无重复 file/line/link/decision；stale fencing token 不能写入。
4. **验收门槛 4：** 最终 publication transaction 保持短事务，只包含校验与 CURRENT/Candidate 状态切换。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### JOB-002：Scan artifact 重复全读、重复哈希并形成双重对象图

| 字段 | 内容 |
|---|---|
| Severity | P1 |
| Evidence | Confirmed |
| Owner Skill | `fos-coverage-runtime-reliability` |
| Layer | Background Job / File I/O / Memory |
| File | `app/scan_import/artifacts.py`；`app/scan_import/coordinator.py`；`app/inject/service.py` |
| Function/Class | `ImmutableArtifactStager.stage/verify`、`ScanImportService.parse_info_file/build_files` |
| Code Location | stage copy+SHA、execute verify SHA、parse_info_file 再 SHA/load；parsed 结构转为 files/lines 结构 |
| Root Cause | 各阶段独立自证输入完整性，没有共享 immutable artifact descriptor 与流式转换接口。 |
| Current Impact | 大 info 文件产生重复磁盘读、SHA CPU 和较高 peak RSS。 |
| Scale Impact | I/O 至少按 artifact size 多倍增长；Python 对象数量近似同时覆盖 parsed facts 与 insert records。 |
| Compatibility | 输入 SHA、size、immutable staging 和恢复时重新验证的证据不能取消。 |
| Action Type | `FIX` |
| Phase / Package | `Phase 2` / `PKG-06` |
| Dependencies | `JOB-001`, `OBS-001` |

#### 修改目标

关闭 `JOB-002` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：stage 时单次流式 copy+hash，持久化 descriptor；同进程后续使用 descriptor，恢复时按策略重新校验一次。解析器输出有界 batch，直接进入 Candidate ingest。**

#### 详细修改步骤

1. `ImmutableArtifactStager.stage()` 改为 copy 过程中流式计算 SHA256/size，并把 descriptor 持久化到 import artifact/checkpoint。
2. 正常同进程后续阶段只消费 descriptor，不再次全文件 hash；恢复进程允许在重新进入工作前做一次完整 verify。
3. 将 info parser 改为 iterator/batch 输出；`build_files` 不再同时持有完整 parsed graph 与完整 files/lines insert graph。
4. ingest repository 提供有界 batch API；每 batch 写完后释放 Python 对象并推进 checkpoint cursor。
5. 增加 artifact I/O counter：open count、bytes read、hash count、parse batch count。

#### 数据、兼容与回滚约束

- **原兼容结论：** 输入 SHA、size、immutable staging 和恢复时重新验证的证据不能取消。
- **Migration：** 无业务迁移；checkpoint 需记录 descriptor version。
- **Rollback：** 保留原始 staged artifact 和旧 parser 路径 feature flag；不改变 authoritative data contract。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/vnext/test_scan_import_lifecycle.py -q`
- `python -m pytest tests/incremental/test_phase5_inject_path.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/perf_benchmark.py`
- `scripts/diagnostics/job_lifecycle_audit.py`

#### 专项验证设计

审计报告原始验证要求：记录 open/read bytes、hash calls、parse calls、peak RSS；1GB artifact 目标是正常路径仅一次输入读取加必要的一次恢复验证。

1. **验收门槛 1：** 无中断正常路径对同一 staged artifact 只做一次完整 hash/read；恢复最多增加一次完整 verify。
2. **验收门槛 2：** 输入 SHA/size/immutability 证据与当前实现等价或更强。
3. **验收门槛 3：** 大 artifact peak RSS 由 batch 大小而非文件总大小主导。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### JOB-003：资源桶使 max_workers 不再是进程级并发上限

| 字段 | 内容 |
|---|---|
| Severity | P2 |
| Evidence | Strong Risk |
| Owner Skill | `fos-coverage-runtime-reliability` |
| Layer | Background Job / Concurrency |
| File | `app/bootstrap.py`；`app/jobs/bounded_executor.py` |
| Function/Class | `VNextRuntime.__init__`、`BoundedJobExecutor.__init__/_start_workers` |
| Code Location | default bucket 与 database/cpu/disk buckets 分别创建 workers；`global_worker_budget` 默认 None |
| Root Cause | 资源隔离与总资源治理分离，但默认没有进程级 budget。 |
| Current Impact | 多类任务同时到达时，DB、CPU、磁盘负载可能高于配置直觉；每个运行任务还创建 heartbeat thread。 |
| Scale Impact | 多 Project/Scan 并发时放大连接池竞争、CPU 抢占和磁盘随机 I/O。 |
| Compatibility | 直接收紧并发可能改变任务吞吐；必须明确配置语义。 |
| Action Type | `MEASURE_THEN_FIX` |
| Phase / Package | `Phase 2` / `PKG-08` |
| Dependencies | `OBS-001` |

#### 修改目标

关闭 `JOB-003` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：默认设置 `global_worker_budget=max_workers`，并校验资源桶 worker/queue 配置；指标同时暴露 bucket 与 global saturation。**

#### 详细修改步骤

1. 将 `global_worker_budget` 默认值显式设为 `jobs.max_workers`，而不是 `None`；配置可显式放宽但必须通过 preflight。
2. 启动时校验各 resource bucket workers/queue，总预算与 DB pool 上限关系；危险配置 fail fast 或强告警。
3. metrics 增加 global active/limit、per-bucket queued/active、saturation、queue reject。
4. 为 database/cpu/disk/default 混合 workload 增加并发测试，覆盖队列满、shutdown、cancel。

#### 数据、兼容与回滚约束

- **原兼容结论：** 直接收紧并发可能改变任务吞吐；必须明确配置语义。
- **Migration：** 无。
- **Rollback：** 恢复原配置行为即可；不涉及数据迁移。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/vnext/test_jobs.py -q`
- `python -m pytest test_concurrency.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/job_lifecycle_audit.py`

#### 专项验证设计

审计报告原始验证要求：并发提交 DB/CPU/disk/default jobs，确认 active 总数不超过 budget，测 pool wait、CPU、I/O 与吞吐。

1. **验收门槛 1：** 任意时刻进程 active job 总数不得超过 global budget。
2. **验收门槛 2：** 资源桶隔离仍成立：disk-heavy 不占 database worker，但总并发不失控。
3. **验收门槛 3：** 队列满时返回明确错误，不能静默丢任务。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### JOB-004：恢复或排队任务执行前缺少 data-version/current fence

| 字段 | 内容 |
|---|---|
| Severity | P2 |
| Evidence | Confirmed |
| Owner Skill | `fos-coverage-runtime-reliability` |
| Layer | Background Job / Data Version |
| File | `app/jobs/service.py`；`app/bootstrap.py`；`app/api/application.py` |
| Function/Class | `VNextBackgroundJobService._runner/recover`、`_rebuild_progress_recovery_handler`、`_export_recovery_handler` |
| Code Location | callback 开始前直接执行业务，没有比较 durable job 的 data_version/scan identity 与当前项目状态 |
| Root Cause | durable payload 可重建 callback，但缺少执行资格 fence。 |
| Current Impact | 过期任务消耗资源；rebuild 可能为非预期版本写 Derived State，或使运维误判任务结果属于当前数据。 |
| Scale Impact | 队列积压、重启恢复和高频 Scan 时更常见。 |
| Compatibility | 旧行为是“尽量执行”；新行为需将 stale 明确为终态而非静默丢弃。 |
| Action Type | `FIX` |
| Phase / Package | `Phase 2` / `PKG-06` |
| Dependencies | `JOB-001` |

#### 修改目标

关闭 `JOB-004` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：runner 开始时读取 project state，校验 exact project/scan/data_version/current requirement；不符合则写 `superseded`/`interrupted` 原因。**

#### 详细修改步骤

1. 在 durable job payload 中固化 `project_id/scan_id/data_version/current_requirement/handler_version`。
2. VNext runner 真正执行 callback 前重新读取 ProjectState 与 Scan 状态。
3. 按 kind 定义 fence policy：rebuild_progress 必须 exact data_version/current scan；export 至少 exact immutable scan；scan_import 使用专用 checkpoint/fence。
4. 发现过期时不执行 callback，写入明确 `superseded`（若不新增 enum则 interrupted+reason_code）终态。
5. 恢复 handler 同样执行该 fence，禁止 restart 后重放过期任务。

#### 数据、兼容与回滚约束

- **原兼容结论：** 旧行为是“尽量执行”；新行为需将 stale 明确为终态而非静默丢弃。
- **Migration：** 可能新增 `superseded` 状态或 reason code；需兼容旧状态读取。
- **Rollback：** 仅 runner fence 可回退；已有 job rows 无需迁移，新增字段若有则保持向后兼容默认。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/vnext/test_jobs.py -q`
- `python -m pytest tests/vnext/test_vnext_runtime.py -q`
- `python -m pytest tests/vnext/test_scan_import_lifecycle.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/job_lifecycle_audit.py`

#### 专项验证设计

审计报告原始验证要求：提交 job 后推进 data_version/CURRENT，再释放 worker；验证 stale job 不写新版本事实，状态可解释。

1. **验收门槛 1：** 提交 job 后推进 CURRENT/data_version，再释放 worker，过期 callback 不产生任何新业务写。
2. **验收门槛 2：** 终态可解释且不会在 recover 中再次被无限领取。
3. **验收门槛 3：** scan_import 仍由专用 recovery owner 处理，不被 generic reaper 抢占。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### INHERIT-002：依赖解析对两个 Commit 建立全仓 C/C++ 索引且缓存无字节边界

| 字段 | 内容 |
|---|---|
| Severity | P1 |
| Evidence | Strong Risk |
| Owner Skill | `fos-coverage-change-review` |
| Layer | Analyzer / Cache / Git |
| File | `app/inheritance/engine.py`；`app/inheritance/dependencies.py`；`app/inheritance/git_snapshot.py` |
| Function/Class | `InheritanceEngine._source_index`、`SourceAnalysisIndex` |
| Code Location | `_source_index` 中 `provider.list_source_files(commit)` 后遍历并解析全部支持的源文件 |
| Root Cause | 为保证 callee/macro/constant 依赖完整性，当前实现预先构建全仓索引；缓存只依赖 Python dict 生命周期，没有资源预算。 |
| Current Impact | 中大型仓库首次 inheritance run 可能出现明显 CPU/RSS 峰值；两个 Commit 至少形成两份分析图。 |
| Scale Impact | CPU/内存近似随两个 Commit 的全部 C/C++ 源码总量增长，而非随本次触达文件增长；多 Repository/Commit 会继续累积。 |
| Compatibility | 不能因局部解析漏掉实际依赖而错误继承；不确定时必须 NO_INHERIT。 |
| Action Type | `MEASURE_THEN_FIX` |
| Phase / Package | `Phase 3` / `PKG-10` |
| Dependencies | `INHERIT-001`, `OBS-001` |

#### 修改目标

关闭 `INHERIT-002` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：按触达依赖闭包渐进解析 + byte-aware LRU。** 先解析候选文件，按调用/宏/常量引用扩展到有界闭包；超出深度/预算时返回明确 unresolved。

#### 详细修改步骤

1. 先通过新增 telemetry 量化 `_source_index()` 在真实/代表性仓库中解析文件数、总源码字节、CPU、peak RSS、cache bytes；未证明成为瓶颈前不引入持久化索引。
2. 把 SourceAnalysisIndex 构造改为按候选文件开始的惰性解析接口；callee/macro/constant 解析按依赖闭包扩展。
3. 设置明确的 `max_parsed_files / max_source_bytes / max_dependency_depth`，达到预算时返回明确 unresolved，严格 NO_INHERIT，绝不猜测。
4. 将 `_source_index_cache` 改为 key 含 physical repo identity + commit + parser/algorithm version 的 byte-aware LRU；单 entry 超限时 bypass。
5. 如真实 benchmark 证明同一 Commit 索引跨 Scan 重用价值很高，再单独设计可重建 artifact，不在本次首轮优化中引入新权威表。

#### 数据、兼容与回滚约束

- **原兼容结论：** 不能因局部解析漏掉实际依赖而错误继承；不确定时必须 NO_INHERIT。
- **Migration：** 无；若实施 B，需要可丢弃重建的索引表/文件，不得成为业务事实权威。
- **Rollback：** 禁用渐进索引 feature flag 即回到当前全仓索引；缓存可直接清空。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/vnext/test_inheritance_engine.py -q`
- `python -m pytest tests/vnext/test_parser_toolchain.py -q`
- `python -m pytest tests/vnext/test_deterministic_inheritance_corpus.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/parser_toolchain_preflight.py`
- `scripts/diagnostics/perf_benchmark.py`

#### 专项验证设计

审计报告原始验证要求：固定 1k/10k/50k 文件仓库，比较全仓索引与渐进索引的解析文件数、CPU、peak RSS、unresolved 比例和继承结果差异。

1. **验收门槛 1：** 预算耗尽时只能增加 unresolved/NO_INHERIT，不能增加错误 INHERITED。
2. **验收门槛 2：** cache resident bytes 受配置硬上限约束；不同 repo/commit/parser version 不共享 entry。
3. **验收门槛 3：** 代表性大仓 workload 中解析文件数与触达闭包相关，而不是无条件等于全仓 C/C++ 文件数。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### API-003：Job 列表与恢复扫描无分页、无归档边界

| 字段 | 内容 |
|---|---|
| Severity | P2 |
| Evidence | Strong Risk |
| Owner Skill | `fos-coverage-runtime-reliability` |
| Layer | API / DB / Memory |
| File | `app/api/application.py`；`app/db/repositories/job_repository.py` |
| Function/Class | `VNextApplication.jobs/recover_jobs`、`JobRepository.list/list_recoverable` |
| Code Location | `fetchall` + `ORDER BY created_at,job_id`，无 limit/cursor/time horizon |
| Root Cause | durable job 表同时作为活动队列和永久历史账本，却没有热/冷数据生命周期。 |
| Current Impact | 当前表小可能无明显影响；实际影响需生产行数。 |
| Scale Impact | 多年运行后 API Payload、进程内存、排序与 startup recovery 时间不断增长。 |
| Compatibility | 默认第一页应兼容常见运维查看；历史审计仍需可检索。 |
| Action Type | `FIX_AFTER_MEASURE` |
| Phase / Package | `Phase 3` / `PKG-11` |
| Dependencies | `OBS-001` |

#### 修改目标

关闭 `API-003` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：keyset pagination + state/time filters；恢复只扫描 active/stale window；终态任务按策略归档。**

#### 详细修改步骤

1. `JobRepository.list()` 增加 `limit/cursor/state/project/time`；API 默认返回有限页，并返回 next_cursor。
2. 恢复扫描只选择 queued 或 stale running 的有界窗口；使用 keyset/索引而非全量 fetchall。
3. 定义终态 retention：在线 DB 保留近期窗口，历史转审计归档；首轮不物理删除。
4. metrics 暴露 scanned_rows/candidates/recovered/archived_count。

#### 数据、兼容与回滚约束

- **原兼容结论：** 默认第一页应兼容常见运维查看；历史审计仍需可检索。
- **Migration：** 归档策略可能需要新表；分页本身不需要。
- **Rollback：** API 可暂时提供 legacy default page；归档先不删除，可完全回切。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/vnext/test_jobs.py -q`
- `python -m pytest tests/test_phase3_jobs_export.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/job_lifecycle_audit.py`

#### 专项验证设计

审计报告原始验证要求：10k/1M job rows，测 API p95、RSS、rows examined、startup recovery time，确认 active jobs 不漏。

1. **验收门槛 1：** 10k/1M job rows 时 API payload/内存受 page size 约束。
2. **验收门槛 2：** startup recovery 时间取决于 active/stale candidates，而不是总历史终态数。
3. **验收门槛 3：** active job 零漏检、零重复 recovery claim。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### DB-004：Background Job 热查询缺少匹配复合索引

| 字段 | 内容 |
|---|---|
| Severity | P2 |
| Evidence | Strong Risk |
| Owner Skill | `fos-coverage-runtime-reliability` |
| Layer | DB / Background Job |
| File | `app/db/repositories/job_repository.py`；`scripts/upgrade/vnext_schema.sql` |
| Function/Class | `find_active`、`list_recoverable`、`mark_stale` |
| Code Location | project+scan+kind+data_version+state 查询；state+heartbeat+lease_owner+kind 恢复查询 |
| Root Cause | 表最初面向小规模任务记录，未针对长期保留和恢复扫描设计复合索引。 |
| Current Impact | 当前任务表较小时影响有限；无法仅凭源码确认现有数据规模是否已经慢。 |
| Scale Impact | Job 表持续增长后可能扩大扫描、filesort 和恢复启动时间；queued/running 查找处于提交热路径。 |
| Compatibility | MariaDB 5.5 索引长度与列顺序需谨慎；状态枚举不能变。 |
| Action Type | `MEASURE_THEN_FIX` |
| Phase / Package | `Phase 3` / `PKG-11` |
| Dependencies | `OBS-001`, `API-003` |

#### 修改目标

关闭 `DB-004` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：在真实 EXPLAIN 后增加两类复合索引：active identity 前缀，以及 recovery state/heartbeat/owner 前缀；同时归档过旧终态任务。**

#### 详细修改步骤

1. 先用代表性 job 数据执行 EXPLAIN，记录 find_active、list_recoverable 的 access type、rows、filesort。
2. 根据实际谓词创建复合索引，候选方向为 `(project_id,scan_id,kind,data_version,state,created_at)` 与 `(state,heartbeat_at,lease_owner,kind)`；最终顺序以 EXPLAIN 决定。
3. MariaDB 5.5 上评估索引长度和 DDL 行为，避免超限；若必要使用前缀或拆分。
4. 增加终态 job retention/archive policy，归档只能影响可审计历史展示，不得删除 active/recoverable。
5. 迁移脚本只做 additive index DDL，并提供 index-exists 幂等检查。

#### 数据、兼容与回滚约束

- **原兼容结论：** MariaDB 5.5 索引长度与列顺序需谨慎；状态枚举不能变。
- **Migration：** YES：在线/低锁索引迁移，需记录 DDL 耗时和 rollback。
- **Rollback：** 索引可独立 DROP；归档策略先仅标记/冷存，不在首轮做不可逆删除。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/vnext/test_jobs.py -q`
- `python -m pytest tests/database/test_mariadb_compatibility_contract.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/job_lifecycle_audit.py`
- `scripts/diagnostics/mysql_vnext_integration.py`

#### 专项验证设计

审计报告原始验证要求：复制脱敏生产 job 分布，执行 EXPLAIN/ANALYZE；测 find_active/recover p95、rows examined、DDL 锁窗口。

1. **验收门槛 1：** EXPLAIN 显示热查询使用目标索引且 rows examined 明显下降。
2. **验收门槛 2：** DDL 在 MariaDB 5.5 rehearsal 可执行、可重复、不会修改业务行。
3. **验收门槛 3：** active/recoverable job 集合与加索引前完全一致。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### INHERIT-003：继承输入、read set 与输出 decisions 全量物化

| 字段 | 内容 |
|---|---|
| Severity | P2 |
| Evidence | Strong Risk |
| Owner Skill | `fos-coverage-change-review` |
| Layer | Service / Memory / DB |
| File | `app/inheritance/engine.py` |
| Function/Class | `InheritanceEngine.run`、`_read_set_for_relations` |
| Code Location | `fetchall(source_relations)`、`fetchall(candidate_lines)`、`decisions=[]`、`read_set` 构建 |
| Root Cause | 方法以一次返回完整审计结果为目标，没有将“数据库已持久化的逐行证据”与“调用者实际需要的汇总”分离。 |
| Current Impact | 当前小中规模可能可接受，但会增加 GC、对象分配和事务内 RSS。 |
| Scale Impact | 空间复杂度至少 `O(R + L + D)`；当全部未覆盖行都生成 decision 时 D 接近 L。 |
| Compatibility | 必须继续持久化每个候选行的 decision/reason；不能为了省内存跳过未继承行。 |
| Action Type | `FIX_AFTER_MEASURE` |
| Phase / Package | `Phase 3` / `PKG-09` |
| Dependencies | `INHERIT-001`, `JOB-001`, `OBS-001` |

#### 修改目标

关闭 `INHERIT-003` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：按 repository/file 使用服务器游标或 keyset 分页；每批持久化 decision，只在内存保留计数、read-set hash 和当前文件状态。**

#### 详细修改步骤

1. 把 source relations 与 candidate lines 的 `fetchall()` 改为 repository/file keyset 分页或服务器游标；分页 key 必须稳定。
2. 每个 file batch 处理完立即持久化 decisions；调用者返回值改为 summary + decision query identity，不再携带全部 decision DTO。
3. read set 不保留全量 Python dict；使用排序流计算 deterministic hash，同时必要的 relation/content revisions 持久化到 checkpoint/evidence。
4. candidate file/line lookup 只维护当前文件映射；跨文件切换时释放对象。
5. 增加 batch size 配置与默认安全值，并把 batch size、rows processed、peak batch rows 暴露到 metrics。

#### 数据、兼容与回滚约束

- **原兼容结论：** 必须继续持久化每个候选行的 decision/reason；不能为了省内存跳过未继承行。
- **Migration：** 无。
- **Rollback：** 保留一次性模式为诊断开关；Candidate 未发布前可废弃重跑。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/vnext/test_inheritance_engine.py -q`
- `python -m pytest tests/vnext/test_scan_import_lifecycle.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/perf_benchmark.py`

#### 专项验证设计

审计报告原始验证要求：10k/100k/1M candidate lines，记录 peak RSS 与数据库 row count 守恒；对比 decision semantic hash。

1. **验收门槛 1：** 10k/100k/1M candidate fixture 中 peak resident Python rows 由 batch size 决定，而不是由总行数决定。
2. **验收门槛 2：** 数据库 decision 总数、reason 分布、semantic hash 与旧实现一致。
3. **验收门槛 3：** 任何分页/恢复中断都不允许遗漏或重复 candidate line decision。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### RUNTIME-001：连接池在全局锁内建立网络连接

| 字段 | 内容 |
|---|---|
| Severity | P2 |
| Evidence | Strong Risk |
| Owner Skill | `fos-coverage-runtime-reliability` |
| Layer | Runtime / DB Pool / Lock |
| File | `app/db/connection_pool.py` |
| Function/Class | `MySQLConnectionPool.borrow_connection`、`_create_raw_connection` |
| Code Location | `_active_count < max_connections` 的 lock scope 内调用 `pymysql.connect` |
| Root Cause | 为防止并发超建连接，把 slot 检查与实际网络创建放在同一锁内。 |
| Current Impact | 正常复用 idle connection 时影响小；连接失效、启动、突发并发时可能串行阻塞其他 borrow/metrics/discard。 |
| Scale Impact | 数据库抖动时形成 head-of-line blocking，并可能触发更多 borrow timeout。 |
| Compatibility | 必须严格保持 max_connections，不得因锁外 connect 超建。 |
| Action Type | `MEASURE_THEN_FIX` |
| Phase / Package | `Phase 3` / `PKG-11` |
| Dependencies | `OBS-001` |

#### 修改目标

关闭 `RUNTIME-001` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：锁内原子预留 connecting slot，锁外建立连接；成功转 active，失败释放 slot 并 notify。**

#### 详细修改步骤

1. 在 pool state 中增加 `_connecting_count`；borrow 锁内只检查/预留 slot，随后释放锁。
2. 网络 `_create_raw_connection()` 在锁外执行；成功后锁内把 reserved 转 active，失败则归还 slot 并 notify waiters。
3. shutdown 时同时考虑 active/idle/connecting，防止连接建立完成后进入已 shutdown pool。
4. metrics 提供真实 waiters/connecting/acquire latency。
5. 故障注入覆盖慢 DNS、连接超时、认证失败和 pool exhaustion。

#### 数据、兼容与回滚约束

- **原兼容结论：** 必须严格保持 max_connections，不得因锁外 connect 超建。
- **Migration：** 无。
- **Rollback：** 恢复旧 pool 逻辑即可；连接池无持久化数据。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest test_concurrency.py -q`
- `python -m pytest tests/vnext/test_vnext_runtime.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/connection_pool_audit.py`

#### 专项验证设计

审计报告原始验证要求：注入 1s/5s connect delay 和失败，100 并发 borrow；确认连接上限、无死锁、wait p95 和 timeout 行为。

1. **验收门槛 1：** 任何网络 connect 都不在 pool 全局锁内执行。
2. **验收门槛 2：** 100 并发 borrow 下 active+connecting 从不超过 max_connections。
3. **验收门槛 3：** 连接失败不会泄漏 slot，shutdown 无悬挂连接。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### API-001：进展主页主动请求全 Scan Pending 物理行

| 字段 | 内容 |
|---|---|
| Severity | P1 |
| Evidence | Confirmed |
| Owner Skill | `fos-coverage-performance-ui` |
| Layer | Frontend / API / DB |
| File | `web/assets/js/coverage_progress.js`；`app/api/application.py`；`app/services/progress_service.py`；`app/db/repositories/file_state_repository.py` |
| Function/Class | `loadProgress`、`unanalyzed`、`pending_by_file`、`pending_line_references` |
| Code Location | `/progress` 成功后立即调用 `/incremental/unanalyzed`；后端无 limit 时返回全部 pending lines |
| Root Cause | 复用了面向开发者任务的全量明细接口，没有区分首页聚合、文件分页和逐行明细。 |
| Current Impact | 每次加载产生全 Scan JOIN/ORDER BY、Python grouping、JSON 序列化和大数组分配。 |
| Scale Impact | DB rows、Payload bytes、浏览器 heap 随 pending line count 线性增长；多用户刷新时再乘请求数。 |
| Compatibility | 新接口需保留项目、Scan、Repository 身份；不能把 inherited/manual pending 混淆。 |
| Action Type | `FIX` |
| Phase / Package | `Phase 4` / `PKG-12` |
| Dependencies | `DB-001`, `OBS-001` |

#### 修改目标

关闭 `API-001` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：首页使用 SQL 按文件聚合并 cursor pagination；逐行列表只在用户打开文件详情时分页加载。** `/progress/pending` 已有分页能力，可统一到同一合同。

#### 详细修改步骤

1. Progress 首页停止调用无分页的全量 `/incremental/unanalyzed` 来填充文件表。
2. 后端提供按 file 聚合的 pending page：`repository_name/file_path/pending_total/...`，使用 cursor/keyset，而非 OFFSET 深分页。
3. 逐行 pending 仅在用户打开文件详情时按页加载；页面首次加载只拿 summary + 第一页文件。
4. 为旧 endpoint 保留受控兼容期，但增加最大 page_size，禁止无界返回。
5. 前端状态保存 cursor/page identity；project/scan 变化时丢弃旧响应。

#### 数据、兼容与回滚约束

- **原兼容结论：** 新接口需保留项目、Scan、Repository 身份；不能把 inherited/manual pending 混淆。
- **Migration：** 无数据库迁移；API contract 需版本化。
- **Rollback：** 前端可切回旧 endpoint，但服务端必须保留 response hard cap，避免恢复无界行为。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/progress/test_phase4_progress.py -q`
- `python -m pytest tests/vnext/test_vnext_runtime.py -q`
- `python -m pytest tests/browser/vnext_http_integration.spec.js -q`
- `python -m pytest tests/browser/coverage_real_browser.spec.js -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/perf_benchmark.py`
- `scripts/diagnostics/real_browser_evidence.js`

#### 专项验证设计

审计报告原始验证要求：1k/100k/1M pending lines，测 SQL rows examined、response bytes、TTFB、JS heap、DOM 和页面交互 p95。

1. **验收门槛 1：** 1M pending lines 时首页 response bytes 仍受 page_size 上限约束，不随总行数线性增长。
2. **验收门槛 2：** 首页不加载物理 pending line 全集。
3. **验收门槛 3：** 分页切换、Scan 切换、stale response 不混淆数据。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### CACHE-001：Code Detail/Sidecar 缓存按数量而非字节治理

| 字段 | 内容 |
|---|---|
| Severity | P1 |
| Evidence | Strong Risk |
| Owner Skill | `fos-coverage-performance-ui` |
| Layer | Cache / Memory / Code Detail |
| File | `app/code_detail/vnext_service.py`；`app/code_detail/sidecar_store.py`；`web/assets/js/coverage_enhance.js` |
| Function/Class | identity/overlay caches、decoded chunk cache、metadata/legacy cache、browser RegionLineLRUCache |
| Code Location | 固定 entry/chunk 上限；部分 metadata/legacy 容器未见 byte limit；overlay entry 可含大量 line mapping |
| Root Cause | 容量模型使用对象数量近似内存，未按内容大小和进程 RSS 反馈治理。 |
| Current Impact | 当前没有 exact-SHA RSS 证据，不能确认已超限。 |
| Scale Impact | 多报告/Scan/大文件轮换时，缓存可能维持数百万 line metadata/decoded objects，导致不可预测 RSS 与 GC。 |
| Compatibility | cache key 必须继续绑定 report/scan/file/data_version/asset identity；不能用错误命中换性能。 |
| Action Type | `MEASURE_THEN_FIX` |
| Phase / Package | `Phase 4` / `PKG-14` |
| Dependencies | `OBS-001`, `UI-004` |

#### 修改目标

关闭 `CACHE-001` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：所有服务端缓存改为 byte-aware LRU，设置单 entry、单 store、进程总预算；暴露 bytes、hit/miss、eviction、oversize bypass。legacy full-source cache 默认禁用或严限。**

#### 详细修改步骤

1. 统一服务端 cache entry 接口，必须能报告估算/实际 bytes；至少覆盖 overlay、sidecar metadata/chunk、decoded source/legacy full-source cache。
2. 增加 per-entry max、per-cache max、process cache budget；oversize entry 直接 bypass 而不是强塞。
3. LRU key 必须包含 report/scan/source_signature/schema/asset/data_version 等现有 identity，不能为了命中率降级身份。
4. eviction 前确认 cache 仅含可重建数据；浏览器 DraftStore 不跟随服务端 cache eviction。
5. metrics 暴露 bytes/hit/miss/eviction/oversize_bypass；用 release identity 区分版本。

#### 数据、兼容与回滚约束

- **原兼容结论：** cache key 必须继续绑定 report/scan/file/data_version/asset identity；不能用错误命中换性能。
- **Migration：** 无；缓存均可重建。
- **Rollback：** cache feature flag/预算调到 0 可关闭；缓存数据可直接丢弃。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest test_code_detail_service.py -q`
- `python -m pytest tests/code_detail/test_phase6_sidecar.py -q`
- `python -m pytest test_lazy_collapse_v11_fixes.py -q`
- `python -m pytest tests/browser/vnext_http_integration.spec.js -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/release_performance_ab.js`
- `scripts/diagnostics/performance_evidence_audit.py`

#### 专项验证设计

审计报告原始验证要求：大小文件混合 workload、多 Scan/re-expand；测 steady/peak RSS、eviction、cache hit、p95、内容身份正确性。

1. **验收门槛 1：** steady-state cache bytes 受配置预算约束；单 oversize 对象不会挤爆总预算。
2. **验收门槛 2：** eviction/re-expand 后内容 identity、line ordering、draft/edit 状态正确。
3. **验收门槛 3：** 缓存关闭时功能仍正确，只允许性能下降。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### UI-001：继承按钮请求前 500 条全局 Pending，失败时静默本地复制

| 字段 | 内容 |
|---|---|
| Severity | P1 |
| Evidence | Confirmed |
| Owner Skill | `fos-coverage-performance-ui` |
| Layer | Frontend / API / Business Behavior |
| File | `web/assets/js/coverage_enhance.js`；inheritance pending endpoint |
| Function/Class | 前端 server inheritance candidate lookup、panel inherit action |
| Code Location | 每次操作请求固定上限 pending 集合，再在客户端按当前 file/line 查找；未命中/异常后使用上一面板内容 |
| Root Cause | 将 legacy convenience copy 与 VNext deterministic inheritance 混在一个按钮/错误处理路径。 |
| Current Impact | 每次点击产生不必要列表请求；在大 pending 集合中可能稳定找不到目标；用户无法区分服务器继承与本地复制。 |
| Scale Impact | Pending 越多，命中率越低；并发用户增加相同列表查询。 |
| Compatibility | 本地复制可能改变既定“只有 deterministic engine 产生继承关系”的业务语义和审计血缘。 |
| Action Type | `FIX` |
| Phase / Package | `Phase 4` / `PKG-13` |
| Dependencies | `API-001` |

#### 修改目标

关闭 `UI-001` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：提供 exact `scan_id + repository + file + line/relation` 查询/操作 endpoint；没有服务端 relation 时按钮明确禁用或转为单独的“复制上一条”。禁止 silent fallback。**

#### 详细修改步骤

1. 新增 exact inheritance relation/query contract，以 `scan_id + repository_name + file_path + line_id/relation_id` 定位，不再为了一个按钮请求前 500 条全局列表。
2. 前端将“自动继承复核”和“复制上一条内容”拆成两个明确动作；自动继承必须有服务器 relation/revision 事实。
3. 网络失败、404、stale revision 时保持当前 panel 不变并显示可操作错误，不 silent fallback。
4. mutation 继续携带 expected relation/content/rejection revision，服务端 409 时重新加载对应行，不覆盖用户 draft。
5. compat copy 动作若保留，必须只产生 MANUAL draft，不伪装 INHERITANCE。

#### 数据、兼容与回滚约束

- **原兼容结论：** 本地复制可能改变既定“只有 deterministic engine 产生继承关系”的业务语义和审计血缘。
- **Migration：** 无；可能需新增 endpoint。
- **Rollback：** 可隐藏 exact-action UI 并保留手工复制按钮；不得恢复 silent fallback。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/vnext/test_analysis_domain.py -q`
- `python -m pytest tests/vnext/test_vnext_runtime.py -q`
- `python -m pytest tests/browser/vnext_http_integration.spec.js -q`
- `python -m pytest test_lazy_collapse_v11_fixes.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/real_browser_evidence.js`

#### 专项验证设计

审计报告原始验证要求：创建目标 relation 位于第 501 条后的 fixture；断言 exact 查询命中、无全局列表、网络失败不改变面板事实、审计 relation/revision 正确。

1. **验收门槛 1：** 目标 relation 位于旧列表第 501 条之后仍可精确命中。
2. **验收门槛 2：** 单次按钮操作无全局 pending-list 请求。
3. **验收门槛 3：** 网络失败/409 不改变 review_state、relation origin 或 draft 内容。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### UI-002：默认展开区域一次性批量请求超过服务端硬上限

| 字段 | 内容 |
|---|---|
| Severity | P1 |
| Evidence | Confirmed |
| Owner Skill | `fos-coverage-performance-ui` |
| Layer | Frontend / API / Code Detail |
| File | `web/assets/js/coverage_enhance.js`；`app/code_detail/vnext_service.py` |
| Function/Class | `CodeRegionLoader.loadInitialBatch`、Code Detail range validation |
| Code Location | `batchRegions.map(...)` 形成单 POST；服务端限制 ranges 和 logical lines |
| Root Cause | 前端和后端都有限制，但前端未按后端 capacity contract 分区。 |
| Current Impact | 单个大文件初始化可能 400；所有默认展开区域进入 error/collapsed，用户误以为懒加载失效。 |
| Scale Impact | 随待分析函数/区间数增长；并非仅视觉问题。 |
| Compatibility | 必须保持“默认待分析函数展开”语义；分批不能改变 region 顺序/身份。 |
| Action Type | `FIX` |
| Phase / Package | `Phase 4` / `PKG-13` |
| Dependencies | `API-002`, `OBS-001` |

#### 修改目标

关闭 `UI-002` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：客户端按 `max_ranges` 与 `max_logical_lines` 双约束分 batch，有限并发；每个 batch 独立重试，失败时按单 range GET 回退。**

#### 详细修改步骤

1. 从服务端合同或共享常量获取 `max_ranges/max_logical_lines`；客户端 batch builder 同时满足两个上限。
2. 多个 batch 使用有限并发，默认并发与现有 network chunk concurrency 分开配置。
3. 每 batch 保持 range 顺序和 identity；失败只重试该 batch，必要时退化为单 range GET。
4. 取消/restore default/页面切换时使用 generation/AbortController 丢弃 stale responses。
5. 大单 region 继续走 virtual range，不把 10k+ region 整体装入 initial batch。

#### 数据、兼容与回滚约束

- **原兼容结论：** 必须保持“默认待分析函数展开”语义；分批不能改变 region 顺序/身份。
- **Migration：** 无。
- **Rollback：** batch builder 可退回逐 range GET；保证 correctness 优先于吞吐。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest test_code_detail_api.py -q`
- `python -m pytest test_code_detail_service.py -q`
- `python -m pytest test_lazy_collapse_e2e_and_perf.py -q`
- `python -m pytest test_lazy_collapse_v11_fixes.py -q`
- `python -m pytest tests/browser/vnext_http_integration.spec.js -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/real_browser_evidence.js`
- `scripts/diagnostics/release_performance_ab.js`

#### 专项验证设计

审计报告原始验证要求：1001 regions、>20k logical lines、单 10k region 三组 fixture；断言请求分区、并发上限、结果顺序、取消与重试。

1. **验收门槛 1：** 1001 regions、>20k logical lines 时不存在超服务端 cap 的请求。
2. **验收门槛 2：** 结果顺序、默认展开集合与单请求语义一致。
3. **验收门槛 3：** 取消后 stale response 不写入 DOM/state。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### UI-004：Code Detail identity cache 的全局锁覆盖数据库查询

| 字段 | 内容 |
|---|---|
| Severity | P2 |
| Evidence | Confirmed |
| Owner Skill | `fos-coverage-performance-ui` |
| Layer | Service / Lock / DB |
| File | `app/code_detail/vnext_service.py` |
| Function/Class | `VNextCodeDetailService._identity` |
| Code Location | cache hit/validation 路径持有 `_cache_lock` 时读取 project data_version |
| Root Cause | 为验证 cache freshness，把 DB read 与 cache mutation 放在同一临界区。 |
| Current Impact | 并发浏览大文件时潜在串行化；单请求功能正确。 |
| Scale Impact | DB 延迟越高、并发越大，head-of-line blocking 越明显。 |
| Compatibility | 不能放松 scan/report/file/data_version identity 校验。 |
| Action Type | `FIX` |
| Phase / Package | `Phase 4` / `PKG-14` |
| Dependencies | `OBS-001` |

#### 修改目标

关闭 `UI-004` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：锁内读取候选 cache entry，释放锁后查询 version；再用 compare-and-swap 更新。对同 key 使用 singleflight，不同 key 并行。**

#### 详细修改步骤

1. 将 identity cache 全局锁临界区缩到纯内存 map 操作；数据库 data_version 查询在锁外执行。
2. 为同一 cache key 增加 singleflight/per-key lock，防止缓存 miss 时重复 DB/Sidecar work。
3. 不同 key 不共享同一互斥锁；CAS 更新时再次比较 version/key。
4. metrics 增加 lock wait/singleflight shared/miss DB calls。

#### 数据、兼容与回滚约束

- **原兼容结论：** 不能放松 scan/report/file/data_version identity 校验。
- **Migration：** 无。
- **Rollback：** 可回到旧 cache lock 实现；不影响 persistent data。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest test_code_detail_service.py -q`
- `python -m pytest test_concurrency.py -q`
- `python -m pytest tests/code_detail/test_phase2_core.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/perf_benchmark.py`

#### 专项验证设计

审计报告原始验证要求：注入 100ms DB latency，20/100 并发不同 key 与同 key 请求；测吞吐、锁等待、DB 次数和身份正确性。

1. **验收门槛 1：** 注入 100ms DB 延迟时不同 key 请求可以并行，不被全局锁串行。
2. **验收门槛 2：** 同 key 并发 miss 只执行一次昂贵 identity load。
3. **验收门槛 3：** cache identity 与 scan/data_version 一致，无 stale publish。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### COMPAT-001：上一生产数据库不能直接复制给当前 VNext 使用

| 字段 | 内容 |
|---|---|
| Severity | P1 |
| Evidence | Confirmed |
| Owner Skill | `fos-coverage-release-governance` |
| Layer | Database Compatibility |
| File | `scripts/upgrade/vnext_schema.sql`；`scripts/upgrade/migration_runner.py`；`app/db/repositories/*`；`app/config/runtime_config.py` |
| Function/Class | VNext runtime bootstrap 与 repositories；Legacy snapshot/migration runner |
| Code Location | VNext 读取 `coverage_projects/coverage_scans/coverage_files/coverage_lines/coverage_analysis_*`；legacy source 使用 `coverage_analysis/coverage_line_index/coverage_project_state` 等旧形态 |
| Root Cause | 当前是领域模型重构，不是同表加列升级；同名/近似业务概念具有不同主键、关系和状态权威。 |
| Current Impact | 直接复制路径不安全，可能启动即缺表/字段，或错误地把兼容表当权威。 |
| Scale Impact | 与规模无关；属于拓扑兼容问题。 |
| Compatibility | 明确不满足用户要求中的“直接复制后安全启动”。 |
| Action Type | `CONTRACT_AND_MIGRATION` |
| Phase / Package | `Phase 5` / `PKG-16` |
| Dependencies | `MIGRATION-001`, `MIGRATION-002`, `COMPAT-003` |

#### 修改目标

关闭 `COMPAT-001` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：旧库保持只读 source，创建全新空 VNext target，运行受控 migration、semantic gate、Candidate 验收后再切换配置/流量。**

#### 详细修改步骤

1. 明确产品合同：当前 VNext 对 v10 数据库的支持级别是 `YES WITH MIGRATION`，不是“直接指向旧库”。
2. 启动 preflight 检测 legacy schema；若 runtime_mode=vnext 指向旧库，必须 fail closed 并给出迁移命令/文档，不做自动原地 DDL。
3. Candidate 使用独立数据库；迁移完后配置才切 target。
4. 保留 previous runtime + untouched legacy DB 作为 rollback authority；禁止 current/candidate 同时写同一 DB。
5. 把 schema/semantic migration 与 cutover runbook 固化到 release evidence。

#### 数据、兼容与回滚约束

- **原兼容结论：** 明确不满足用户要求中的“直接复制后安全启动”。
- **Migration：** **必须迁移。**
- **Rollback：** 切回 previous code/config/database；Candidate 独立保留用于审计或废弃。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/vnext/test_legacy_migration_contract.py -q`
- `python -m pytest tests/vnext/test_runtime_config.py -q`
- `python -m pytest tests/release/test_release_readiness.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/legacy_compatibility_smoke.py`
- `scripts/diagnostics/final_read_only_verification.py`

#### 专项验证设计

审计报告原始验证要求：用 v10 只读副本执行 previous→candidate；核对 schema、项目、Scan、行、分析、状态、CURRENT、Job、provenance、semantic zero-loss，再做 rollback boot。

1. **验收门槛 1：** 旧库直接绑定 VNext 时得到明确拒绝而非半启动。
2. **验收门槛 2：** v10 backup → empty target rehearsal 成功且 Candidate 可读。
3. **验收门槛 3：** rollback 可启动 previous runtime 并读取 untouched legacy DB。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### MIGRATION-002：Legacy→VNext 迁移全量内存化、重复扫描并使用单大事务

| 字段 | 内容 |
|---|---|
| Severity | P1 |
| Evidence | Confirmed |
| Owner Skill | `fos-coverage-release-governance` |
| Layer | Migration / DB / Memory / Recovery |
| File | `scripts/upgrade/migration_runner.py` |
| Function/Class | `capture_legacy_snapshot`、`capture_legacy_semantic_snapshot`、`migrate_legacy`、`capture_vnext_snapshot` |
| Code Location | 全表 `fetchall` 到 lists；多次 dict/list copy；每项目重新过滤全 source；一个 target transaction；结果阶段两次计算 target semantic snapshot/hash |
| Root Cause | 迁移实现针对确定性 fixture/小库设计，缺少生产规模的 streaming、batch checkpoint 和 publication 隔离。 |
| Current Impact | 小库功能可能正确；本轮无真实 v10 库，不能测绝对资源。源码可确认重复 materialization 和长事务。 |
| Scale Impact | RSS 近似随源库全部 line/analysis facts 增长；目标 snapshot 再复制同量级数据；大事务增加 undo、锁、失败重做。 |
| Compatibility | 必须保留 raw provenance、semantic hash、幂等性、active legacy job 的人工决策语义。 |
| Action Type | `FIX_BLOCKER` |
| Phase / Package | `Phase 5` / `PKG-16` |
| Dependencies | `MIGRATION-001`, `JOB-001`, `JOB-002`, `OBS-001` |

#### 修改目标

关闭 `MIGRATION-002` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：按 project/file keyset 流式读取；每个 Candidate 项目或有界 batch 幂等提交并记录 migration checkpoint；所有目标仍不可发布，最终统一做语义/数量/哈希门禁后切换配置。target semantic snapshot 只计算一次并复用。**

#### 详细修改步骤

1. 把 legacy source 读取从全表 `fetchall` 改成 project/file/job keyset streaming；source connection 始终只读。
2. 每个 project 或有界 batch 在 target 形成幂等 migration checkpoint，记录 source cursor、semantic fragment hash、target counts、migration version。
3. 避免在 Python 中反复 `project_lines = [..]` 等全量过滤；使用按 project 查询/iterator。
4. target semantic snapshot 只生成一次供最终比较；大表 hash 使用稳定排序流式 hash。
5. 批次提交后 Candidate 仍不可发布；最终所有 project 完成后执行全局 semantic zero-loss、provenance、count、CURRENT mapping gate。
6. 故障恢复只从 checkpoint 继续；同 migration_id 二次运行必须 no-op/一致。

#### 数据、兼容与回滚约束

- **原兼容结论：** 必须保留 raw provenance、semantic hash、幂等性、active legacy job 的人工决策语义。
- **Migration：** YES：迁移 runner/checkpoint/ledger 升级；旧源只读。
- **Rollback：** 废弃/删除独立 Candidate target，旧 source/production 完全不动；绝不实现自动 VNext→legacy 反向降级。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/vnext/test_migration_runner.py -q`
- `python -m pytest tests/vnext/test_legacy_migration_contract.py -q`
- `python -m pytest tests/database/test_mariadb_compatibility_contract.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/mysql_vnext_integration.py`
- `scripts/diagnostics/data_hash_gate.py`
- `scripts/upgrade/run_verified_backup_rehearsal.py`

#### 专项验证设计

审计报告原始验证要求：脱敏生产规模副本；记录 rows/s、peak RSS、transaction time、resume phase、semantic hash、两次运行幂等、kill/restart。

1. **验收门槛 1：** 脱敏生产规模副本迁移 semantic hash/关键 counts/provenance 100% 守恒。
2. **验收门槛 2：** kill/restart 可从 durable cursor 继续且无重复业务行。
3. **验收门槛 3：** peak RSS 与 batch size 相关，不再需要同时容纳全源 snapshot+全 target snapshot。
4. **验收门槛 4：** 二次运行 idempotent，旧 source 未发生任何写。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### COMPAT-004：Legacy inherit CLI 行为已被直接退休

| 字段 | 内容 |
|---|---|
| Severity | P2 |
| Evidence | Confirmed |
| Owner Skill | `fos-coverage-change-review` |
| Layer | CLI / Behavior Compatibility |
| File | `app/compat/legacy_runtime_impl.py` |
| Function/Class | CLI command dispatch for `inherit` |
| Code Location | `cmd == 'inherit'` 输出 retired 错误并 `sys.exit(2)` |
| Root Cause | 自动继承已迁入 VNext Scan Import，但入口迁移没有兼容翻译层。 |
| Current Impact | 依赖旧命令的脚本/操作手册立即失败。 |
| Scale Impact | 非性能型；可能导致人工改走未经审计的替代流程。 |
| Compatibility | 明确行为变化：旧 `A -> B -> C` 变为 `A -> error`。 |
| Action Type | `DECIDE_THEN_FIX` |
| Phase / Package | `Phase 5` / `PKG-17` |
| Dependencies | `COMPAT-001` |

#### 修改目标

关闭 `COMPAT-004` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：兼容命令仅做参数校验和 VNext Scan Import 提交/说明，返回新的 job/scan identity；无法安全映射时 fail closed 并输出机器可读迁移原因。**

#### 详细修改步骤

1. 先扫描仓库、部署脚本、计划任务、操作手册对 `inherit --from --to` 的真实调用；没有 fresh usage evidence 前不直接重新实现旧语义。
2. 若仍有生产调用：legacy CLI 只做参数解析/身份校验并提交 VNext Scan Import；返回 machine-readable job/scan identity。
3. 无法从旧参数唯一确定 project/repository/predecessor 时 fail closed，要求显式新参数，不猜测。
4. 若已无调用：保留退出 2，但补充 migration detector、runbook 和正式 retirement evidence，Issue 可按“安全退役”关闭。
5. 无论哪种路径，都不得恢复独立第二套 inheritance writer。

#### 数据、兼容与回滚约束

- **原兼容结论：** 明确行为变化：旧 `A -> B -> C` 变为 `A -> error`。
- **Migration：** 无数据库迁移；可能需运维脚本迁移。
- **Rollback：** previous CLI 仅与 previous runtime/DB 配套；candidate 不恢复 legacy writer。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/vnext/test_architecture_audits.py -q`
- `python -m pytest tests/vnext/test_scan_import_lifecycle.py -q`
- `python -m pytest test_enhance_coverage.py -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/legacy_retirement_audit.py`
- `scripts/diagnostics/runtime_legacy_dependency_audit.py`

#### 专项验证设计

审计报告原始验证要求：搜索部署脚本/计划任务；运行旧命令 fixture，确认不会产生跨分支或错误 predecessor 继承。

1. **验收门槛 1：** 旧命令不会绕开 VNext fixed predecessor/branch/identity/fencing。
2. **验收门槛 2：** 零调用场景有可审计 retirement evidence；有调用场景所有调用点迁移完成。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### COMPAT-005：历史静态报告可能仍绑定 legacy API/asset contract

| 字段 | 内容 |
|---|---|
| Severity | P2 |
| Evidence | Strong Risk |
| Owner Skill | `fos-coverage-release-governance` |
| Layer | API / Static Asset / Deployment Compatibility |
| File | `app/api/application.py`；root compatibility assets；`web/assets/*`；report generation/injection code |
| Function/Class | VNext route registration、static report asset injection |
| Code Location | VNext route table只暴露 canonical endpoints；没有完整 legacy aliases |
| Root Cause | API 与静态制品一起演进，但历史报告没有 asset/version negotiation。 |
| Current Impact | 是否已有历史报告依赖旧合同需扫描生产报告目录；本轮未取得。 |
| Scale Impact | 历史报告越多，重生成/兼容成本越高。 |
| Compatibility | 直接影响历史数据浏览和回滚窗口。 |
| Action Type | `MEASURE_THEN_FIX` |
| Phase / Package | `Phase 5` / `PKG-17` |
| Dependencies | `API-002`, `COMPAT-002` |

#### 修改目标

关闭 `COMPAT-005` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：报告写入 immutable asset/API version；VNext 在受控期限提供 compatibility proxy，或批量重注入/重生成；建立资产 inventory 与退役日期。**

#### 详细修改步骤

1. 先对生产/备份报告目录做只读 inventory：HTML 版本、script src/hash、API endpoints、report_id/scan metadata。
2. 定义 compatibility window 和版本矩阵：哪些历史报告可重注入 canonical asset，哪些必须走 proxy，哪些只允许 previous server。
3. 新报告写入 immutable asset/API contract version 与 release identity。
4. VNext compatibility proxy 只做 DTO/route 适配，不拥有独立业务语义；设置明确退役日期/使用计数。
5. 批量重注入/重生成前保留原报告副本和 manifest。

#### 数据、兼容与回滚约束

- **原兼容结论：** 直接影响历史数据浏览和回滚窗口。
- **Migration：** 静态报告/asset 迁移可能需要；数据库不一定。
- **Rollback：** 旧报告与 previous API 一起保留；重注入采用新目录或可恢复备份。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/vnext/test_registry_and_api_contract.py -q`
- `python -m pytest tests/browser/coverage_real_browser.spec.js -q`
- `python -m pytest tests/browser/vnext_http_integration.spec.js -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/sidecar_registry_audit.py`
- `scripts/diagnostics/legacy_compatibility_smoke.py`

#### 专项验证设计

审计报告原始验证要求：从生产报告目录抽样旧版本 HTML，解析 endpoint/assets，分别对 previous 与 candidate 做真实浏览器验收。

1. **验收门槛 1：** 抽样历史报告在其声明支持路径中真实浏览器可用。
2. **验收门槛 2：** 新报告携带可追溯 asset/API version。
3. **验收门槛 3：** compat proxy 使用量可观测，达到退役条件后可安全删除。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### UI-003：Progress 页面文件与模块表格全量 DOM 渲染

| 字段 | 内容 |
|---|---|
| Severity | P2 |
| Evidence | Strong Risk |
| Owner Skill | `fos-coverage-performance-ui` |
| Layer | Frontend / DOM |
| File | `web/assets/js/coverage_progress.js` |
| Function/Class | `renderFileTable`、`renderTeamTable`、`renderTable` |
| Code Location | 对全部 rows 使用 `map(...).join('')` 并一次写入 `innerHTML`；每个团队预生成全部 module subrows |
| Root Cause | 数据层全量返回，UI 也按全量静态报表思路渲染。 |
| Current Impact | 当前文件数未知，无法确认生产是否已经卡顿。 |
| Scale Impact | HTML 字符串、DOM node、event target lookup 和 layout 成本随文件/模块数线性增长；大表可能阻塞主线程。 |
| Compatibility | 分页/虚拟化必须保持筛选、展开、详情跳转和 ownership 字段。 |
| Action Type | `MEASURE_THEN_FIX` |
| Phase / Package | `Phase 5` / `PKG-15` |
| Dependencies | `API-001`, `OBS-001` |

#### 修改目标

关闭 `UI-003` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：服务器 cursor pagination；浏览器只保留当前页/窗口，团队子行按展开时构建。**

#### 详细修改步骤

1. Progress 文件表只渲染当前 page/window；切页释放旧 row DOM 与引用。
2. 团队模块子行在用户展开团队时才构建，折叠可释放子行。
3. 如一页仍较大，加入简化 virtualization；避免复杂虚拟表先于分页。
4. 搜索/筛选优先服务端查询或当前页限定，明确 UI 语义。
5. 加入 DOM nodes、long task、heap telemetry。

#### 数据、兼容与回滚约束

- **原兼容结论：** 分页/虚拟化必须保持筛选、展开、详情跳转和 ownership 字段。
- **Migration：** 无。
- **Rollback：** 保留分页，虚拟化可单独关闭；不要回退到全量 DOM。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/browser/coverage_real_browser.spec.js -q`
- `python -m pytest tests/browser/vnext_http_integration.spec.js -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/synthetic_dom_microbenchmark.js`
- `scripts/diagnostics/release_performance_ab.js`

#### 专项验证设计

审计报告原始验证要求：100/1k/10k 文件，测 DOM nodes、long tasks、JS heap、首次交互时间和滚动 p95。

1. **验收门槛 1：** 10k 文件项目首次 DOM 节点数受 page/window 上限约束。
2. **验收门槛 2：** 首次交互和滚动无持续长任务堆积；heap 在切页后可回落。
3. **验收门槛 3：** 分页/筛选结果与服务端总数一致。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

### MAINT-001：大型 active compatibility implementation 仍是第二业务实现

| 字段 | 内容 |
|---|---|
| Severity | P2 |
| Evidence | Strong Risk |
| Owner Skill | `fos-coverage-change-review` |
| Layer | Architecture / Maintainability / Performance Consistency |
| File | `app/compat/legacy_runtime_impl.py`；`app/legacy_runtime.py`；根入口与兼容资产 |
| Function/Class | legacy HTTP、DB、inject、incremental、progress、jobs、CLI 等多类能力 |
| Code Location | 兼容实现约数千行，仍可完整运行 legacy server/业务流程 |
| Root Cause | 迁移期通过搬迁文件名降低入口复杂度，但没有持续把业务 owner 收敛到 canonical services。 |
| Current Impact | 当前无法证明已发生具体生产瓶颈；维护成本和回归矩阵已经存在。 |
| Scale Impact | 功能继续增加时，重复实现、测试组合和 release 兼容负担非线性增长。 |
| Compatibility | 不能一次删除 legacy；它仍是 previous/rollback 与旧数据库读取路径。 |
| Action Type | `INCREMENTAL_REFACTOR` |
| Phase / Package | `Phase 6` / `PKG-18` |
| Dependencies | `COMPAT-004`, `COMPAT-005`, `API-003` |

#### 修改目标

关闭 `MAINT-001` 的根因，同时保持审计报告中定义的兼容/数据语义边界。原审计推荐为：**方案 A：按能力建立 retirement matrix。先让 legacy adapter 委托 canonical auth/static/utility，再逐步只保留旧参数/DTO 转换；每移除一个 owner 都保留 contract test 和 previous release。**

#### 详细修改步骤

1. 建立 `legacy-retirement matrix`：server/auth、jobs、progress、export、incremental、inject、assets、CLI 等能力逐项标记 KEEP/DELEGATE/RETIRE。
2. 优先把 legacy 中无状态 utility、auth/static、job/progress/export 路径委托 canonical service；legacy 层只保留旧参数/DTO/route 翻译。
3. 每迁移一个能力都运行 authoritative mutation owner audit，保证数据库写者只剩一个。
4. 禁止一次删除 339KB compatibility implementation；每步单独 commit，可独立 revert。
5. 当某能力无生产调用、contract test 通过、previous release 已保留时再删除旧 owner。
6. 更新 docs/architecture/legacy-retirement.md 与 source ownership map。

#### 数据、兼容与回滚约束

- **原兼容结论：** 不能一次删除 legacy；它仍是 previous/rollback 与旧数据库读取路径。
- **Migration：** 分能力决定；不可同时改 schema、API 和 UI 全部层。
- **Rollback：** 每个能力单独 revert；previous release 保留完整 legacy 实现直到 acceptance window 结束。
- 修改期间不得改写 previous production authoritative data；所有测试写入 disposable/Candidate 环境。

#### Targeted Tests（禁止全量）

- `python -m pytest tests/vnext/test_architecture_audits.py -q`
- `python -m pytest tests/vnext/test_legacy_telemetry.py -q`
- `python -m pytest tests/browser/vnext_http_integration.spec.js -q`

现有诊断/Benchmark 工具（执行前先用 `--help` 确认当前参数合同）：
- `scripts/diagnostics/canonical_ownership_audit.py`
- `scripts/diagnostics/legacy_retirement_audit.py`
- `scripts/diagnostics/runtime_legacy_dependency_audit.py`

#### 专项验证设计

审计报告原始验证要求：source ownership audit、重复路由/SQL/状态枚举扫描、canonical-vs-compat contract matrix；确认每个业务规则只有一个 owner。

1. **验收门槛 1：** 每个持久化业务语义只有一个 authoritative writer/owner。
2. **验收门槛 2：** compat 层只做适配，不拥有独立 CURRENT/analysis/job/progress 规则。
3. **验收门槛 3：** 每个退休项有 fresh usage evidence、contract test、rollback path。

#### Issue 关闭条件

- 修改提交必须绑定新的 exact Commit SHA。
- 上述 targeted tests 全部通过；若某项因环境缺失未执行，Issue 不得标 Verified。
- 所有验收门槛满足并保存 evidence；Strong Risk 类型还必须有 fixed-workload 实测后才能决定是否继续实施完整优化。
- 回归检查必须覆盖当前 Issue 所依赖的上游 Issue 合同。

## 7. 分阶段验证矩阵

| 验证域 | 必跑 targeted tests / evidence | 主要 Issue | 通过标准 |
|---|---|---|---|
| Inheritance correctness | `tests/vnext/test_inheritance_engine.py`, `test_deterministic_inheritance_corpus.py`, R01-R83 audit | INHERIT-001/002/003, UI-001 | decision/fingerprint 语义一致；无错误继承 |
| Scan durability | `tests/vnext/test_scan_import_lifecycle.py`, kill/restart | JOB-001/002/004 | checkpoint 可恢复；CURRENT 不提前变化；stale writer 被 fence |
| Derived state | progress + transaction tests | DB-001 | 保存后 derived ready，pending conservation 通过 |
| DB compatibility | SQLite + MariaDB 5.5 targeted tests | DB-003/004, MIGRATION-* | bind/index/schema 兼容，语义守恒 |
| Code Detail | service/API + Playwright | UI-002/004, CACHE-001 | request cap/identity/cache/DOM 正确 |
| Progress UI | real VNext HTTP browser | API-001/002, UI-003 | payload/DOM 有界，详情/分页正确 |
| Release identity | release tests + no-.git artifact negative cases | COMPAT-002 | 正确制品可启动；篡改/缺失 fail closed |
| Legacy migration | disposable MariaDB + semantic gate | MIGRATION-001/002, COMPAT-001 | Empty Target、zero-loss、idempotent、restart/rollback |
| Legacy retirement | architecture audits + usage inventory | COMPAT-004/005, MAINT-001 | 单一 authoritative owner，兼容窗口可退役 |

## 8. Benchmark / Workload 设计

性能问题统一采用固定 workload，不使用“感觉更快”判断。建议建立以下层级：

| Workload | 规模 | 用途 |
|---|---:|---|
| W1 Small | 1k lines / 10 files / 100 relations | correctness + 快速回归 |
| W2 Medium | 100k lines / 1k files / 10k relations | 常规规模 |
| W3 Large | 1M lines / 10k files / 100k relations | 内存、DB、分页、Job |
| W4 Very Large Code Detail | 100k+ 单文件 / 1000+ regions | Lazy Collapse / DOM / cache |
| W5 Migration | 脱敏 v10 生产规模副本 | RSS、rows/s、事务、resume、semantic zero-loss |

每次 performance evidence 至少记录：Commit SHA、Python/DB/browser 版本、CPU/RAM、workload identity/hash、cold/warm cache、wall time、p50/p95（重复运行时）、peak RSS、SQL count/rows、Git subprocess、payload bytes、DOM nodes。

## 9. 数据库 / Migration 验证方案

### 9.1 必须使用四套数据库角色

1. `legacy_source_rehearsal`：从 v10 backup 恢复，只读。
2. `vnext_target_disposable`：Empty Target，用于 migration/restart/fault injection。
3. `candidate_target_final`：最终 Candidate，只在正式迁移阶段写。
4. `production_legacy`：本轮修改/验证全过程不写，直到 cutover 冻结点。

### 9.2 迁移必须验证

- Empty Target preflight 在首个业务 DDL/DML 前执行。
- source/target runtime fingerprint 不同。
- migration ledger/checkpoint 可重复、可恢复。
- projects/scans/files/lines/analysis/line-links/jobs/provenance 数量与语义 hash 守恒。
- unknown historical Git identity 继续 unknown/unverified，不从当前 HEAD 伪造。
- final Candidate 验证阶段只读。
- rollback 使用 untouched v10 DB + previous release，不对 VNext target 做“降级迁移”。

## 10. 浏览器与 API 验证方案

必须使用 canonical `web/assets/*` + 真实 VNext HTTP server。

浏览器证据至少覆盖：

- Code Detail 默认折叠、默认展开、单区间展开、展开全部、恢复默认、取消/重展开。
- 1001 regions 与 >20k logical lines 的 initial batching。
- inherited pending：confirm/edit-confirm/reject/undo，含 409 stale revision。
- Progress 首页 1M pending 的分页/载荷边界。
- Progress Details 顶层 DTO。
- 10k 文件项目 DOM/window。
- cache eviction 后 draft 不丢、内容 identity 不串 Scan。
- console error = 0（已知明确 allowlist 除外）；stale response 不覆盖新状态。

## 11. 推荐实际开发顺序

`OBS-001 → MIGRATION-001 → COMPAT-002 → API-002 → PERF-001 → DB-002 → DB-003 → COMPAT-003 → DB-001 → JOB-001 → JOB-002 → JOB-004 → JOB-003 → INHERIT-001 → INHERIT-003 → API-003 → DB-004 → RUNTIME-001 → INHERIT-002 → API-001 → UI-001 → UI-002 → UI-004 → CACHE-001 → MIGRATION-002 → COMPAT-001 → COMPAT-004 → COMPAT-005 → UI-003 → MAINT-001`

### 11.1 为什么不是先做所有 P1

- `OBS-001` 虽是 P3，但它是 Strong Risk 性能验证的证据基础，应提前。
- `MIGRATION-001` 是唯一 P0，必须最先阻断危险路径。
- `COMPAT-002` 决定后续 Candidate artifact 能否用 exact SHA 验收，必须早于 release rehearsal。
- `JOB-001/002` 会改变 Scan Import 执行粒度，因此应早于 `MIGRATION-002` 和大规模 inheritance memory 优化。
- `API-001` 依赖 `DB-001` 的 fresh derived state；否则分页只是把全量 fallback 的压力换一种形式。
- `MAINT-001` 最后做，避免在业务主链仍变化时大规模搬迁 compatibility code。

## 12. 每个 Package 的提交与证据模板

```text
Package: PKG-xx
Issues: ISSUE-xxx, ...
Base Commit:
Result Commit:
Changed Files:
Behavior Contract Changed: YES/NO
Schema/Config Migration: YES/NO
Targeted Tests:
Benchmark / Workload:
Correctness Result:
Performance Result:
Compatibility Result:
Rollback Result:
Missing Evidence:
Issue Status: Open / Measurement Needed / Verified
Full Test Suite Executed: NO
```

## 13. 最终 Release / Gate 验收

所有修改完成后，不直接依据“30 个 Issue 都提交了”宣布可以发布。最终至少满足：

1. P0 = 0；P1 Open/Measurement Needed = 0。
2. 当前新 SHA 重新执行一次独立源码复检，确认旧问题未回归。
3. Inheritance R01-R83 语义证据与 adversarial cases 通过。
4. Scan Import kill/restart/fencing/CURRENT publication targeted integration 通过。
5. 真实 MariaDB disposable rehearsal 完成，v10 backup → Empty Target semantic zero-loss。
6. 无 `.git` Candidate release artifact 的 release identity 正/负例通过。
7. Code Detail / Progress / inheritance review 真实 Chromium evidence 通过。
8. fixed workload performance A/B 证明 P1 性能根因已消除；Strong Risk 已测量并作出明确接受/修复决策。
9. previous release + untouched v10 DB 的 rollback boot rehearsal 通过。
10. **本计划仍不执行 Full Test Suite**；若组织 Release Gate 最终要求全量，则单独进入 `需要后续 Full Verification`，不得在本轮偷跑。

## 14. 完成后的目标状态

完成本计划后，期望把当前审计结论从 **C：存在关键性能/兼容问题，不建议进入下一阶段**，提升到至少：

> **B：核心 P0/P1 已关闭，真实数据库/浏览器/固定 workload 验证通过后可进入下一阶段。**

是否最终达到 **A：可以直接进入下一阶段**，必须由修改后的 exact Commit 再做一次独立审计决定，本计划本身不预先承诺。

## 15. Evidence Boundary

### 本方案已实际依据

- 审计报告中 30 个稳定 Issue ID、Severity、Evidence、Owner、文件/函数、推荐方案与验证要求。
- 当前审计 Commit 的仓库树和现有 targeted tests/diagnostics 路径。
- 当前最新 FOS Coverage Maintainer / Change Review / Runtime Reliability / Performance UI / Release Governance 职责与不变量。

### 本方案没有声称已完成

- 没有修改仓库源代码。
- 没有执行任何 targeted test 或 benchmark。
- 没有取得 v10 真实数据库副本、production schema/config/inventory。
- 没有执行真实 Chromium、MariaDB migration rehearsal 或 rollback。
- 因此本文件是 **开发与验证方案**，不是修复完成证明。

## 16. Plan Identity

```text
Repository: Chary-yu/fos_coverage_tool
Audit Source Branch: main
Audit Source Commit SHA: b07d7ee346a5c09169bc125145c0d5bf2547ed30
Previous Compatible Baseline: v10 (exact SHA/schema snapshot pending)
Audit Source Report: FOS_Coverage_Performance_Compatibility_Audit_b07d7ee_20260822.md
Plan Date: 2026-08-22
Issues Planned: 30
Skills Used: fos-coverage-maintainer, fos-coverage-change-review, fos-coverage-runtime-reliability, fos-coverage-performance-ui, fos-coverage-release-governance
Full Test Suite Executed: NO
```
