# FOS Coverage Tool e9fcc837 全量修复与 Skill 升级方案 v1.0

> 日期：2026-08-28  
> 当前生产 Release：`v11.7 2026-08-19`  
> 当前生产 Commit：`e9fcc837a1ac9847f3966fc8ddb2aed92ca473fc`  
> 当前生产数据库：`coverage_vnext_e9fcc837`  
> 当前决策：`NOT_READY`  
> 方案性质：**完整修改方案 + Skill 体系升级/安装结果**。本方案不直接修改业务仓库代码；在用户明确说出 `开始制作补丁` 之前，不生成项目补丁、不改生产数据。

---

## 1. 结论与修复目标

本次生产事故已经从“历史人工分析数据是否丢失”收敛为“VNext 上线后多个读路径、派生状态、静态报告兼容与发布治理缺陷叠加”。Legacy Analysis 权威数据已完成语义守恒验证：旧库 `coverage_analysis=67,826`，VNext `coverage_analysis_records=67,826`、`coverage_analysis_line_links=67,826`、兼容表 `coverage_analyses=67,826`，且迁移前后 semantic hash 完全一致，孤儿关系、跨 Scan 错链、重复 active relation 均为 0。因此本方案明确禁止通过重迁、重导、恢复旧库、清理 CURRENT 等方式“修历史分析”。

本轮修复目标不是让页面“看起来恢复”，而是同时完成以下闭环：

1. Incremental/Developer Tasks 能消费完整分页快照，完成分析的文件立即归零，且不会跨 `scan_id/data_version` 混合分页。
2. `coverage_file_state` 只有在 Canonical Analysis Domain 已完成并且 pending conservation 通过后才能 Ready；运行时不能继续返回已知错误的派生汇总。
3. Progress 前端与 VNext DTO 统一，不再把缺失字段静默渲染为 `0.0%`。
4. Code Detail 明确区分 `LEGACY_STATIC` 与 `VNEXT_ARTIFACT_READY`，不允许“旧 HTML + 新 JS + 缺失 Report/Sidecar 身份”的半迁移状态。
5. Report Root、Registry、Sidecar、Nginx Served Root 与 Release Manifest 建立统一的可验证发布身份。
6. Legacy 后台任务统一经过 canonical JSON serializer，消除 Decimal 序列化失败。
7. 发布验证 Candidate/Baseline 服务必须具备会话所有权、最小暴露与自动 teardown。
8. 修复项目名身份碰撞风险；数据库参数优化在功能正确性恢复后单独实施。
9. 所有 P1 均有修改点相关测试与发布 Gate，真实浏览器验收单独作为最终证据类别。

---

## 2. 不可破坏的架构约束

### 2.1 权威数据边界

- `AnalysisRecord`：分析内容权威。
- `AnalysisLineLink`：当前 Scan 下行关系和 review state 权威。
- `AnalysisBlock`：仅表示人工保存范围，Legacy 迁移时不能伪造。
- `ProjectState.current_scan_id`：CURRENT 唯一权威。
- `coverage_file_state`：可重建派生数据，不是 Analysis 权威事实。
- Legacy `legacy_source_analysis_id`：迁移 provenance，不允许再次错误地当成旧数据库业务主键一对一比较。

### 2.2 Scan / Report 身份

- 所有 VNext Code Detail 请求必须绑定明确的 `scan_id + report_id + repository identity + repository-relative file_path`。
- Legacy 历史仓库身份缺失时必须 fail closed 或进入显式 `LEGACY_STATIC` 模式，绝不能从当前 Git HEAD 猜历史身份。
- 不允许跨 Scan、跨 Report、跨 Repository 静默 fallback。

### 2.3 分页快照

- Pending-only API 中“当前页没有某文件”不能等价为“该文件为 0”。
- 只有完整消费 `has_more/next_cursor` 后，才能将完整快照中缺失的文件归零。
- Cursor 必须绑定 `effective_scan_id + data_version + filter/scope`；发生 `PAGINATION_CURSOR_STALE` 后必须丢弃整个部分结果并有限次数重启，禁止混合不同版本页面。

### 2.4 派生状态 Ready

`file_state_version == data_version` 只是必要条件，不是充分条件。Ready 至少必须同时满足：

```text
file_state_version == data_version
AND pending_conservation.status == PASSED
AND mismatched_files == 0
AND required file-state rows are complete for the Scan
```

### 2.5 发布一致性

浏览器实际读取的 HTML、JS、CSS、Report Root、Sidecar 与后端 API 必须属于同一 release-validation session。不能只验证应用源码目录的 asset hash，而忽略 Nginx 实际 `alias` 的 Served Root。

---

## 3. 全量问题清单与目标状态

| ID | 级别 | 问题 | Root Owner | 当前状态 | 修复目标 |
|---|---|---|---|---|---|
| F01 | P1 | `/incremental/unanalyzed` 前端只拉第一页 | Performance/UI | 已确认 | 完整分页、原子应用快照、缺失归零 |
| F02 | P1 | Developer Tasks 同样只拉第一页且保留旧 HTML 值 | Performance/UI | 已确认 | 与 Incremental 共用分页快照客户端 |
| F03 | P1 | `/incremental/unanalyzed` cursor 未绑定 Scan/DataVersion | Change Review | 已确认 | 复用 VNext keyset cursor 契约，陈旧 cursor fail closed |
| F04 | P1 | Analysis Domain 回填发生在 FileState Ready 之后 | Release Governance | 已确认 | Canonical Analysis 完成后再 rebuild/reconcile/mark_ready |
| F05 | P1 | ProgressService 仅凭版本相等返回错误派生状态 | Runtime Reliability | 已确认 | conservation 失败时自动 authoritative fallback |
| F06 | P1 | Progress 前端仍消费 v9 DTO 字段 | Performance/UI | 已确认 | 切到 VNext canonical DTO，缺字段显式报 contract error |
| F07 | P1 | Legacy `.gcov.html` 缺 `scan_id/report_id` 等身份 | Performance/UI | 已确认 | 显式 `LEGACY_STATIC` / `VNEXT_ARTIFACT_READY` 模式 |
| F08 | P1 | Legacy Report Root/Registry/Sidecar 缺失 | Runtime Reliability | 已确认 | Legacy 不误进 VNext；VNext Report 发布必须具备完整 artifact identity |
| F09 | P1 | 旧 Report HTML + 新 JS/CSS + 新 API 混合发布 | Release Governance | 已确认 | Versioned immutable publish root + atomic CURRENT pointer |
| F10 | P1 | Legacy background job 直接 `json.dumps(Decimal)` | Change Review | 已确认 | 统一 `to_jsonable`/canonical serializer |
| F11 | P1 | Gate E 19529/19530 发布后仍绑定 `0.0.0.0` | Release Governance | 已确认 | validation session 自动 teardown，最终端口关闭 Gate |
| F12 | P2 | `FOSV6R2` / `FOS_V6R2` 存在归一化碰撞风险 | Change Review | 风险确认、非主因 | project_id / exact canonical name，禁止 strip `_/-` 当身份 |
| F13 | P2 | MariaDB 128MB buffer pool、5MB redo、线程复用弱 | Runtime Reliability | 优化空间确认 | correctness 后基准化调优 |
| F14 | P2 | 历史 staging deadlock | Runtime Reliability | 历史证据、非当前事故 | deterministic batch order + deadlock retry/backoff 验证 |
| F15 | P1 Gate | 真实浏览器 E2E 未闭环 | Performance/UI | NOT_VERIFIED | 独立 browser node 完成 exact-session E2E |
| F16 | P2 | Release/Incident 审计曾使用错误 Nginx vhost | Maintainer Skill | Skill 缺陷已修 | HTTP 审计必须显式 Host/vhost |
| F17 | P2 | 审计曾把 surrogate provenance ID 当业务 ID | Maintainer/Release Skill | Skill 缺陷已修 | semantic/business identity 比较优先 |
| F18 | P2 | 审计源码脱敏破坏 `owner_token` 等合法标识符 | Maintainer Skill | Skill 缺陷已修 | source/config/log 分模式脱敏 |
| F19 | P2 | 原 Runtime helper 使用旧 `coverage_analyses` 作为主权威 | Runtime Skill | Skill 漂移已修 | `AnalysisRecord + AnalysisLineLink` canonical authority |
| F20 | P2 | 审计未覆盖 Served Root / Registry / Sidecar / cursor identity | 五 Skill 协同 | Skill 缺陷已修 | 新 helper + routing regression + release governance |

---

## 4. 业务代码详细修改方案

## 4.1 F01/F02/F03：Incremental + Developer Tasks 分页快照与 Cursor 一致性

### 4.1.1 后端：`app/api/application.py::unanalyzed`

当前 `unanalyzed()` 自行 base64 编解码只包含 `file_id` 的 cursor，而 `/progress/pending` 已有 `_decode_keyset_cursor()` / `_encode_keyset_cursor()` 的 Scan/DataVersion 契约。修改目标：让 `/incremental/unanalyzed` 与 VNext 其他 keyset API 使用同一快照规则。

建议实现顺序：

1. 按 exact `project_name` 获取 Project。
2. 获取 `ProjectState`。
3. 解析 `effective_scan_id = query.scan_id or current_scan_id`，并验证 Scan 属于 Project。
4. 读取当前 `data_version`。
5. 使用 `_decode_keyset_cursor(cursor, effective_scan_id, data_version, "incremental_unanalyzed")`。
6. 调用 `ProgressService.pending_by_file()`。
7. 返回实际 `effective_scan_id`、`data_version`、`page_size`、`files`、`has_more`。
8. `next_cursor` 用 `_encode_keyset_cursor(..., "incremental_unanalyzed")`。
9. 若 cursor 中 Scan/DataVersion/scope 不匹配，统一返回现有 `PAGINATION_CURSOR_STALE` 错误语义；不要降级成第一页，也不要静默接受旧 cursor。
10. 限制 `page_size <= 200`，保持服务端请求上限。

建议响应 DTO：

```json
{
  "project_name": "FOS_V6R2",
  "scan_id": 2,
  "data_version": 11980,
  "page_size": 200,
  "files": [],
  "has_more": false,
  "next_cursor": null
}
```

### 4.1.2 前端：`web/assets/js/incremental_coverage.js`

新增单一职责函数，例如：

```text
fetchCompleteUnanalyzedSnapshot(projectName, scanId, requestToken)
```

规则：

- 循环请求直到 `has_more=false`。
- 保存首个响应的 `scan_id/data_version`，后续页必须一致。
- 若收到 `PAGINATION_CURSOR_STALE`，丢弃当前 map，最多重启 2 次；超过次数向 UI 显示“数据正在变化，请稍后刷新”，不能应用半快照。
- 用 `repository_name + file_path` 作为首选 key；仅在明确兼容旧单仓模式时允许 file_path fallback，且必须无歧义。
- 完整快照完成前禁止修改 DOM。
- 完成后遍历所有行：存在于 pending map => 写真实值；不存在 => **明确写 0**。
- 同时更新 `textContent` 和 `data-sort-value`。
- 如果当前排序字段为“待分析/缺口”等动态字段，重新执行当前 sort；如果筛选条件依赖 pending，重新执行 filter。
- 使用 request generation/token 防止慢响应覆盖较新的刷新。

必须删除当前“API 没返回就保留旧 HTML 数字”的 fallback。

### 4.1.3 前端：`web/assets/js/incremental_developer_tasks.js`

不要复制第二套分页实现。推荐抽出共享模块（例如 `web/assets/js/pending_snapshot.js`），两个页面都调用同一函数；如果当前项目结构不适合新增模块，至少将同一契约封装成一个可测试函数并保持代码一致。

Developer Tasks 的 owner-line intersection 必须在完整 pending snapshot 上执行；absent file => pending=0。

### 4.1.4 针对性测试

修改/新增：

- `test_incremental_coverage.py`
  - 201~500 个 pending 文件，验证所有分页被消费。
  - 已从 17 -> 0 的文件，刷新后 `textContent=0` 且 `data-sort-value=0`。
  - 第二页 cursor 使用旧 `data_version` => `PAGINATION_CURSOR_STALE`。
  - stale cursor 重启成功；连续变化超过阈值 => fail closed。
- `tests/browser/coverage_real_browser.spec.js`
  - Incremental >200 文件分页。
  - Developer Tasks >200 pending 文件。
  - 排序/筛选在刷新后仍有效。
  - 慢旧请求不能覆盖新结果。

---

## 4.2 F04/F05：FileState 生命周期与 Progress False-Ready

这是当前最重要的后端一致性修复之一。

### 4.2.1 迁移顺序重构

涉及：

- `scripts/upgrade/migration_runner.py`
- `scripts/upgrade/domain_migration.py`
- `app/db/repositories/project_state_repository.py`
- `app/db/repositories/file_state_repository.py`
- `app/services/progress_service.py`

当前旧路径存在：

```text
set_current_scan
-> rebuild_scan
-> mark_ready
-> seal_scan
-> 后续才 apply_analysis_domain/backfill canonical links
```

应改为：

```text
迁移 Core/Compat facts
-> 建立/固定 Scan 与 Report
-> 将 file_state 标记 STALE
-> apply Analysis Domain
   -> AnalysisRecord
   -> AnalysisLineLink
   -> consistency audit
-> rebuild coverage_file_state from canonical facts
-> pending conservation
-> derived/authoritative reconciliation
-> mark file_state READY
-> 最终 publication owner 切 CURRENT
-> seal/publish
```

重要要求：CURRENT 的最终切换必须保留现有单一 publication owner，不要因为迁移顺序修复而新增第二套 CURRENT 写路径。

### 4.2.2 新增单一 Ready Owner

建议新增服务级方法，例如：

```text
FileStateService.rebuild_validate_and_mark_ready(connection, project_id, scan_id, data_version)
```

它是唯一允许调用 `ProjectStateRepository.mark_ready()` 的路径，内部必须：

1. `rebuild_scan()`；
2. `pending_conservation()`；
3. 验证 `mismatched_files==0`；
4. 验证 Scan 文件状态行数量完整；
5. 必要时与 `scan_summary_from_facts()` 做关键聚合 reconciliation；
6. 全部通过后才 `mark_ready()`；
7. 任一失败保持 `file_state_version` 非 Ready，并抛出阻断错误。

迁移、手工 rebuild、后台 rebuild 都调用同一个 owner，禁止复制逻辑。

### 4.2.3 `ProgressService.summary()` Fail-Safe

当前逻辑在 `file_state_version == data_version` 后直接返回 FileState aggregate，只是附带一个 `pending_conservation=FAILED`。修改为：

```text
version equal
  -> load aggregate
  -> validate file-state completeness
  -> pending_conservation
  -> PASSED: source=coverage_file_state
  -> FAILED: source=authoritative, derived_state_status=INVALID
```

失败时应返回 authoritative facts 汇总，并带显式诊断：

```json
{
  "source": "authoritative",
  "derived_state_status": "INVALID",
  "derived_state_reason": "PENDING_CONSERVATION_FAILED",
  "data_version": 11980,
  "file_state_version": 11980
}
```

不要在在线请求中自动 rebuild 大量 FileState，以避免浏览器请求触发重计算/长事务；在线请求只 fail-safe，修复由明确的 rebuild job/升级阶段负责。

### 4.2.4 数据版本规则

任何会改变 Canonical LineLink/ReviewState 对 pending 语义的写操作，必须保证：

```text
先推进/保留新的 data_version 语义
-> canonical mutation
-> derived invalidation
-> rebuild/reconcile
-> file_state_version=data_version
```

需要用 ownership audit 检查所有 save/confirm/reject/undo/inheritance/current-only mutation 路径，避免存在“改 canonical relation 但不使 derived stale”的第二类 false-ready。

### 4.2.5 针对性测试

- `tests/vnext/test_migration_runner.py`
  - assert Analysis Domain backfill 完成前不能 `mark_ready`。
  - backfill 后 rebuild 使用 canonical links。
  - conservation fail => migration gate fail，CURRENT 不切换。
- `tests/progress/test_phase4_progress.py`
  - version 相等 + conservation FAILED => authoritative fallback。
  - version 相等 + aggregate row incomplete => authoritative fallback。
  - version 相等 + conservation PASSED => derived path。
- 新增 migration fixture：Legacy confirmed/draft 混合，确保最终 `pending_total = ordinary + inherited + manual_draft`。

---

## 4.3 F06：Progress VNext DTO 收敛

### 4.3.1 契约先行

在 `docs/api_contract.json` / `docs/api_contract.md` 或现有 VNext DTO contract 中明确 Progress summary 的 canonical 字段：

```text
total_uncovered
filled_total
draft_total
confirmed_total
pending_total
ordinary_pending_total
inherited_pending_total
manual_draft_pending_total
pending_conservation
data_version
file_state_version
source
derived_state_status (新增，必要时)
```

### 4.3.2 Rate 口径

Legacy 代码显示 `fill_rate = filled_total / total_uncovered`、`confirmed_rate = confirmed_total / total_uncovered`。如果产品口径保持不变，VNext 前端可用 canonical totals 本地计算：

```text
fill_rate = total_uncovered ? filled_total * 100 / total_uncovered : 0
confirmed_rate = total_uncovered ? confirmed_total * 100 / total_uncovered : 0
```

建议将这两个 rate 定义为**展示派生值**，不再作为独立数据库权威字段；测试固定口径即可。

### 4.3.3 前端修改

`web/assets/js/coverage_progress.js`：

- 移除对缺失 `fill_rate/confirmed_rate` 的无条件读取，改用统一 rate helper。
- 不再依赖 VNext summary 不存在的 `unfilled_total/coverable_total/uncoverable_total/redundant_total`；若这些仍属于产品需求，必须给它们定义新的 VNext API owner，而不是继续从旧 DTO 猜。
- 必填字段不存在时显示 `-- / contract error` 并在 console/页面状态区记录契约错误，不能转成 `0.0%`。
- 保留 `/progress/files`、`/progress/details`、`/progress/pending` 的 cursor 分页能力，统一处理 stale cursor。
- 更新 `PROGRESS_PAGE_VERSION` 与 release manifest/cache identity，不再保留 `visible-progress-20260818_v9_12` 名称。

### 4.3.4 测试

- summary canonical DTO 不含旧字段时 UI 仍正确展示。
- missing required canonical field => 可见 contract error，不能显示 0。
- `filled_total=67377` 等非零值不能出现 `fill_rate=0.0%`。
- 分页 details/pending 前后版本变化时不混页。

---

## 4.4 F07/F08：Code Detail Legacy/VNext 双模式契约

当前不能通过“给旧 HTML 随便补 scan_id/report_id”来修，因为 Legacy Report 同时没有可靠 Report Root、Registry、Sidecar。必须从模式契约解决。

### 4.4.1 新增显式 Report Mode

建议定义：

```text
LEGACY_STATIC
VNEXT_ARTIFACT_READY
```

可放在 Report 持久化字段、生成 manifest 或可靠的 report metadata 中；关键是它必须是持久事实，不能由浏览器猜。

#### `LEGACY_STATIC`

适用于历史迁移但无法证明 Report Root/Sidecar/Repository identity 的报告：

- 使用静态 gcov HTML 中已有代码内容。
- `coverage_enhance.js` 发现 `LEGACY_STATIC` 时不得调用 VNext `/code-layout`/`/code-lines`。
- 保留可安全支持的旧静态折叠/查找功能；需要 Analysis overlay 的功能若没有可靠物理行身份，应显式不可用，而不是跨 Scan 猜测。
- UI 给出轻量标识：“历史静态报告”。

#### `VNEXT_ARTIFACT_READY`

必须同时满足：

```text
scan_id present and valid
report_id present and bound to scan
repository identity sufficient for file disambiguation
report_root or exact registry resolution exists
asset_identity matches
sidecar_schema supported
sidecar metadata exists
file_path resolves uniquely
```

缺任一项都不能进入 VNext Code Detail。

### 4.4.2 HTML 生成/注入

对 `VNEXT_ARTIFACT_READY` 的每个 `.gcov.html` 注入至少：

```html
<meta name="coverage-report-mode" content="VNEXT_ARTIFACT_READY">
<meta name="coverage-scan-id" content="...">
<meta name="coverage-report-id" content="...">
<meta name="coverage-repository-name" content="...">
<meta name="coverage-file-path" content="...">
```

Legacy static 则显式：

```html
<meta name="coverage-report-mode" content="LEGACY_STATIC">
```

### 4.4.3 `app/code_detail/vnext_service.py`

保持当前严格 `resolve_exact_root()`、Sidecar metadata fail-closed 原则；不要为了兼容 Legacy 而增加 current Git fallback。只需要让调用者在进入 VNext service 前证明模式是 `VNEXT_ARTIFACT_READY`。

### 4.4.4 Registry/Sidecar 发布

对未来 VNext Report：

- Report publication 原子写入 Report DB identity + Report Root/Registry + asset identity + Sidecar schema。
- Sidecar 根据该 Report 的确切 source facts 生成。
- Registry entry 与 Report ID 一一绑定。
- 发布后运行 read-only audit：每个 CURRENT `VNEXT_ARTIFACT_READY` Report 必须 exact resolve。

对现有 Legacy Report：

- 第一阶段直接归类 `LEGACY_STATIC`，不伪造 Registry/Sidecar。
- 第二阶段如果能从保留的 report artifact/source provenance **验证**历史 root，可做受控 promotion；不能用今天 Git HEAD 补造。

### 4.4.5 测试

- `test_code_detail_api.py`：VNext ready 身份完整才能调用。
- `test_code_detail_service.py`：Registry/Sidecar 缺失 fail closed。
- `tests/browser/coverage_real_browser.spec.js`：Legacy static 页面不发送 `/code-layout`；VNext ready 页面必须发送且身份正确。
- 同路径多 Repository 测试，验证 repository dimension 不被丢失。

---

## 4.5 F09：静态 Report 发布一致性

当前 Nginx 真实服务 `/home/zcyu/coverage/export0810/onesensor/`；JS/CSS 已更新到 e9fcc837，但 HTML 是历史报告，形成 mixed publication。

### 4.5.1 Versioned immutable publish root

推荐目录：

```text
coverage_publish/
  releases/
    <release_session_id>/
      reports/
      assets/
      release_manifest.json
      report_manifest.json
      registry/
  CURRENT -> releases/<release_session_id>
```

Nginx `/coverage/` 只指向 `CURRENT/reports`（或更合适的统一 root）。

### 4.5.2 Manifest 扩展

Release Manifest 不只覆盖共享 JS/CSS，还必须覆盖：

- 实际入口 HTML；
- 报告模式；
- Report IDs/Scan IDs；
- Report Root identity；
- Sidecar manifest/schema；
- shared assets SHA256；
- build/commit/release-validation-session ID。

### 4.5.3 原子切换与回滚

发布顺序：

```text
prepare immutable release root
-> validate manifest/hash/report mode
-> validate backend DB/runtime identity
-> HTTP smoke on candidate root
-> atomic switch CURRENT symlink/pointer
-> real browser validation
-> teardown candidate validation services
```

回滚必须同时恢复浏览器 artifact 与对应兼容 backend/release identity，不能只回滚 JS 或只切数据库。

---

## 4.6 F10：Decimal JSON 统一序列化

涉及：`app/compat/legacy_runtime_previous_release.py::_finish_background_job()`。

当前：

```python
json.dumps(values["data"], ensure_ascii=False)
```

修改为调用唯一 canonical helper，例如：

```text
safe_data = to_jsonable(values["data"])
json.dumps(safe_data, ensure_ascii=False)
```

`to_jsonable` 应复用 `app/api/serialization.py`，不要在 compatibility 层再维护一套 Decimal 规则。

针对性测试：

- Decimal 位于顶层、dict、list、嵌套 tuple/list/dict。
- Job result atomic file write 成功。
- DB result 与 JSON file readback 一致。
- restart 后 job terminal result 可读取。

---

## 4.7 F11：Gate E Validation Session 生命周期

当前 Candidate/Baseline 仍监听 19528/19529/19530，其中 19529/19530 为 `0.0.0.0`。

### 4.7.1 默认网络规则

- Candidate API/Gateway/Browser bridge 默认绑定 `127.0.0.1`。
- 只有确实需要远端真实浏览器时才能显式绑定非 loopback。
- 非 loopback 模式必须同时记录：allowlist、临时 token、start time、expiry、PID、port、session ID。

### 4.7.2 Session owner

验证工具创建 session manifest：

```text
session_id
candidate_sha
baseline_sha
pids
ports
listeners
created_at
expires_at
evidence_paths
teardown_status
```

### 4.7.3 Final Gate

Release 结束前必须：

1. stop session-owned processes；
2. 验证 PIDs 不存在；
3. `ss -lntp` 验证端口关闭；
4. 写 teardown evidence；
5. Release Governance 将 teardown failure 判为 P1 / NOT_READY。

不要依赖人工记得 kill。

---

## 4.8 F12：Project Identity 碰撞治理

- 后端内部优先 `project_id`；对外仍可显示 exact `project_name`。
- 禁止 `replace('_','')`、`replace('-','')` 等结果作为项目唯一 identity。
- 建立 normalization collision diagnostic，仅用于提醒，不参与匹配。
- UI project selector value 使用 project_id 或 exact canonical name；显示名称可独立。
- 本次事故期间不自动 merge/rename `FOSV6R2` 与 `FOS_V6R2`，避免产生新的数据迁移风险。

---

## 4.9 F13/F14：数据库性能与历史 Deadlock（正确性恢复后）

当前生产不是 CPU/RAM/锁耗尽事故，因此数据库调参不应和 P1 修复混在一个发布窗口。

第二阶段性能专项：

- 建立固定工作量：Progress、Incremental >200 pending、Code Detail 大文件、Analysis save batch、Scan import。
- 采集 baseline：QPS、P50/P95/P99、rows examined、tmp disk tables、buffer pool hit、Threads_created、connection count、lock wait、RSS。
- 在数据库管理员允许的范围内逐步调整 buffer pool 与 redo；每次只改一组参数并保留 rollback。
- 审查应用 connection pool/keepalive，减少“Connections≈Threads_created”的线程创建压力。
- Import 批量写入固定 key 顺序，检测 deadlock 后有限指数退避重试；只把可重试 deadlock 回滚到最小 batch。
- Aug-13 staging deadlock 不直接升级为当前 P1，只有在当前 VNext workload 重现后再升级严重度。

---

## 5. 推荐开发阶段与提交拆分

为了避免一次提交同时改变迁移、API、UI、Report 发布和数据库参数，建议按以下阶段实施，每阶段均可独立回退。

| Phase | 内容 | 主要文件 | Gate |
|---|---|---|---|
| P0 | 建立修复分支、固定 e9fcc837 baseline、保留证据 | 无业务逻辑改动 | baseline identity PASS |
| P1 | `/incremental/unanalyzed` cursor + 前端完整分页/归零 | `application.py`、两个 incremental JS | >200 + zero transition PASS |
| P2 | FileState migration ordering + single ready owner | `migration_runner.py`、`domain_migration.py`、FileState/ProjectState | conservation PASS |
| P3 | Progress runtime fail-safe + DTO 前端收敛 | `progress_service.py`、`coverage_progress.js` | false-ready fallback + UI PASS |
| P4 | Report Mode + Legacy Static + VNext Artifact Ready | Code Detail、HTML generator、report publication | legacy/vnext 双路径 PASS |
| P5 | Immutable served-root publication + manifest/rollback | release scripts、Nginx deploy contract | served-root hash PASS |
| P6 | Decimal serializer + background result tests | legacy compat + serialization | nested Decimal PASS |
| P7 | Validation session teardown | release/validation scripts | all temp ports closed |
| P8 | 真实浏览器 exact-session 验收 | Playwright/browser node | browser E2E PASS |
| P9 | DB 性能专项 | DB config/pool/import | benchmark + rollback PASS |

建议一个 Phase 一个逻辑提交或小型提交组；不要把 DB 调优和 P1 correctness 修复塞进同一 commit。

---

## 6. 修改点相关测试矩阵（禁止跑无关全量测试）

### 6.1 必跑 Python/JS 单测

- `test_incremental_coverage.py`：分页、归零、stale cursor、Developer Tasks。
- `tests/progress/test_phase4_progress.py`：derived/authoritative、conservation。
- `tests/vnext/test_migration_runner.py`：迁移 ordering、Ready gate。
- `test_code_detail_api.py`：身份/模式。
- `test_code_detail_service.py`：Report Root/Sidecar fail-closed。
- 与 background job result/serialization 直接相关的测试文件或新增专用测试。
- `tests/release/test_release_readiness.py`：P1 gate、teardown、served-root identity。

### 6.2 必跑浏览器测试

基于 `tests/browser/coverage_real_browser.spec.js` 增补：

- >200 pending 全量分页。
- pending nonzero -> zero。
- refresh 后排序/筛选仍正确。
- stale/reordered response 不污染 DOM。
- Progress canonical DTO + rate。
- Legacy static Code Detail 不调用 VNext API。
- VNext artifact-ready Code Detail 调用 API 且 identity 精确。
- collapse/expand/retry/reopen 原有场景不回归。

真实浏览器证据与 mock DOM/HTTP 集成分开记录；后两者不能替代真实浏览器。

### 6.3 Python 3.6 / MariaDB 5.5 兼容

所有新增代码必须保持生产约束：

- Python 3.6 grammar；
- 不使用 Python 3.7+ 无条件 API；
- SQL 不使用 MariaDB 5.5 不支持的特性；
- 老 systemd 219 诊断脚本不使用 `systemctl --value`。

---

## 7. 发布与回滚方案

### 7.1 发布前

- 重新确认当前生产仍是 e9fcc837 baseline；若生产已变化，重新 pin，不直接套本方案的动态值。
- 备份 Legacy DB 和当前 VNext DB，记录 SHA256/restore rehearsal 证据。
- 候选 release 使用独立 DB/Report publish root 验证。
- Migration rehearsal 必须证明 canonical Analysis 67,826 等权威历史事实语义不损失。
- FileState derived reconciliation 必须在 Candidate 上 PASS。

### 7.2 发布硬 Gate

至少满足：

```text
P0/P1 open findings = 0
semantic_hash_match = true
orphan_records = 0
orphan_links = 0
scan_line_mismatch = 0
pending_conservation.status = PASSED
mismatched_files = 0
served-root manifest = PASS
report mode classification = 100%
VNext artifact-ready registry/sidecar = PASS
incremental >200 pagination = PASS
zero transition = PASS
progress DTO = PASS
real-browser E2E = PASS
validation teardown = PASS
```

### 7.3 回滚

- 保留上一个 Published release root 和数据库。
- 浏览器 artifact 与 backend 必须作为一个 release identity 回滚。
- 只允许切换到已验证的旧 Published 状态；不在回滚过程中重写历史 Analysis。
- 回滚后重新核对 CURRENT、semantic identity、HTTP served-root、Progress source 与端口 teardown。

---

## 8. 生产现有数据的处理原则

对于当前 `coverage_vnext_e9fcc837`：

- 67,826 条 AnalysisRecord/LineLink 不做恢复、不删除、不重迁。
- 修复 FileState 时，只重建可派生 `coverage_file_state`，且必须从 CURRENT Scan + Canonical facts 计算；执行前后生成 reconciliation evidence。
- 修复 Legacy Report 兼容时，不修改历史 Analysis identity；优先通过 `LEGACY_STATIC` 模式恢复安全展示。
- Gate E cleanup 只处理 validation-session 临时进程/端口，与业务数据库无关。
- 项目名治理本轮只防止新碰撞，不做 FOSV6R2/FOS_V6R2 合并。

---

## 12. 推荐的实际执行顺序

第一轮开发只处理 P1 correctness：F01~F11，不同时做数据库性能调优。开发完成后只运行与这些修改点直接相关的测试。Candidate 通过后，在独立 release-validation session 中完成 migration rehearsal、served-root、HTTP/API、真实浏览器和 teardown。只有所有 P1 Gate 关闭后，才允许切生产。

P2 的项目身份治理可以随 P1 代码一起增加“禁止新碰撞”的防护，但不要在事故窗口合并既有项目。MariaDB 参数和 staging deadlock 优化单开性能版本。

---

## 13. 最终 Definition of Done

此次修复只有同时满足以下条件才视为完成：

- Legacy Analysis 语义 hash 仍完全守恒，67,826 条权威历史事实无损。
- `/incremental/unanalyzed` Cursor 绑定 exact Scan/DataVersion。
- >200 pending 文件完整刷新，完成分析文件立即从旧值变 0。
- Developer Tasks 与 Incremental 使用同一 pending snapshot 契约。
- `coverage_file_state` 只有 conservation PASSED 才 Ready。
- ProgressService 发现派生状态 INVALID 时返回 authoritative facts，不返回错误 FileState。
- Progress UI 不再依赖未定义 v9 DTO 字段，不把 contract drift 渲染成 0。
- 所有 CURRENT Report 被明确分类为 `LEGACY_STATIC` 或 `VNEXT_ARTIFACT_READY`，无第三种半迁移状态。
- VNext-ready Report 具备 exact Report Root/Registry/Sidecar/identity；Legacy 不借用当前 Git。
- Nginx 实际 Served Root 与 release manifest 一致，HTML+JS+CSS+API 同 release session。
- Decimal background result 可序列化、持久化、重启读取。
- Gate E 临时进程在发布完成前全部 teardown，19529/19530 等 session 端口关闭。
- 修改点相关 Python/JS 测试通过，Python 3.6/MariaDB 5.5 兼容通过。
- 真实浏览器 exact-session E2E 通过。
- Release Governance 最终决策从 `NOT_READY` 变为 `READY`；任何 P1 或关键 evidence 缺失都不得标 READY。

---

## 14. 当前边界

本轮已按本方案完成代码实现、针对性测试、完整 Python 回归、真实 Chromium 回归、真实 VNext HTTP 回归以及本地诊断。工作树中的实现变更即为本方案的候选补丁，未执行生产数据库、Nginx Served Root 或历史数据写入。

本地验证结果：574 个 Python 测试通过、1 个仅 live-MySQL opt-in 测试跳过；修改点专项测试、真实 Chromium 11 场景、真实 VNext HTTP 6 场景以及浏览器 smoke 7 场景均通过；本轮新增了已确认 Legacy fact 的 pending 分区回归、继承 confirm/reject/undo 后 FileState Ready 版本一致性断言、VNext 与 Legacy Ready 并发版本变化拒绝、Legacy Sidecar report identity 拒绝、显式及缺失报告模式下 Legacy static 离线回归、VNext Code Detail 身份查询断言、Validation Session zombie PID teardown 与含空格进程名 start-time 解析回归、发布器 Legacy/VNext HTML 模式一致性回归、Progress 文件/详情 envelope 严格校验、pending snapshot 非法 envelope/重复与混合仓库身份 fail-closed 回归、Progress canonical 非零 rate 与缺字段可见 contract error 回归、Legacy DictCursor 字段按名读取与畸形 data_version/Ready CAS fail-closed 回归、合法 `data_version=0` Ready 派生快照与分页版本漂移回归，以及 canonical progress template cache-buster 回归；JS 语法、兼容副本一致性、Python 编译和 diff 检查通过。本地 loopback owned-process Validation Session teardown 也已实测通过，最终 PID/端口均关闭。另在临时 Python 3.12 + PyMySQL 0.10.1 venv 中重跑 43 个 migration/reliability/progress/release/real-browser evidence 专项，全部通过；Gate E `real_browser_evidence.js` 对 disposable 100k HTTP + Chromium fixture 也采集成功，但证据明确保持 `synthetic=true`、`release_eligible=false`；synthetic DOM 1k/10k/50k/100k 四档及 100k virtual-scroll 也通过，performance audit 正确保持 `PARTIAL` 与 `release_eligible=false`。

本轮又补充了三项内部边界硬化：pending snapshot 请求显式指定的 `scan_id` 必须与每一页响应一致且 `data_version` 不得为 null；immutable release 递归拒绝源目录、构建目录和验证目录中的嵌套符号链接；Validation Session 对创建时不存在的 PID 不建立归属记录，PID 复用只产生 teardown failure 证据且不发送信号。对应发布/会话专项共 12 个测试通过，pending snapshot/副本语法与一致性通过，浏览器 smoke 仍全量通过。

随后补充了两项一致性回归：FileState rebuild 在任意 Repository/数据库异常后都会以独立事务持久化 stale marker，避免旧的 false-ready 状态残留；Legacy backfill 复用其 schema-specific 的唯一派生投影 owner，不再重复实现 readiness SQL。死锁策略也核对为导入单 batch 事务的稳定顺序、有界指数退避和完整回滚重放，相关 targeted/full regression 通过；SQLite migration schema 的 PRAGMA 游标也改为显式关闭，消除了全量回归中暴露的 `ResourceWarning`。

本轮严格警告回归又发现并修复了 Legacy incremental 真集成测试遗留的 SQLite 连接未关闭问题（`test_incremental_coverage.py` 增加测试清理钩子）；目标用例及全量 `python3 -W error::ResourceWarning -m unittest discover -s . -p 'test*.py'` 均通过，结果为 574 个测试 `OK (skipped=1)`。

随后补充了在线 Progress Ready 校验的失败安全边界：畸形派生值、驱动类型异常或校验仓储异常不再传播为在线 500，而是返回显式 `FILE_STATE_VALIDATION_ERROR` 并回退 authoritative facts；summary、文件页、pending 文件页和 pending 行页均有回归覆盖，专项与严格全量回归均通过。

在当前 exact HEAD 上又重新采集了 disposable 100k HTTP/Chromium 跨层 fixture，输出保留 `synthetic=true`、`release_eligible=false`，并记录 DB/Sidecar/RSS/virtual-scroll 指标；将该 artifact 提供给本地性能审计后，Gate E 的本地 `performance_evidence` 检查为 `PASSED`，但真实 Candidate 性能证据仍保持缺失。当前 exact HEAD 的 Legacy compatibility surface smoke（导入、CLI help、退休 inherit fail-closed）也为 `PASSED`；由于 Legacy transitional 使用窗口与移除/回滚 manifest 仍未提供，Legacy retirement 仍为 `INCOMPLETE`。

当前工作树的 `app/` 与 `scripts/` 共 175 个 Python 文件已使用 Python 3.6 grammar（`ast.parse(..., feature_version=(3, 6))`）逐个检查并通过；环境没有可用的 Python 3.6 解释器，因此真实 Python 3.6 runtime 兼容性仍依赖独立 CI/目标主机证据，不能由该 grammar 检查替代。

同时收紧了 `.github/workflows/ci.yml` 的 `py36-compat` lane：除原有兼容套件外，主执行和失败隔离执行均纳入 reliability、immutable publication、Legacy Decimal serialization、Validation Session 四个本轮新增专项；工作流契约、YAML 解析、相关 44 个当前环境专项测试均通过。该 lane 仍必须在真实 Python 3.6 容器中成功后，才能关闭 Python 3.6 runtime 证据。

随后收紧了 MariaDB 集成证据：`mysql_vnext_integration.py` 现在在 scan ingest、analysis upsert 和显式 rebuild 三个阶段记录完整 FileState Ready gate（data/file-state version、completeness、pending conservation、authoritative reconciliation），并实际读取 Progress files、pending lines 和 Incremental pending pages；MariaDB 5.5 compatibility lane 对这些字段、事务回滚和 HTTP/分页结果进行强断言。诊断脚本编译、YAML 解析、`git diff --check` 及相关 23 个数据库/发布治理专项均通过；本地仍无 MariaDB 服务，因此真实 5.5 runtime 结果必须由 CI/兼容主机重新执行后才能计入外部证据。

本轮又将 Legacy migration fixture 扩展为可生成 confirmed/draft 混合事实：SQLite migration regression 与 MariaDB rehearsal 均校验 `pending_total = ordinary_pending_total + inherited_pending_total + manual_draft_pending_total`、FileState version 与 CURRENT publication，并在 migration 重跑后再次校验。当前混合 fixture/helper 专项 32 项通过；该结果仍不能替代真实 MariaDB 5.5 服务上的 rehearsal artifact。

为避免把“Python 3.6 通过”和“MariaDB 5.5 通过”作为两个相互独立的近似证据，MariaDB 5.5 CI lane 进一步增加了同一 disposable rehearsal 在实际 Python 3.6 容器中的执行与 runtime/database identity 强断言；artifact 同时保留 Python 版本、MariaDB 版本、Ready gate 和 mixed pending 分区。Python 3.6 compatibility lane 也纳入 Gate Matrix 证据校验专项及失败隔离执行。当前环境没有 Docker/MariaDB/Python 3.6，因此该组合验证仍待 CI 或兼容主机实际运行。

Gate A 的真实 backup rehearsal 也已补充逐项目 FileState Ready artifact：备份恢复后的每个 CURRENT Scan 都必须通过 data/file-state version、completeness、pending conservation 和 authoritative reconciliation，且第一次迁移与幂等重跑都被记录；SQLite helper 回归已覆盖该输出结构。该验证逻辑已纳入 Python 3.6 compatibility suite，但真实生产备份恢复仍未在当前环境执行。

Gate A 的 production-rehearsal CI workflow 现同时生成并上传 `evidence-manifest-v2.json`，并强断言 manifest 的 Gate、exact candidate SHA、MariaDB 5.5 runtime identity 及唯一 PASS record；flat rehearsal JSON 与 v2 manifest 必须作为同一份外部证据共同留存。

本轮还收紧了 Gate Matrix 外部数据库证据校验：Gate A backup、Gate A MariaDB、Gate B backfill 和 Gate C restart 的 flat JSON PASS 必须携带非空 `database_runtime_identity`；MariaDB 5.5 rehearsal 的运行时版本必须以 `5.5` 开头，且不能通过泛化 `evidence_class` 绕过该约束。缺失指纹、错误版本和正确版本三态回归已通过；该校验只会收紧证据真实性，不会把缺失的外部证据提升为 PASS。

另修复了 validation session 与 local staging controller 作为路径脚本直接调用时的仓库根目录导入问题，并让 staging controller 对无 session 归属的存活 PID fail-closed、禁止误发信号；Legacy 聚合层也对非标准数据库行做了 fail-closed 处理，不再把测试替身或异常驱动结果升级为 `IndexError`；两个 CLI 入口的 `--help` 直调回归及相关 9 项 session 专项均通过。

另核对了当前 `HEAD` 对应的公开 GitHub Actions run [#32927098614](https://github.com/Chary-yu/fos_coverage_tool/actions/runs/32927098614)：clean checkout 上的 MariaDB 5.5 compatibility、VNext MariaDB、Python 3.6、Python 3.10/3.12、specialist、semantic migration 与 browser E2E lane 均为 `success`，真实候选浏览器、生产备份和跨层性能 lane 为 `skipped`。由于本地工作树仍包含未提交候选补丁，且 artifact 下载需要认证（API 返回 401），该 run 仅作为 clean-`HEAD` lane 索引，不替代当前工作树或生产证据。

最新本地门禁快照仍为：Gate Matrix `INCOMPLETE`（A 10、B 12、C 12、D 19、E 14、F 13 项任务均因证据链未闭合而未完成），Gate Task Status `INCOMPLETE`（80 项），DoD `INCOMPLETE`（24 项），Release Readiness `NOT_READY`（112 个阻断项）。本方案的生产 Release Governance 仍必须保持 `NOT_READY`，直到补齐外部证据：真实 MariaDB 5.5/候选数据库 migration rehearsal、67,826 条历史事实 semantic hash、真实 Nginx Served Root 与 manifest、外部 Skill/Legacy retirement 证据、以及完整 validation-session teardown 证据。合并或切生产前不得把本地测试结果替代这些证据，也不得把合成性能基准当作生产性能发布证据。

2026-08-31 16:00（Asia/Shanghai）在当前候选工作树上重新执行严格警告全量回归：`python3 -W error::ResourceWarning -m unittest discover -s . -p 'test*.py'` 结果为 574 个测试 `OK (skipped=1)`，耗时 107.343 秒；随后重新生成 Gate Matrix、Gate Task Status、DoD 和 Release Readiness 快照，并完成 Python 3.6 grammar、Python 编译、CI YAML 解析及 `git diff --check`。快照状态未改变：Gate Matrix `INCOMPLETE`、80 项任务 `INCOMPLETE`、24 项 DoD `INCOMPLETE`、Release Readiness `NOT_READY`（112 个阻断项）。

同一候选工作树随后重新执行 JS 语法检查、7 个浏览器 smoke 和 `npx playwright test tests/browser`，结果为 17/17 通过；synthetic DOM 四档（1k/10k/50k/100k）及 100k virtual-scroll 采集通过，性能审计仍明确返回 `PARTIAL` 且 `release_eligible=false`。这些结果证明本地行为回归稳定，但不替代真实 Candidate 的 Served Root、数据库/Sidecar/RSS/p95 或生产 exact-session 证据。

此外重新生成了 `.artifacts/vnext/gate-bundle-current`：Gate A–F 六份 Evidence Manifest v2 均通过结构、哈希和身份校验，各 Gate 专项测试均 `PASSED/exit_code=0`；bundle 总状态仍为 `INCOMPLETE`，仅保留真实 MariaDB/生产/候选环境等外部证据缺口。

本轮环境复核还确认当前验证主机仅提供 gcc/g++，未提供可配置的外部 C/C++ parser、Docker、MariaDB 5.5、Python 3.6 或 Candidate/Nginx 服务；因此没有把 builtin parser、synthetic fixture、静态配置或历史 artifact 提升为 Gate D/A/E/F 的真实证据，相关门禁继续保持 `INCOMPLETE`。

随后修复了 Gate Task Status 的依赖阻断诊断：上游任务阻断信息现在报告实际依赖 ID（例如 `A-03`），不再错误重复当前任务 ID；相关 Gate Task/Matrix/Release Governance 回归 33 项通过，并重新生成了当前状态快照。

修复后于 2026-08-31 16:10（Asia/Shanghai）再次执行严格警告全量回归，574 个测试仍为 `OK (skipped=1)`（111.628 秒）；Gate Matrix、Task Status、DoD、Release Readiness、Python 3.6 grammar、编译、YAML 和 diff 检查均重新执行，结果保持可追溯且未产生新的失败。

2026-08-31 17:28（Asia/Shanghai）根据新增审计关闭了大文件 Developer Tasks 的 P1 遗漏：`/api/coverage/incremental/unanalyzed` 的文件行 DTO 现在明确携带 `pending_line_numbers_complete`，当 pending 行超过预览上限时返回空预览但不再表达“无待分析行”；新增 `GET /api/coverage/scans/{scan_id}/files/{file_id}/pending-lines`，其分页 cursor 绑定 Scan、data version 和 file identity，前端只有在完整分页快照成功后才计算 owner-specific 交集，旧请求或 stale cursor 仍 fail-closed。新增 VNext runtime、API cursor/repository identity 和真实 Chromium 大文件 owner-specific 回归；浏览器专项最终 19/19 通过。新增公共 DTO 后 API contract 版本更新为 `vnext-api-20260831.1`，并同步更新报告兼容性 identity。

同一轮修复了 Progress 页面 DTO 语义：canonical VNext summary 未提供 `ownership`、`teams`、`dirs` 时，归属卡片、归属状态、小组表和目录表全部隐藏；只有响应显式提供对应字段时才渲染，避免把字段缺失误解为 0 或“暂无数据”。MariaDB 5.5 CI evidence assertion 已将字符串数值显式转换为 `int`；Python 3.6 ValidationSession 单元测试 mock `_port_listeners` 固定无 listener 的单元环境，production fail-closed 规则保持不变。

FileState mutation 路径已使用已知 `affected_file_ids`：先将未受影响的 derived rows rebase 到新版本，再只聚合受影响文件，之后继续执行完整 completeness、pending conservation 和 authoritative reconciliation Ready Gate；新建 Scan Import 仍全量构建，因为其整个 Scan 都是新事实。新增局部/全量 Ready Gate 合成基准脚本 `scripts/diagnostics/file_state_rebuild_benchmark.py` 和回归，当前本机小型 SQLite 试跑（20 文件 × 50 行、2 次）局部路径中位数约为全量的 0.70 倍；该结果明确标为 synthetic、`release_eligible=false`，不能替代发布前 MariaDB 等价性能门禁。

上述修复已完成后端与发布专项、真实 Chromium 19 场景、API contract/JS/Python 静态检查；严格警告全量回归已通过 577 个测试（`OK (skipped=1)`），当前只剩最终 exact commit 推送。GitHub `main` 分支保护、required Candidate checks、真实 MariaDB/生产 Served Root/候选性能与完整 release evidence 仍属于外部待办，不能由本地测试替代。
