# FOS Coverage VNext：Gate 1 ～ Gate 3 一轮重构详细开发与验证方案

> 基线仓库：`Chary-yu/fos_coverage_tool`
> 基线分支：`main`
> 基线提交：`7baca93bb141755febb5ebf641d375c5a4d91441`
> 基线提交信息：`fix(incremental): address review findings`
> 基线时间：2026-08-20 14:15:36 +08:00
> 方案目标：在**不升级 CentOS / Python / MariaDB / Nginx** 的前提下，一轮完成 VNext 的架构成型、核心数据模型重构、全部现有核心能力迁移，并为 Gate 4 的 Candidate 全链路验收与迁移演练做好准备。

---

## 0. 方案结论

本轮不从零重写。

当前 GitHub 最新代码已经完成了一部分模块化与可靠性基础建设，包括：

- `app/db/connection_pool.py`
- `app/db/manager.py`
- `app/jobs/bounded_executor.py`
- `app/jobs/service.py`
- `app/progress/file_state_service.py`
- `app/progress/service.py`
- `app/inject/parse_once.py`
- `app/inject/service.py`
- `app/incremental/path_index.py`
- `app/incremental/service.py`
- `app/code_detail/sidecar_store.py`
- `app/code_detail/overlay_cache.py`
- `app/config/runtime_config.py`
- `app/release_identity.py`
- `app/upgrade/lifecycle.py`
- `web/assets/...`
- `scripts/diagnostics/...`
- `scripts/upgrade/...`

因此 Gate 1 ～ Gate 3 的正确方向是：

> **保留已经正确的基础能力，完成 canonical ownership，逐步把仍在 root 单体文件中的真实业务实现迁入 `app/`，最终让 root 文件仅保留 CLI / 兼容入口。**

而不是重新建立一套与现有 `app/` 平行的新架构。

本轮的内部推进方式：

```text
Gate 1：架构归位 / 唯一实现
        ↓
Gate 2：最终数据模型 / 无损迁移
        ↓
Gate 3：全部现有核心能力迁入新架构
        ↓
Gate 4：Candidate 全链路验收、真实迁移演练
        ↓
Gate 5：正式生产切换
```

Gate 1 ～ Gate 3 可以属于一个 VNext 开发周期，但必须保持三个明确验收闸门，不能把三者合并成一次“代码写完即通过”。

---

# 1. 固定约束

## 1.1 环境不升级

本轮明确保持：

```text
CentOS      7
Python      3.6.8
MariaDB     5.5.64
Nginx       1.26.1
systemd     保持现状
```

因此所有新代码必须：

- Python 3.6.8 可编译、可运行；
- 不使用 `dataclasses` 原生依赖；
- 不使用 `match/case`；
- 不使用 `:=`；
- 不使用 `list[str]` / `dict[str, ...]` 等 Python 3.9 泛型语法；
- 不使用 `X | None` 类型联合语法；
- 不依赖 MySQL 8 / MariaDB 新特性；
- Schema 不使用 JSON 类型、窗口函数、CTE、生成列等新版本依赖；
- 保留当前标准库 HTTP 服务形态，不为了重构引入 Flask/FastAPI 等新运行时框架。

当前仓库已提供 `requirements-py36.txt`，明确使用 `PyMySQL==0.10.1`，继续沿用这一兼容基线。

## 1.2 Current 生产不参与 Gate 1 ～ Gate 3 写操作

Gate 1 ～ Gate 3 开发期间：

```text
Current
/home/zcyu/coverage
DB: coverage
API: 127.0.0.1:9528
```

保持生产运行。

所有开发、Schema 创建、数据迁移、功能验证必须落到：

```text
Candidate
/home/zcyu/coverage_candidate
DB: coverage_candidate
API: 独立端口，例如 127.0.0.1:19528
```

禁止 Candidate 在验证期写生产数据库 `coverage`。

## 1.3 不破坏的业务合同

以下属于本轮不可破坏的系统合同：

1. Confirmed 分析必须保存被确认代码块覆盖的**物理源码行**；
2. Draft 仍计入待分析；
3. 成功写分析后必须推进项目权威 `data_version`；
4. 派生统计只能加速读取，不能覆盖权威 analysis / line-index 事实；
5. 保存后待分析数量必须刷新；
6. 页面重新打开/恢复前台时不能长期展示过期数量；
7. Background Job 必须可持久化，终端关闭不影响任务；
8. Code Detail 的 Lazy Collapse 必须继续支持：取消、重试、重新展开、无重复 DOM、未提交编辑不丢失；
9. 显式 `report_id` 必须 fail closed，不得跨 Report 猜测 source/cache；
10. 新 Sidecar 必须继续读取已支持的历史格式；
11. 多仓库相同相对路径必须保持 namespace 隔离；
12. Git ownership 必须以 `oldgit..newgit` 最终新增行和目标 commit 的真实 blame 为准；
13. `saved reviewer > suggested reviewer > blank`；
14. 不允许新旧两套业务实现长期同时活动。

---

# 2. 当前最新代码架构评估

## 2.1 当前代码已经不是纯单体

GitHub `7baca93` 已有 `app/`、`web/`、`scripts/` 分层。

但当前结构本质上仍处于“**新模块已经存在并部分运行，旧 root 仍承担大量真实业务**”的过渡态。

### 已经具备较好基础，应保留并继续强化

| 当前模块 | 当前价值 | VNext 处理 |
|---|---|---|
| `app/db/connection_pool.py` | Python 3.6 兼容连接池 | 保留，作为唯一连接池 |
| `app/inject/parse_once.py` | Parse Once + SourceContext + line index | 保留，继续作为解析主路径 |
| `app/incremental/path_index.py` | exact/normalized/unique suffix + fail-closed | 保留，作为统一路径解析 |
| `app/incremental/service.py` | 缓存 path-keyed lookup index | 保留并扩大职责边界 |
| `app/code_detail/sidecar_store.py` | Sidecar v2 + v1 fallback | 保留，继续维护历史兼容 |
| `app/code_detail/overlay_cache.py` | Analysis overlay cache | 保留 |
| `app/jobs/bounded_executor.py` | 有界并发、任务状态 | 保留作为执行引擎 |
| `app/progress/file_state_service.py` | derived state + authoritative fallback | 保留概念，SQL 后续下沉 |
| `app/config/runtime_config.py` | 无副作用配置加载 | 保留并成为唯一配置入口 |
| `app/release_identity.py` | release manifest / hash | 保留 |
| `app/upgrade/lifecycle.py` | freeze/drain/start/rollback lifecycle | 保留，为 Gate 4/5 服务 |
| `web/assets/*` | 目标 canonical web assets | 设为唯一前端源文件 |

### 当前只是 facade / adapter，需要补齐

| 当前模块 | 当前问题 | Gate 目标 |
|---|---|---|
| `app/api/server.py` | 只负责创建 HTTPServer，路由/handler 仍在 root | 形成完整 API 层 |
| `app/db/manager.py` | 连接边界存在，但真实历史 CRUD 仍由 root legacy class 提供 | 形成 Repository 层 |
| `app/jobs/service.py` | Service 很薄，真实 recovery/持久化仍分散 | 形成唯一 Job 生命周期 owner |
| `app/progress/service.py` | 只是 facade | 成为 progress 业务入口 |
| `app/inject/service.py` | 只是 parse_once facade | 承担 inject orchestration |
| `app/incremental/service.py` | 主要只有 path resolving | 接管增量核心 orchestration |

### 当前仍应迁出的 root 业务实现

```text
enhance_coverage.py      ~336 KB
coverage_check.py         ~74 KB
source_reader.py          ~42 KB
code_detail_service.py    ~31 KB
code_region.py            ~11 KB
```

其中 `enhance_coverage.py` 目前仍然：

- 定义 `_LegacyDatabaseManager`；
- 绑定 `LegacyManagerAdapter`；
- 定义完整 `CoverageHTTPRequestHandler`；
- 定义 Report Registry 逻辑；
- 定义 `inject_coverage_report()`；
- 定义 Background Job recovery；
- 定义 server bootstrap；
- 直接负责多个业务子系统的组合。

`coverage_check.py` 虽已使用 `IncrementalService` 做路径解析，但仍直接承担：

- Git diff；
- Git log；
- Git blame；
- 新增行提取；
- LCOV 匹配；
- ownership；
- developer tasks；
- report 构造等大量职责。

因此 VNext 的核心不是“创建 app/”，而是：

> **让 app/ 成为真正唯一实现，让 root 退出真实业务所有权。**

## 2.2 当前还存在 canonical asset 漂移风险

当前 tree 中 root 与 `web/assets` 部分同名文件 SHA 不一致：

```text
coverage_enhance.js
coverage_enhance.css
incremental_developer_tasks.js
```

而当前 `scripts/diagnostics/canonical_ownership_audit.py` 明确把 `web/assets/...` 视为 canonical source，root 只应作为兼容副本。

Gate 1 必须清掉这种“两个可编辑源”的状态。

## 2.3 当前 CI 不能单独作为 Python 3.6 运行通过证明

当前 GitHub CI：

- 主测试矩阵：Python 3.10 / 3.12；
- 单独有 Python 3.6 compatibility job；
- py36 job 主要做 compile + 少量兼容测试；
- real browser E2E 需要显式变量开启。

因此 VNext 的正式 Gate 不能只看 GitHub 主 CI 是否绿灯；必须在目标主机 Python 3.6.8 上执行真实 runtime compile / targeted tests。

---

# 3. Gate 1：架构成型与 Canonical Ownership

## 3.1 Gate 1 目标

Gate 1 **不追求业务功能变化，也不改最终业务语义**。

只做一件事：

> 把当前已经部分模块化的工程，整理成一个“每项能力只有一个真实实现”的目标架构。

Gate 1 完成后应具备：

```text
CLI / bootstrap
      ↓
API / Services
      ↓
Repositories
      ↓
MariaDB

业务能力：
Code Detail / Incremental / Inject / Jobs / Progress / Reports
各自只有一个 canonical implementation
```

## 3.2 Gate 1 目标目录

建议基于现有 `app/` 继续演进为：

```text
app/
├── __init__.py
├── bootstrap.py
│
├── api/
│   ├── server.py
│   ├── handler.py
│   ├── router.py
│   ├── auth.py
│   ├── serialization.py
│   └── endpoints/
│       ├── analysis.py
│       ├── progress.py
│       ├── code_detail.py
│       ├── incremental.py
│       ├── jobs.py
│       ├── reports.py
│       ├── health.py
│       └── release.py
│
├── config/
│   └── runtime_config.py
│
├── db/
│   ├── connection_pool.py
│   ├── manager.py
│   ├── transaction.py
│   └── repositories/
│       ├── analysis_repository.py
│       ├── line_index_repository.py
│       ├── project_state_repository.py
│       ├── file_state_repository.py
│       └── job_repository.py
│
├── services/
│   ├── analysis_service.py
│   ├── project_service.py
│   └── progress_service.py
│
├── code_detail/
│   ├── service.py
│   ├── source_reader.py
│   ├── code_region.py
│   ├── sidecar_store.py
│   └── overlay_cache.py
│
├── incremental/
│   ├── service.py
│   ├── path_index.py
│   ├── git_diff.py
│   ├── blame.py
│   ├── lcov.py
│   └── report.py
│
├── inject/
│   ├── service.py
│   ├── parse_once.py
│   └── directory_signature.py
│
├── jobs/
│   ├── service.py
│   ├── bounded_executor.py
│   └── excel_streaming.py
│
├── reports/
│   ├── registry.py
│   └── identity.py
│
├── release_identity.py
└── upgrade/
    └── lifecycle.py

web/
├── assets/
│   ├── css/
│   └── js/
└── templates/
```

不强制一次创建所有空目录；只有真正有明确 owner 的模块才创建，避免形式化拆目录。

## 3.3 Gate 1 开发工作包

### G1-01：锁定基线与建立“现有能力 → owner”清单

先建立一份机器可读/人工可审阅映射：

```text
capability
current_owner
current_entrypoints
target_owner
compatibility_entrypoint
migration_status
```

至少覆盖：

- DB CRUD；
- Analysis；
- Line Index；
- Project State；
- Progress；
- Background Job；
- Export；
- Code Detail；
- Sidecar；
- Report Registry；
- Inject；
- Incremental；
- Git Diff / Blame；
- Path Mapping；
- Release Identity；
- Upgrade write freeze；
- Web Assets。

**完成标准：**任何业务能力都能指出唯一目标 owner。

### G1-02：配置与 Bootstrap 统一

当前已有 `app/config/runtime_config.py`，继续使用它作为唯一配置读取器。

开发要求：

1. `enhance_coverage.py::load_config()` 改为兼容调用，不再自己维护默认/合并逻辑；
2. 全部 runtime path 从配置或 bootstrap context 提供；
3. 禁止新增 `/home/zcyu/coverage`、`export0810`、9528 等硬编码；
4. Candidate 配置明确指向 Candidate DB / runtime / reports / logs；
5. `bootstrap.py` 统一组装：
   - config；
   - DB pool；
   - repositories；
   - services；
   - job executor；
   - code detail service；
   - API handler。

### G1-03：数据库访问层归位

当前 `app/db/manager.py` 只有连接边界，真实历史方法仍从 `_LegacyDatabaseManager` 动态绑定。

Gate 1 要把业务 SQL 分到 Repository：

```text
AnalysisRepository
LineIndexRepository
ProjectStateRepository
FileStateRepository
JobRepository
```

原则：

- Repository 只做 SQL / row mapping；
- Service 做业务事务；
- API 不直接 SQL；
- Job 不直接拼 SQL；
- Code Detail 不直接知道具体 SQL；
- `DatabaseManager` 只负责 connection / transaction / health；
- `LegacyManagerAdapter` 在 Gate 1 可以暂留，但只做兼容 shim；
- Gate 3 结束时业务 runtime 不得再依赖 legacy methods。

### G1-04：统一事务边界

新增或明确 transaction helper：

```text
with transaction_manager.transaction() as conn:
    ...
```

必须覆盖下列原子操作：

- 保存 Draft；
- Confirm；
- block physical lines 写入；
- data_version 推进；
- derived readiness 失效；
- line-index 同步；
- Scan 导入（Gate 2/3）。

一个业务事务不能在多个 repository 自己 commit。

### G1-05：API 层从 root 迁出

保留 stdlib `ThreadingHTTPServer`，不换框架。

将 root `CoverageHTTPRequestHandler` 拆成：

```text
handler.py       HTTP request/response
router.py        method + path 分派
auth.py          reverse-proxy / mutation authorization
serialization.py JSON DTO/Decimal/datetime统一编码
endpoints/*      参数校验 + Service 调用
```

要求：

- endpoint 不写 SQL；
- endpoint 不读全局 mutable state；
- 所有 JSON 走统一 serializer；
- `Decimal`、datetime、set 等转换规则集中；
- 400 / 401 / 403 / 404 / 409 / 500 返回格式统一；
- mutation endpoint 统一检查 write-freeze；
- API base 只有一个，不再让浏览器尝试多个端口/路径 fallback。

### G1-06：Report Registry 统一

当前 `enhance_coverage.py` 与 `code_detail_service.py` 都存在 registry 读取逻辑。

迁入：

```text
app/reports/registry.py
```

统一负责：

- register；
- load；
- prune；
- report_id validation；
- report root 绑定；
- sidecar-required metadata；
- fail-closed。

Code Detail 与 Inject 只调用 Registry Service，不再自己实现一份。

### G1-07：Code Detail 归位

迁移：

```text
code_detail_service.py → app/code_detail/service.py
source_reader.py       → app/code_detail/source_reader.py
code_region.py         → app/code_detail/code_region.py
```

现有：

- `overlay_cache.py`；
- `sidecar_store.py`

继续使用。

必须保持：

- DB MD5 `file_path_hash` 与 Sidecar SHA256 key 不混淆；
- Sidecar v2 → legacy v1 → fail closed；
- explicit report_id 不跨 report fallback；
- SourceContext cache 有界；
- Lazy Collapse layout/lines API 行为不变。

迁移期间 root 文件只允许：

```python
from app.code_detail.service import *
```

或更窄的显式兼容导出，不能继续保留独立实现。

### G1-08：Incremental 归位

保留现有：

```text
app/incremental/path_index.py
app/incremental/service.py
```

把 `coverage_check.py` 内剩余职责拆出：

```text
git_diff.py       oldgit..newgit diff / added lines
blame.py          porcelain blame / ownership
lcov.py           DA/FN/FNL/FNA parsing
report.py         incremental model / export composition
service.py        orchestration
```

`coverage_check.py` 最终只保留 CLI compatibility。

必须继续遵守：

- blame 目标固定到 `newgit`，不能用 moving HEAD；
- ambiguous suffix fail-closed；
- basename only 禁止自动匹配；
- repo namespace 隔离；
- developer task 以 owner line ∩ current pending line 计算；
- function range 缺失/冲突时整文件 fallback 到 source parser。

### G1-09：Job 生命周期归一

当前 `BoundedJobExecutor` 可保留为“执行器”，`BackgroundJobService` 升级为业务 owner。

目标：

```text
Job API
  ↓
BackgroundJobService
  ↓
JobRepository + BoundedJobExecutor
```

Service 负责：

- submit；
- dedupe；
- state transition；
- heartbeat；
- cancel；
- recovery；
- data_version/scan validity；
- result_path；
- retry policy。

Executor 只负责：

- bounded queue；
- worker thread；
- 执行 callback。

Gate 1 要把 root `recover_background_jobs()`、cleanup loop 等变为 Service 委托，不能和 `BackgroundJobService.recover()` 形成两套独立状态机。

### G1-10：前端资产 Canonical Ownership

最终只有：

```text
web/assets/js/*
web/assets/css/*
web/templates/*
```

是可编辑源。

root 同名文件若为兼容用途，只能满足二选一：

1. 构建/发布时自动生成并保证 SHA 完全一致；或
2. Candidate runtime 完全不读取，且逐步删除。

重点解决当前已发现的 root/web SHA 漂移。

### G1-11：`enhance_coverage.py` 变成薄入口

Gate 1 不要求它立即压缩到极小，但必须做到：

- 从 Gate 1 后不再向 root 新增业务实现；
- 已迁出的能力 root 只做 delegate；
- CLI 参数兼容；
- `server` / `inject` / `incremental` 最终都进入 `app.bootstrap`/Service；
- `_LegacyDatabaseManager` 进入退休状态。

Gate 3 结束时目标：

> `enhance_coverage.py` 只保留 CLI parser、兼容入口、少量 bootstrap glue，不再是业务 owner。

## 3.4 Gate 1 验证方案

### A. Python 3.6.8 兼容性

在真实目标 Python 3.6.8：

```text
py_compile 全部修改过的 app/*.py
py_compile enhance_coverage.py
py_compile coverage_check.py
py_compile code detail/root compatibility shims
```

并执行与移动模块对应的 targeted unit tests。

### B. Runtime Participation

必须证明：

```text
新 module
  ≠ 文件存在
  ≠ unit test 通过

新 module
  = server/inject/job/incremental 的真实入口确实调用它
```

运行现有 `scripts/diagnostics/runtime_participation_audit.py`，并根据 VNext 新结构更新检查规则。

### C. Canonical Ownership

运行/扩展：

```text
scripts/diagnostics/canonical_ownership_audit.py
```

要求：

- root/web 兼容副本无漂移；
- root/app 不存在两套同功能 active implementation；
- Report Registry 只有一个 owner；
- API handler 只有一个 owner；
- Background Job recovery 只有一个 owner；
- DB business CRUD 只有 Repository owner。

### D. 当前 Schema 行为回归

Gate 1 暂时仍可针对复制后的现有 Schema 做行为验证：

- fetch analysis；
- save draft；
- confirm；
- line index sync；
- data_version；
- progress fallback；
- job persist/recover；
- Code Detail overlay。

必须在 `coverage_candidate` 或测试库，不使用生产 `coverage`。

### E. Sidecar / Report 回归

覆盖：

- v1 Sidecar 读取；
- v2 chunk 读取；
- v2 metadata；
- content hash mismatch fail closed；
- missing chunk fail closed；
- report_id mismatch fail closed；
- registry exact report lookup；
- registry stale prune。

### F. Incremental 回归

至少：

- Git diff added lines；
- real porcelain blame；
- boundary commit；
- 多 repo 同 relative path；
- ambiguous suffix；
- LCOV legacy FN；
- LCOV 2.2 FNL/FNA；
- invalid range fallback；
- Suggested Reviewer 与 DB Reviewer 优先级。

## 3.5 Gate 1 退出条件

全部满足才进入 Gate 2：

```text
[ ] Python 3.6.8 修改点 compile 全通过
[ ] Runtime participation audit 通过
[ ] Canonical ownership audit 通过
[ ] root/web asset drift = 0
[ ] 同一业务能力无两套 active implementation
[ ] DB SQL 已明确 Repository owner
[ ] Report Registry 已统一
[ ] API serializer 已统一
[ ] Background Job recovery 已统一
[ ] Code Detail / Incremental 新模块真实进入 runtime
[ ] 当前业务语义无回归
```

---

# 4. Gate 2：最终数据模型与无损迁移

## 4.1 Gate 2 目标

Gate 2 不是“给旧四张表再加几个字段”。

目标是建立可以长期支撑：

- 多 Project；
- 多 Scan；
- 每次导入不可变身份；
- 每次 Scan 对应 Git RepositorySnapshot；
- Report identity；
- 历史分析追溯；
- 后续跨版本分析继承；
- 后续 AI 预分析；

的正式 VNext 数据模型。

核心链路：

```text
Project
   ↓
Scan
 ├── RepositorySnapshot
 ├── Report
 └── CoverageFile
       ↓
   CoverageLine
       ↓
     Analysis
```

## 4.2 设计原则

### 原则 1：Scan 是历史边界

每次覆盖率导入生成一个新的 Scan。

Scan 一经形成：

- `scan_id` 不改变；
- Git commit snapshot 不改变；
- info identity 不改变；
- report identity 不改变。

禁止把“当前 repositories.json”当历史 Scan 身份。

### 原则 2：权威事实与派生状态分离

权威：

```text
Project
Scan
RepositorySnapshot
CoverageFile
CoverageLine
Analysis
ProjectState.data_version
BackgroundJob业务状态
```

派生：

```text
coverage_file_state
progress aggregate
runtime cache
部分 sidecar/cache
```

### 原则 3：不伪造历史

旧数据库只有当前 project/path/line 事实，没有完整历史 Scan 记录。

迁移时：

> 每个项目只能生成一个“legacy migrated current state Scan”。

不能凭当前 `repositories.json` 猜测它就是历史上所有分析发生时的 Git commit。

只有有明确证据的字段才写入 verified snapshot；不确定的必须标记 unknown/unverified。

## 4.3 建议最终 Schema

以下是逻辑模型，DDL 需要按 MariaDB 5.5 最终落地。

### 4.3.1 `coverage_projects`

```text
id                  BIGINT PK AUTO_INCREMENT
project_name        VARCHAR(128) UNIQUE NOT NULL
created_at          DATETIME NOT NULL
updated_at          DATETIME NOT NULL
```

### 4.3.2 `coverage_scans`

```text
id                  BIGINT PK AUTO_INCREMENT
project_id          BIGINT NOT NULL
scan_key            CHAR(64) UNIQUE NOT NULL
scan_type           VARCHAR(32) NOT NULL
review_scope        VARCHAR(32) NOT NULL
info_file_name      VARCHAR(255)
info_sha256         CHAR(64)
imported_at         DATETIME NOT NULL
status              VARCHAR(32) NOT NULL
legacy_migrated     TINYINT NOT NULL DEFAULT 0
metadata_version    INT NOT NULL DEFAULT 1
```

建议 `scan_key` 对下列稳定字段计算：

```text
project
info_sha256
repository snapshots
review_scope
```

### 4.3.3 `coverage_scan_repositories`

```text
id                  BIGINT PK AUTO_INCREMENT
scan_id             BIGINT NOT NULL
repository_name     VARCHAR(128) NOT NULL
repository_path     VARCHAR(512)
branch_name         VARCHAR(255)
old_commit_sha      CHAR(40)
new_commit_sha      CHAR(40)
verified            TINYINT NOT NULL DEFAULT 0
captured_at         DATETIME NOT NULL
UNIQUE(scan_id, repository_name)
```

### 4.3.4 `coverage_reports`

```text
id                  BIGINT PK AUTO_INCREMENT
scan_id             BIGINT NOT NULL
report_id           VARCHAR(64) UNIQUE NOT NULL
report_root         VARCHAR(1024)
source_signature    VARCHAR(128)
sidecar_schema      INT
asset_identity      VARCHAR(128)
generated_at        DATETIME
```

注意：`report_root` 是部署路径，不应参与业务事实 identity。

### 4.3.5 `coverage_files`

```text
id                  BIGINT PK AUTO_INCREMENT
scan_id             BIGINT NOT NULL
repository_name     VARCHAR(128) NOT NULL DEFAULT ''
file_path_hash      CHAR(32) NOT NULL
file_path           VARCHAR(512) NOT NULL
source_file_name    VARCHAR(255)
UNIQUE(scan_id, repository_name, file_path_hash)
```

保留历史 MD5 `file_path_hash` 兼容，但真正隔离身份是：

```text
scan_id + repository_name + file_path_hash
```

### 4.3.6 `coverage_lines`

```text
id                  BIGINT PK AUTO_INCREMENT
file_id             BIGINT NOT NULL
line_number         INT NOT NULL
line_text           TEXT
coverage_state      VARCHAR(32) NOT NULL
block_start_line    INT
block_end_line      INT
block_type          VARCHAR(64)
function_name       VARCHAR(512)
function_hash       VARCHAR(64)
code_line_hash      VARCHAR(64)
code_occurrence     INT
suggested_reviewer  VARCHAR(255)
UNIQUE(file_id, line_number)
```

### 4.3.7 `coverage_analyses`

```text
id                  BIGINT PK AUTO_INCREMENT
line_id             BIGINT NOT NULL
status              VARCHAR(64)
is_draft            TINYINT NOT NULL DEFAULT 0
reviewer            VARCHAR(255)
coverage_method     TEXT
uncovered_reason    TEXT
comment             TEXT
created_at          DATETIME
updated_at          DATETIME
UNIQUE(line_id)
```

重点：

- Analysis 绑定**具体 Scan 的具体物理源码行**；
- 将来跨版本继承不是复用同一行 ID，而是产生新 Scan Line 后执行“继承动作”。

### 4.3.8 `coverage_project_state`

VNext 建议语义为：

```text
project_id          BIGINT PK
current_scan_id     BIGINT
 data_version       BIGINT NOT NULL
file_state_version  BIGINT NOT NULL DEFAULT 0
updated_at          DATETIME NOT NULL
```

权威版本仍是 `data_version`。

### 4.3.9 `coverage_file_state`

继续作为 Derived：

```text
scan_id
file_id
...
data_version
```

任何 rebuild 都不能反向修改 `coverage_lines` / `coverage_analyses`。

### 4.3.10 `coverage_background_jobs`

VNext 建议保留业务表名，但升级语义：

```text
job_id              VARCHAR(64) PK
project_id          BIGINT
scan_id             BIGINT
kind                VARCHAR(64)
state               VARCHAR(32)
progress            DECIMAL/DOUBLE
input_payload       LONGTEXT
result_path         VARCHAR(1024)
error_message       TEXT
data_version        BIGINT
heartbeat_at        DATETIME
created_at          DATETIME
started_at          DATETIME
finished_at         DATETIME
updated_at          DATETIME
```

MariaDB 5.5 无 JSON 类型，因此 payload 用 `LONGTEXT` 保存严格 JSON 文本，由应用 serializer 负责。

## 4.4 Gate 2 Migration 设计

### G2-01：先扩展现有 Data Hash Gate

当前已有 `scripts/diagnostics/data_hash_gate.py`，但它是“同表名 pre/post”比较。

VNext Schema 变化后必须增加：

```text
Legacy Normalized Snapshot
             ↓
      semantic facts
             ↓
VNext Normalized Snapshot
```

两边都归一成同一业务结构再 hash。

至少比较：

```text
project
file path
line number
line text
block range/type
function name/hash
code hash/occurrence
analysis reviewer/status/draft/method/reason
project data_version
job id/kind/state/error
```

### G2-02：Candidate DB 创建最终 Schema

严禁在生产库上直接 DDL。

流程：

```text
生产DB只读副本/备份
        ↓
coverage_candidate_legacy
        ↓
创建 VNext schema
        ↓
执行 migration
        ↓
coverage_candidate
```

具体库名可最终决定，但必须让 migration source 与 target 清晰分离。

### G2-03：Project 迁移

Project 来源为旧表事实并集，不只依赖某一张表：

```text
coverage_analysis
coverage_line_index
coverage_project_state
coverage_background_jobs
```

确保 orphan 项目不会被遗漏。

### G2-04：Legacy Scan 生成

每个旧项目生成一个：

```text
scan_type       = legacy_migrated
legacy_migrated = 1
```

这代表：

> “迁移时点旧系统当前权威状态”

不把它描述成真实历史导入批次。

### G2-05：RepositorySnapshot 迁移

只有盘点/Report 元数据能够证明的 commit 才：

```text
verified = 1
```

无法证明的：

```text
verified = 0
old_commit_sha/new_commit_sha = NULL 或保留来源但标注 provenance
```

禁止用“现在 Git HEAD”替换历史 snapshot。

### G2-06：File / Line 迁移

以：

```text
coverage_line_index
UNION
coverage_analysis 中出现但 index 不存在的 line identity
```

构建文件和行。

这样可以防止历史 Analysis 因 line index 变化而在迁移中丢失。

对于只有 Analysis、缺少 line-index context 的行：

- 仍创建 line identity；
- 缺失字段为空/unknown；
- 单独记录 migration anomaly；
- 不静默丢弃。

### G2-07：Analysis 原样迁移

要求逐字段保留：

```text
status
is_draft
reviewer
coverage_method
uncovered_reason
comment/已有业务字段
```

并保证：

```text
legacy project + path hash + physical line
        ↓
VNext scan + file + line
        ↓
analysis
```

一一对应。

### G2-08：Project State 原样迁移

`data_version` 必须完全保持。

Derived `file_state_version`：

- 可以先设为 0；
- migration 后重新 rebuild；
- rebuild 成功后设为当前 `data_version`；
- 该过程不能推进权威 `data_version`。

### G2-09：Background Job 迁移

历史 job 保留。

但 Candidate migration rehearsal 时：

> 不允许恢复旧库中的 queued/running 任务并自动执行。

先做状态审计：

```text
completed/failed/cancelled → 可历史保留
queued/running/interrupted → 标记需要人工迁移决策
```

正式 Gate 4/5 再决定 active job cutover 策略。

### G2-10：Migration 幂等性

Migration 至少连续执行两遍：

```text
run 1
run 2
```

第二遍必须：

- 不重复 Project；
- 不重复 Scan；
- 不重复 File/Line；
- 不重复 Analysis；
- 不推进 data_version；
- semantic hash 不变化。

## 4.5 MariaDB 5.5 特殊要求

不能直接依赖：

```sql
ALTER TABLE ... ADD COLUMN IF NOT EXISTS
```

等不同版本支持差异。

建议 migration runner 统一使用：

```text
information_schema 检查
        ↓
确认不存在
        ↓
执行标准 ALTER/CREATE
```

所有 DDL 先经过增强后的 schema preflight。

`schema_preflight.py` 的保护表集合必须升级，覆盖 VNext 的所有权威表。

## 4.6 Gate 2 验证方案

### A. Schema 验证

```text
[ ] MariaDB 5.5.64 实机执行成功
[ ] 全表 charset/collation 统一
[ ] 主键/唯一键完整
[ ] 关键查询索引存在
[ ] 无 MySQL 8/MariaDB 新语法依赖
[ ] Migration 两次执行结果一致
```

### B. 权威数据零损失验证

要求：

```text
Analysis semantic count/hash       100% 相等
Line Index semantic count/hash     100% 相等
Project data_version               100% 相等
Project 集合                        100% 相等
Job identity/state                  100% 可解释
Draft/Confirmed 分类                100% 相等
```

不能只比较总行数。

### C. 异常映射报告

必须单独输出：

```text
orphan analyses
missing line-index context
unknown repository snapshots
path conflicts
multi-repo same-path conflicts
invalid report identities
```

目标不是强行做到 0，而是：

> 每一条差异都必须可解释，不允许 silent loss。

### D. Repository 层新 Schema 验证

Gate 2 结束时，Candidate VNext runtime 必须：

- 只访问新 Schema；
- 不运行时 fallback 到 legacy tables；
- legacy source DB 只在 migration 工具读取；
- 业务启动后可以完全移除 legacy DB 连接。

## 4.7 Gate 2 退出条件

```text
[ ] VNext 最终 Schema 在 MariaDB 5.5.64 创建成功
[ ] migration 可从旧结构完整生成 VNext 数据
[ ] migration 幂等
[ ] authoritative semantic hash 0 mismatch
[ ] data_version 0 mismatch
[ ] Draft/Confirmed 0 mismatch
[ ] 所有 orphan/unknown 均生成显式报告
[ ] VNext runtime 不依赖 legacy 表
[ ] derived file state 可以从权威事实重建且不会改权威数据
```

---

# 5. Gate 3：全部现有核心能力迁入最终架构

## 5.1 Gate 3 目标

Gate 3 完成后：

> Candidate 已经是完整的新系统，不再是架构 Demo。

用户当前依赖的所有核心能力都必须走：

```text
VNext API
  ↓
VNext Service
  ↓
VNext Repository
  ↓
VNext Schema
```

旧 root 业务实现退出。

## 5.2 Gate 3 功能迁移矩阵

### G3-01：Project / Scan 生命周期

实现：

```text
Project create/read
Scan create/read/list
current scan
scan identity
RepositorySnapshot
Report binding
```

每次 `.info` 导入：

1. 计算 input identity；
2. 固化相关 repo path + branch + commit；
3. 创建 Scan；
4. 生成 CoverageFile / CoverageLine；
5. 绑定 Report；
6. 更新 current_scan；
7. 推进对应 `data_version`。

Scan 创建后不可被后续 repository config 覆盖。

### G3-02：Analysis Service 完整迁移

API 只调用：

```text
AnalysisService
```

Service 负责：

- load block analysis；
- save draft；
- confirm；
- batch save；
- reviewer precedence；
- physical source line persistence；
- transaction；
- data_version advance；
- derived state invalidate；
- cache invalidate。

明确保持：

```text
Draft → 仍 pending
Confirmed → 不再 pending
```

### G3-03：Progress / Unanalyzed

`ProgressService` 成为唯一入口。

读取策略：

```text
coverage_file_state ready 且 version 匹配
        ↓ yes
读取 aggregate

        ↓ no
读取 authoritative facts
```

不能在 stale 时返回旧 aggregate。

### G3-04：Code Detail

最终：

```text
/api/.../code-layout
/api/.../code-lines/batch
```

绑定：

```text
project_id
scan_id
report_id
file identity
```

而不是仅靠当前 project + path 猜 report。

必须保持：

- chunked Sidecar；
- legacy Sidecar read；
- exact report binding；
- cache version；
- overlay data_version；
- large function split/loading；
- cancellation/retry。

### G3-05：Incremental Coverage

`IncrementalService` 接管全部 orchestration。

保留当前最新代码已经实现的：

- added-line ownership；
- `git blame --line-porcelain`；
- boundary metadata；
- `suggested_reviewer`；
- developer tasks owner-specific pending lines；
- FNL/FNA function ranges；
- path-index reuse；
- multi-repo namespace；
- invalid LCOV range fallback。

并把结果写入/绑定到 Scan metadata，而不是只存在生成 HTML 里。

### G3-06：Background Jobs

所有耗时动作通过 Job Service：

- 大型 inject；
- export；
- 大型统计/rebuild；
- 后续可扩展 migration/AI job。

Job recovery 规则：

```text
启动
 ↓
读取 persisted jobs
 ↓
核对 scan / data_version / input existence
 ↓
可恢复 → retry/requeue
不可恢复 → interrupted/failed + 原因
```

任何进程退出/终端关闭不丢任务事实。

### G3-07：统一 JSON DTO

所有 API 输出经过 `app/api/serialization.py`。

必须统一转换：

```text
Decimal → int/float/string（按字段定义）
datetime → ISO8601
set      → list
Path     → str
```

彻底解决当前日志中已有的：

```text
Decimal is not JSON serializable
```

这一类跨层泄漏。

### G3-08：Export

继续复用 streaming export 思路。

导出内容需要新增可追溯身份：

```text
Project
Scan ID
Report ID
Repository/commit range
Generated At
```

并保持现有业务字段与 developer task 字段。

### G3-09：前端 API 统一

浏览器只认一个同源 API base。

例如：

```text
/api/coverage
```

或最终确定的：

```text
/api/v1
```

二选一，在 Gate 3 冻结。

核心要求不是具体名字，而是：

```text
禁止：
127.0.0.1:9528
127.0.0.1:19528
/coverage/api/coverage
多级 guessing fallback
```

进入浏览器业务代码。

实际 upstream 由 Nginx 负责。

### G3-10：Web 资源单一来源

所有页面只引用 `web/assets` 生成的正式资源。

shared JS/CSS 每次变化必须推进 asset hash/cache identity。

不同 Report 的运行配置通过 HTML meta/report metadata 传递，不通过生成不同版本 JS。

### G3-11：CLI Compatibility

保留用户习惯的：

```text
python3 enhance_coverage.py server
python3 enhance_coverage.py inject ...
python3 enhance_coverage.py incremental ...
```

但这些命令只能 delegate 到 `app`。

`coverage_check.py` 同样只允许成为 standalone CLI shim。

### G3-12：旧实现退出

Gate 3 最重要的架构验收之一：

```text
Legacy root implementation
       ↓
必须退出 runtime
```

允许：

```text
thin import shim
CLI wrapper
compat constant export
```

禁止：

```text
root 和 app 各自维护一份 DB CRUD
root 和 app 各自维护一份 Report Registry
root 和 app 各自维护一份 Background Job recovery
root 和 app 各自维护一份 Code Detail Service
root 和 app 各自维护一份 Incremental business path
```

---

# 6. Gate 3 完整验证矩阵

## 6.1 测试原则

仍然遵守：

> **不跑与修改无关的全量测试。**

但 Gate 1 ～ Gate 3 会触及几乎全部核心链路，所以 Gate 3 的“修改点相关测试集合”本身会较广。

必须区分证据级别：

```text
Unit
DB Integration
HTTP Integration
Real Git
Mock DOM
Real Browser
Performance
Migration
```

任何一种不能替代另一种。

## 6.2 Python 3.6.8 Gate

必须在实际目标 Python 3.6.8：

```text
compile 全部 VNext runtime 文件
运行 Repository/Service/HTTP targeted tests
运行 migration tests
```

GitHub Python 3.10/3.12 通过不能替代该证据。

## 6.3 数据库事务测试

至少覆盖：

1. Draft 保存成功；
2. Confirm 保存成功；
3. block 多物理行原子保存；
4. 保存失败 rollback；
5. data_version 只推进一次；
6. derived readiness 被正确置 stale；
7. rebuild 不修改 authoritative facts；
8. 并发保存同一 block 的冲突处理；
9. connection pool return 自动 rollback 未提交事务；
10. reconnect 后语义不变。

## 6.4 API Integration

覆盖所有核心 endpoint：

```text
health/release
project
scan
analysis read/save
progress/unanalyzed
code layout
code lines batch
incremental/developer tasks
jobs
export
```

验证：

- schema；
- HTTP status；
- error payload；
- auth；
- write freeze；
- Decimal/datetime serialization；
- malformed parameters；
- oversize batch；
- path traversal。

## 6.5 Real Git 验证

必须使用真实 Git repo fixture，而不只 mock：

- diff added lines；
- rename；
- deleted file；
- multiple commits；
- multiple authors；
- boundary commit；
- blank-boundary SHA；
- same relative path in different repos；
- old/new commit 不存在时 fail closed；
- worktree 当前 HEAD 与 newgit 不一致时仍以 newgit 为准。

## 6.6 Sidecar / Code Detail 验证

覆盖：

- v1 legacy read；
- v2 chunk read；
- meta-only layout；
- batch ranges；
- missing chunk；
- content hash mismatch；
- wrong report_id；
- source cache not found；
- large file；
- ultra-large function；
- cache LRU；
- overlay data_version refresh。

## 6.7 Real Browser E2E

至少覆盖：

1. 首次打开 Code Detail；
2. Lazy Collapse 展开；
3. 展开中取消；
4. 取消后重新展开；
5. 网络失败后 retry；
6. 不重复 DOM 行；
7. Draft 未提交编辑跨折叠保留；
8. restore default 与 expand all 竞态；
9. 保存分析后待分析数量刷新；
10. 页面切后台再回来时数据版本变化能够刷新；
11. 排序；
12. 筛选；
13. Developer Tasks owner-specific pending；
14. 相同文件多人 owner 不互相覆盖数量。

Mock DOM 只能作为快速回归，不算最终浏览器证据。

## 6.8 Background Job 恢复验证

流程：

```text
提交长任务
  ↓
任务 running
  ↓
停止 Candidate API process
  ↓
重新启动
  ↓
读取 persisted job
  ↓
根据规则恢复/标记 interrupted
```

验证：

- job_id 不变；
- state transition 合法；
- result_path 在 Candidate data root；
- 不引用 Current 路径；
- 不自动恢复到错误 Scan/data_version；
- 终端关闭不影响 systemd 下任务。

## 6.9 数据 Freshness 验证

必须覆盖：

```text
save analysis
    ↓
data_version + 1
    ↓
file_state stale
    ↓
progress API 不返回旧 aggregate
    ↓
rebuild
    ↓
file_state_version == data_version
```

## 6.10 Path Mapping 验证

现有 `path_mapping_audit.py` 基础上覆盖：

- exact；
- normalized；
- unique suffix；
- ambiguous suffix；
- basename-only rejected；
- multi-repo same path；
- LCOV absolute path；
- migrated scan repository namespace。

## 6.11 安全验证

至少：

- Code Detail path traversal；
- report_id traversal；
- Sidecar symlink escape；
- export path；
- mutation API auth；
- reverse-proxy trusted address；
- CORS/origin；
- subprocess 不使用 shell 拼接 Git 参数；
- upload/output path 不能逃逸配置根目录；
- write freeze 对所有 mutation endpoint 生效。

## 6.12 性能验证

与固定 baseline 使用**同一数据集、同一 cache 状态、同一硬件**比较。

建议至少测：

```text
API server latency
progress query
Code Detail layout
first expand
large region expand
expand all
Sidecar range load
incremental path join
large export
inject parse
```

建议默认门槛：

> 核心用户链路无明确理由不得比当前等价 workload 回退超过 20%。

如果存在可解释换取数据正确性的性能损失，需要单独记录并接受，不允许静默退化。

---

# 7. Gate 1 ～ Gate 3 的内部提交顺序

虽然只做一个 VNext 版本，代码仍建议使用可审计的小步 commit：

```text
Commit A  基线/架构 mapping + config/bootstrap
Commit B  DB repository + transaction
Commit C  API/router/serialization/auth
Commit D  Code Detail / Report Registry canonicalization
Commit E  Incremental canonicalization
Commit F  Job canonicalization
Commit G  Schema VNext + migration runner
Commit H  Project/Scan/RepositorySnapshot
Commit I  Analysis/Progress 切 VNext schema
Commit J  Inject/Incremental/Report 绑定 Scan
Commit K  Frontend统一 API/asset
Commit L  Legacy runtime deactivation
Commit M  Gate 3 focused regression fixes
```

这样仍然是“一轮开发”，但每个风险点都可以单独 review / revert。

---

# 8. 每个 Gate 必须产出的证据

## Gate 1

```text
architecture_ownership_matrix
runtime_participation_report
canonical_ownership_report
py36_compile_report
targeted_test_report
api_route_contract
```

## Gate 2

```text
vnext_schema.sql / migration runner
schema_preflight_report
legacy_normalized_snapshot.json
vnext_normalized_snapshot.json
semantic_integrity_report.json
migration_anomaly_report.json
migration_idempotency_report
```

## Gate 3

```text
capability_migration_matrix
api_contract_report
runtime_legacy_dependency_audit
background_job_recovery_report
path_mapping_report
sidecar_registry_report
real_browser_e2e_report
performance_comparison_report
py36_runtime_report
```

所有证据必须绑定：

```text
Git commit SHA
build/release identity
schema version
asset hash
测试数据/Scan identity
```

不能使用“PASS”文字或无来源截图替代可复查证据。

---

# 9. Gate 1 ～ Gate 3 明确不做的事情

本阶段不做：

```text
× 停止当前生产服务
× 修改生产 Nginx 流量
× 在 production coverage DB 直接执行 VNext DDL
× 删除 /home/zcyu/coverage
× 删除生产历史 Report
× 使用 Candidate 写 production coverage DB
× 正式生产 mysqldump/cutover
× 最终生产 rollback 演练
× OS / Python / MariaDB / Nginx 升级
× AI 预分析产品功能扩展
```

AI 预分析、智能跨版本继承属于 VNext 架构完成后的正常功能版本，不纳入本轮 Gate 1 ～ Gate 3，避免目标失控。

---

# 10. Gate 3 完成后的目标状态

代码：

```text
enhance_coverage.py
coverage_check.py
        ↓
仅 CLI / compatibility shim

app/*
        ↓
唯一真实业务实现
```

数据库：

```text
Project
  ↓
Scan
 ├── RepositorySnapshot
 ├── Report
 └── CoverageFile
       ↓
   CoverageLine
       ↓
     Analysis

ProjectState / BackgroundJob
```

运行：

```text
Candidate API
   ↓
统一 Service
   ↓
统一 Repository
   ↓
coverage_candidate VNext Schema
```

前端：

```text
Browser
   ↓
单一同源 API Base
   ↓
Nginx
   ↓
Candidate API
```

持久化：

```text
MariaDB = 权威业务事实
Report/Sidecar = 报告与源数据资产
Cache/Aggregate = 可重建派生状态
Git repos = 只读源码证据源
```

---

# 11. Gate 3 最终判定标准

满足以下所有条件，才可以进入 Gate 4：

```text
[ ] 当前全部核心业务能力已迁入 VNext runtime
[ ] root 仅有 compatibility / CLI，不再有独立业务实现
[ ] VNext 只使用最终 Schema
[ ] legacy → VNext semantic migration 0 authoritative mismatch
[ ] confirmed/draft/pending 业务合同保持
[ ] data_version / file_state freshness 保持
[ ] Background Job 可持久恢复
[ ] Code Detail Sidecar v1/v2 历史可读
[ ] report identity fail closed
[ ] multi-repo path namespace 正确
[ ] Incremental blame/ownership/function range 合同保持
[ ] 统一 JSON serializer 已覆盖 Decimal 等类型
[ ] Browser Lazy Collapse/排序/筛选/刷新行为通过
[ ] Python 3.6.8 真实环境通过
[ ] canonical ownership = PASS
[ ] runtime participation = PASS
[ ] 无未解决 P0/P1
```

此时状态是：

> **Gate 1 ～ Gate 3 开发完成，但仍不是 Production READY。**

下一步 Gate 4 才进行生产数据副本上的完整迁移演练、Candidate 真机验收、性能和浏览器正式验收、备份/恢复/回滚演练。

---

# 12. 推荐执行顺序（最终版）

```text
锁定 7baca93 基线
      ↓
Gate 1
现有模块归位 + 唯一 owner + root 降级为 shim
      ↓
Gate 1 验收
runtime participation / canonical ownership / py36 / targeted regression
      ↓
Gate 2
最终 Project → Scan → RepositorySnapshot → File → Line → Analysis Schema
      ↓
Gate 2 验收
semantic hash / data_version / migration idempotency / anomaly report
      ↓
Gate 3
全部现有业务切入 VNext Service + Repository + VNext Schema
      ↓
Gate 3 验收
API / Job / Git / Sidecar / Browser / Performance / Security / py36
      ↓
进入 Gate 4
真实 Candidate 全链路验收与迁移演练
```

这套方案的核心不是“重写更多代码”，而是：

> **利用当前 `7baca93` 已有模块化成果，结束长期过渡态，建立唯一业务实现、不可变 Scan 历史模型和可以被严格迁移验证的新系统基线。**
