# `be349add` → `bfa63de` 功能移植方案：逐行责任人 + LCOV 函数范围

## 1. 目标

在当前主干 `Chary-yu/fos_coverage_tool@bfa63de85eb8620d3a49f4c45a6776b53eaa59a9` 上，提取 `be349add3e4579e8b9b9f5b78eba4b4dce708f18` 中两组有价值能力：

1. **增量覆盖率逐行责任人**：对 `oldgit..newgit` 最终新增行执行固定到 `newgit` 的 `git blame`，把每一条新增行精确归属到 Git author，并用于开发人员任务、默认确认人、JSON/Excel 证据。
2. **LCOV 函数范围复用**：从 `.info` 中读取带完整结束行的函数范围，在可信时直接交给 Code Detail / Lazy Collapse，失败时继续使用主干现有 C/C++ 源码函数解析器。

本次只做**功能移植**，不整体合并 `be349add`，不搬它的 `src/coverage_tool` 目录重构，不回退主干现有的路径索引、OverlayCache、SidecarStore v2、Parse Once、目录签名、连接池、聚合表和发布治理。

## 2. 基线与约束

- 目标主干：`bfa63de85eb8620d3a49f4c45a6776b53eaa59a9`
- 功能源：`be349add3e4579e8b9b9f5b78eba4b4dce708f18`
- 当前主干增量结果：`schema_version = 2`
- 当前主干路径解析已经通过 `app/incremental/path_index.py` 提供 exact / normalized / unique suffix / ambiguous fail-closed；**不得用分支中简单的 `endswith()` 匹配替换它**。
- 当前主干注入已经通过 `InjectService.parse_once` 单次生成 `SourceContext + line_index + function_ranges`；**不得恢复多次 HTML 解析**。
- 当前主干 SidecarStore v2 已把 `SourceLineDTO.to_dict()` 写入 chunk，同时把 `function_ranges` 写入 `meta.json`；新增字段应走现有序列化链路。
- **不做数据库 schema migration**。Git 默认责任人是“建议值”，真正保存后仍写现有 `coverage_analysis.reviewer`。
- 不跑无关全量单元测试，只跑修改点相关测试。
- 当前仅输出开发方案；未收到“开始制作补丁”前不制作 `.patch`。

## 3. 推荐的最终数据流

```text
Git oldgit..newgit
    │
    ├─ git diff ───────────────→ 新增行集合
    │
    └─ git blame newgit -L ... → 行级 author / email / commit / subject
                                  │
LCOV .info                        │
    ├─ DA → 行覆盖状态            │
    └─ FN/FNL/FNA → 完整函数范围  │
              │                   │
              └────────┬──────────┘
                       ▼
             coverage_check.py
                       │
              schema v3 result
       ┌───────────────┼─────────────────┐
       │               │                 │
 review_lines      reviewers_by_file  function_ranges_by_file
       │               │                 │
       └───────────────┼─────────────────┘
                       ▼
             enhance_coverage.py
                       │
               IncrementalService
             安全路径统一解析
                       │
                       ▼
              InjectService.parse_once
                       │
        ┌──────────────┴──────────────┐
        ▼                             ▼
 suggested_reviewer             known_function_ranges
        │                             │
        └──────────────┬──────────────┘
                       ▼
                 SourceContext
                       │
              SidecarStore v2
                       │
                       ▼
              CodeDetailService
                       │
      DB reviewer > Git suggestion > blank
                       │
                       ▼
        Lazy Collapse / 分析面板 / API
```

## 4. 核心设计原则

### 4.1 Git blame 是“建议责任人”，不是数据库事实

优先级固定为：

```text
已有数据库 reviewer
    > Git blame suggested_reviewer
    > 空字符串
```

因此：

- 已经人工确认、暂存或继承的 reviewer 不能被 Git author 覆盖；
- 首次打开待分析块时，如果数据库没有 reviewer，则自动显示 Git author；
- 数据版本刷新、Sidecar 重载、区块折叠/重新展开后，建议责任人仍然存在；
- 全量报告不执行 blame，只有 `review_scope=incremental` 使用该能力。

### 4.2 Git blame 必须固定到 `newgit`

使用：

```bash
git blame --line-porcelain -L <start>,<end> <newgit> -- <file>
```

不能使用工作区 HEAD，避免用户之后的本地修改改变责任人归属。

只对 `git diff` 确认的最终新增行执行 blame。连续行先合并为 `-L` 范围；碎片过多时允许对单个文件做一次整文件 blame 后过滤，但不能把不在新增集合内的行写进结果。

生产路径中 blame 失败应 **fail closed**：不允许悄悄退化成“文件作者”并继续生成错误责任人。仅测试/导入型无 Git 场景可以显式注入 `line_authors_by_file` 跳过调用。

### 4.3 LCOV 函数范围只能做“可信加速输入”

主干现有 `source_reader.extract_c_function_ranges()` 继续保留为最终回退。

仅当 LCOV 能给出完整的 `start_line + end_line` 时才使用。以下情况必须回退源码解析：

- 只有传统 `FN:<start>,<name>`，没有结束行；
- start/end 非法；
- end 超出真实 HTML 源码行数；
- 范围交叉或重叠无法消歧；
- 同一文件的函数记录明显不完整；
- 多份 `.info` 合并后出现冲突范围。

Compiler alias / lambda / 模板实例造成嵌套范围时，仅保留最外层物理函数范围；真正交叉的范围直接判无效。

## 5. 文件级修改方案

### 5.1 `coverage_check.py` — 功能源头，第一优先级

#### A. 新增逐行 Git attribution

从 `be349add` 提取并适配：

- `_coalesce_line_ranges(line_numbers)`
- `parse_git_blame_porcelain(blame_text, selected_line_numbers=None)`
- `run_git_line_authors(repo_path, newgit, file_changes, repository_name="")`

但保留主干现有：

- `resolve_coverage_file()`
- `_PATH_INDEX_CACHE`
- `IncrementalService(...).path_index`
- ambiguity fail-closed

不要复制分支中退化的 suffix scan。

#### B. LCOV 解析保持旧 API 兼容

建议新增：

- `_add_lcov_function_range(...)`
- `normalize_lcov_function_ranges(...)`
- `parse_lcov_info_data(info_file)` → `(coverage_data, function_ranges)`
- `load_lcov_info_with_functions(info_path)` → `(merged_coverage, function_ranges, info_files)`

同时保留现有接口：

```python
parse_lcov_info(info_file) -> coverage_data
load_lcov_info(info_path) -> coverage_data, info_files
```

旧调用不需要修改；增量生成新链路才调用 `load_lcov_info_with_functions()`。

#### C. 升级单仓计算

`calculate_repository_coverage()` 新增可选参数：

```python
line_authors_by_file=None
function_ranges_data=None
```

计算流程改成：

1. `git diff` 得到新增行；
2. 现有 `git log` 仍用于 commit 元数据/兼容输出；
3. 对新增行执行 `git blame newgit`；
4. 每条 `details` 附加：
   - `author_name`
   - `author_email`
   - `reviewer`
   - `commit`
   - `committed_at`
   - `subject`
5. 只有 `status == 未覆盖` 的新增行进入 `reviewers_by_file`；
6. 当前 LCOV 文件存在可信函数范围时写入 `function_ranges_by_file`。

#### D. `schema_version` 升级到 3

新增字段但保留旧字段，避免不必要破坏：

```json
{
  "schema_version": 3,
  "details": [...],
  "uncovered_lines_by_file": {...},
  "review_lines_by_file": {...},
  "reviewers_by_file": {
    "/abs/src/foo.c": {
      "101": "Alice",
      "102": "Alice",
      "120": "Bob"
    }
  },
  "function_ranges_by_file": {
    "/abs/src/foo.c": [
      {"start_line": 80, "end_line": 155, "name": "foo"}
    ]
  },
  "developer_file_changes": [...],
  "developer_tasks": {...}
}
```

所有仓内消费者必须在 schema bump 同一 commit 内更新；如果发现外部程序硬编码只接受 v2，先做兼容读，再切 v3。

#### E. 开发人员任务从“碰过文件”变成“拥有具体行”

`build_developer_tasks()` 不再把整个文件未覆盖数复制给所有 commit author。

按 `details` 中的逐行 attribution 聚合：

- `owned_added_lines`
- `owned_line_numbers`
- `covered`
- `uncovered`
- `uncovered_line_numbers`
- `ignored`
- `missing`

人员 identity：优先 `author_email.lower()`，邮箱为空时才使用 `author_name.lower()`。

同时保留 commit 列表，便于追溯。

### 5.2 `app/incremental/service.py` / `path_index.py` — 统一路径解析

不要在 `enhance_coverage.py` 新增三套：

- `get_incremental_lines_for_report()` suffix scan
- `get_incremental_reviewers_for_report()` suffix scan
- `get_function_ranges_for_report()` suffix scan

建议扩展 `IncrementalService` 一个通用映射解析接口，例如逻辑上：

```python
resolve_mapping_value(report_file_path, mapping)
```

内部仍使用 `LCOVPathLookupIndex`：

1. exact
2. normalized
3. unique suffix
4. ambiguous → None
5. basename only → reject

`review_lines_by_file`、`reviewers_by_file`、`function_ranges_by_file` 都走同一个 resolver。

收益：

- 不复制分支的弱匹配实现；
- 多仓库同名文件不会串数据；
- 三类元数据共享同一 match semantics；
- 可以把 match_type 打进 debug 日志，便于现场追踪。

### 5.3 `source_reader.py` — 静态建议值 + 可信函数范围

#### A. `SourceLineDTO` 增加可选字段

```python
suggested_reviewer: str = ""
```

`to_dict()/from_dict()` 一并支持。

该字段是静态 Git 元数据，不等价于用户已保存 reviewer。

#### B. Parser 新增两个输入

建议：

```python
parse_source_lines_from_gcov_html(
    ...,
    suggested_reviewers_by_line=None,
    known_function_ranges=None,
)
```

优先直接使用函数参数中的 reviewer map；HTML 中 `data-coverage-reviewer` 只作为兼容 fallback。不要让“先写 HTML，再从 HTML 反解析”成为主数据通路。

#### C. 责任人参与 block 边界

连续未覆盖简单语句只有在 `suggested_reviewer` 相同的情况下允许自动合并。

例如：

```text
101 Alice
102 Alice   -> 同一分析块
103 Bob     -> 必须开启新分析块
```

否则一个分析块会同时包含两个开发人员的行，默认确认人语义错误。

#### D. reviewer 合并规则

首次 parse：

```python
reviewer = db_record.reviewer or suggested_reviewer
```

没有 DB record：

```python
reviewer = suggested_reviewer
```

数据库状态、草稿状态、coverage method/reason 仍按当前规则处理。

#### E. LCOV 范围优先、源码解析兜底

`known_function_ranges` 先转换成 `FunctionRange`，验证：

- `1 <= start <= end <= total_lines`
- 排序后不交叉
- 空/不完整列表不能假装成功

验证通过：直接使用；验证失败：调用当前 `extract_c_function_ranges(raw_lines)`。

函数名回填用一次 O(lines + functions) sweep，不要重新对每行扫描所有范围。

### 5.4 `app/inject/parse_once.py` — 必须适配，不能绕过

当前主干 Parse Once 是正式注入唯一解析通路，所以 LCOV 范围必须进入这里。

`ParsedSourceArtifact` / `parse_gcov_source_once()` 增加：

```python
incremental_lines=None
suggested_reviewers_by_line=None
known_function_ranges=None
```

并转发给 `parse_source_lines_from_gcov_html()`。

这样：

- SourceContext 只构造一次；
- line index 的 `function_name/function_hash` 直接使用最终函数范围；
- Sidecar v2 同一个 SourceContext 写出；
- 不恢复旧版本“HTML parser + source parser + line-index parser”三遍解析。

### 5.5 `enhance_coverage.py` — 编排层接线

#### A. 增量注入 worker

对每个报告文件只做一次安全解析：

```text
report_file_path
  ├─ resolve review lines
  ├─ resolve reviewers map
  └─ resolve function ranges
```

之后：

1. `mark_incremental_review_lines(content, lines, reviewers)` 给输出 HTML 写 `data-coverage-reviewer`，兼容 immediate/lazy DOM 模式；
2. 同时把 `suggested_reviewers_by_line` 直接传给 `InjectService.parse_once`；
3. `known_function_ranges` 直接传给 `InjectService.parse_once`；
4. line index 和 Sidecar 都使用 ParsedSourceArtifact，不单独再解析。

#### B. `mark_incremental_review_lines()`

签名改成：

```python
mark_incremental_review_lines(content, selected_line_numbers, reviewers_by_line=None)
```

重跑时先清理旧的：

- `data-coverage-review`
- `data-coverage-reviewer`

reviewer 使用 `html.escape(..., quote=True)`，防止 author name 破坏 HTML attribute。

#### C. 输出复用签名必须加入两项

在主干现有 `compute_directory_signature()` 的增量 manifest 机制上**追加**：

- `incremental_reviewer_set_hash`
- `function_range_set_hash`

最终 signature 至少包含：

```json
{
  "manifest_hash": "...",
  "incremental_review_set_hash": "...",
  "incremental_reviewer_set_hash": "...",
  "function_range_set_hash": "..."
}
```

原因：新增行集合相同，但 Git author 或 LCOV 函数范围变化时，不能误复用旧 Sidecar/HTML。

不要用 `be349add` 的简单目录遍历签名覆盖主干现有 `calculate_directory_signature_incremental()`。

#### D. `build_incremental_review_site()`

给 `inject_coverage_report()` 增加：

```python
incremental_reviewers_by_file=result.get("reviewers_by_file") or {}
function_ranges_by_file=result.get("function_ranges_by_file") or {}
```

第一阶段建议只在 `incremental` 链路启用 LCOV 函数范围，因为它天然已有 `--info`。不要为了这次功能同时扩大普通 `inject` CLI；全量模式复用 LCOV 范围可以放后续小版本。

### 5.6 `code_detail_service.py` — 动态 Overlay 不得清掉建议责任人

这是移植最容易漏掉的点。

当前 Overlay 刷新在 DB 没有 reviewer 时会写空值。修改成：

```python
if rec:
    line.reviewer = rec.get("reviewer") or line.suggested_reviewer
else:
    line.reviewer = line.suggested_reviewer
```

其他状态逻辑保持不变。

否则初始页面可能显示 Alice，一旦 data_version 刷新或 context cache 命中后 reviewer 又被清空。

### 5.7 `app/code_detail/sidecar_store.py` — 原则上无需重写

当前 v2 chunk 已使用：

```python
[line.to_dict() for line in context.lines]
```

所以 `SourceLineDTO.to_dict()` 新增 `suggested_reviewer` 后会自然持久化。

`function_ranges` 已在 `meta.json` 中持久化。

建议仅增加两类针对性测试，不修改格式版本：

- reviewer suggestion v2 round-trip；
- LCOV function range v2 round-trip。

由于新增字段为 optional，旧 v2 Sidecar 仍可读，不需要数据库/Sidecar migration。

### 5.8 `web/assets/js/coverage_enhance.js` — 只补兼容路径

Lazy Collapse 主路径已经用 `lineData.reviewer` 初始化 reviewer input，因此后端返回建议 reviewer 后可直接工作。

但是 `lazy` / `immediate` DOM 模式需要读取 `data-coverage-reviewer`。从分支提取最小能力：

```javascript
getSuggestedReviewer(item)
```

构建分析 block 时：

- 已加载 DB reviewer 优先；
- 无 DB reviewer 时使用 DOM suggested reviewer；
- 自动合并简单连续未覆盖行时 reviewer 不同必须断块。

不要把 `be349add` 的整份 JS 覆盖主干 v11.7 JS，避免丢失现有 RegionStore、批量渲染、取消、重试和 LRU 修复。

### 5.9 增量任务页 / JSON / Excel

沿用主干现有页面/静态资源组织，只更新数据字段和列，不搬分支页面结构。

#### `incremental_developer_tasks`

文案由：

> Git 提交作者 + 文件级未覆盖统计

改为：

> `newgit` 最终代码逐行 Git blame author + 该 author 自己的增量覆盖状态

显示：

- 本人新增行数
- 本人新增行号
- 本人待填写行数
- 本人待填写行号
- 相关 commit

#### Excel Details

新增：

- Developer
- Email
- Reviewer
- Blame Commit
- Commit Subject

#### Excel Developer Files

新增/明确：

- Owned Added Lines
- Owned Line Numbers
- Uncovered Need Fill
- Uncovered Line Numbers

## 6. 不需要做的事情

本次不要做：

1. 不整体 merge `be349add`。
2. 不把主干改成 `src/coverage_tool` package 结构。
3. 不替换 `LCOVPathLookupIndex`。
4. 不删除/重写 `AnalysisOverlayCache`。
5. 不绕过 `InjectService.parse_once`。
6. 不回退 SidecarStore v2 到旧 JSON 单文件。
7. 不新增 reviewer 数据库列。
8. 不改变现有 `coverage_analysis.reviewer` 的语义。
9. 不把 Git author 当成已确认结论。
10. 不运行无关全量 test suite。

## 7. 建议分 4 个小提交落地

### Commit 1 — `feat(incremental): add exact line ownership attribution`

范围：

- `coverage_check.py`
- `tests/incremental/` 新增逐行 blame/aggregation 针对性测试

完成：

- blame parser
- pinned newgit blame
- schema v3 details
- precise developer tasks
- single/multi repo attribution

先不碰 Code Detail。

### Commit 2 — `feat(incremental): reuse validated LCOV function ranges`

范围：

- `coverage_check.py`
- `source_reader.py`
- `app/inject/parse_once.py`
- `tests/incremental/`
- `tests/code_detail/test_phase2_core.py`

完成：

- LCOV complete range parser
- normalize/fallback
- parse-once plumbing

### Commit 3 — `feat(code-detail): propagate git suggested reviewer safely`

范围：

- `source_reader.py`
- `code_detail_service.py`
- `enhance_coverage.py`
- `app/code_detail/sidecar_store.py`（原则上仅测试；如无需源码修改就不动）
- `web/assets/js/coverage_enhance.js`
- `tests/code_detail/`

完成：

- suggested reviewer DTO
- DB > Git precedence
- reviewer split block
- Sidecar round-trip
- cache/output signature
- lazy/immediate compatibility

### Commit 4 — `feat(incremental-ui): expose line ownership in tasks and exports`

范围：

- `coverage_check.py` export writer
- `enhance_coverage.py` page data
- `web/assets/js/incremental_developer_tasks.js`（如当前页面由该脚本驱动）
- 对应 targeted tests

完成：

- developer task exact lines
- JSON/Excel/UI wording
- browser acceptance

分 4 个提交的好处是：任何阶段出问题都可以独立回退，不会把 Git、LCOV parser、Code Detail 和 UI 绑成一个超大 diff。

## 8. 针对性测试计划

### 8.1 Python compile / Python 3.6.8 兼容检查

只检查本次变化的 Python 文件：

- `coverage_check.py`
- `source_reader.py`
- `code_detail_service.py`
- `enhance_coverage.py`
- `app/inject/parse_once.py`
- 如修改 `app/incremental/service.py`，也包含它

重点禁止引入 Python 3.7+ 才支持的 API/语法到生产运行路径。

### 8.2 新增 `tests/incremental/test_line_ownership_and_lcov_ranges.py`

至少覆盖：

1. `git blame --line-porcelain` 正常解析；
2. `^commit` boundary 行；
3. 姓名/邮箱/subject；
4. 连续新增行合并 `-L`；
5. 碎片很多时整文件 blame + filter；
6. blame 缺少指定行 → fail closed；
7. Alice/Bob 同文件不同新增行，只拿各自行；
8. 只有 Bob 的未覆盖行进入 Bob 的待填写；
9. single repo；
10. multi repo 同名路径隔离；
11. schema v3；
12. FN complete range；
13. FNL/FNA aliases；
14. 传统 FN 无 end → 不使用范围；
15. crossing ranges → fallback；
16. 多 `.info` 范围合并。

### 8.3 复用当前主干现有相关测试

当前主干已经有：

- `tests/incremental/test_phase5_inject_path.py`
- `tests/code_detail/test_phase2_core.py`
- `tests/code_detail/test_phase6_sidecar.py`
- `tests/browser/coverage_real_browser.spec.js`

只跑这些与新增测试，不跑全仓库 discover。

### 8.4 Code Detail 必测场景

1. Git reviewer Alice，DB 无记录 → Alice；
2. Git reviewer Alice，DB reviewer Carol → Carol；
3. DB 记录被删除/无记录后 refresh → 恢复 Alice；
4. Alice 行与 Bob 行相邻 → 两个 block；
5. 折叠→展开 reviewer 不丢；
6. Sidecar v2 保存→重新加载 reviewer 不丢；
7. 已暂存草稿的 reviewer 不被 suggestion 覆盖；
8. 已确认 reviewer 不被 suggestion 覆盖。

### 8.5 LCOV / Lazy Collapse 必测场景

1. 有可信 LCOV range 时 mock `extract_c_function_ranges`，确认不调用源码扫描；
2. 不完整范围时确认调用源码扫描；
3. 函数名进入 line index `function_name/function_hash`；
4. function range 进入 Sidecar meta；
5. Code Detail layout 使用同一范围；
6. reviewer hash 改变时 source signature 改变；
7. function range hash 改变时 source signature 改变；
8. 新增行集合不变但 reviewer 改变时不能 reuse old output。

### 8.6 Browser acceptance

仅增加/执行与本功能直接相关场景：

- 增量页面打开时默认确认人正确；
- 相邻不同 author 的分析框没有错误合并；
- 保存后刷新仍使用数据库 reviewer；
- Lazy Collapse 折叠/重开不丢 reviewer；
- 不影响现有 expand/collapse/cancel/retry 行为。

## 9. 验收标准

### 逐行责任人

必须同时满足：

- 同一文件两个 author 的新增行能被精确拆分；
- developer task 只统计本人拥有的行；
- `details` 可追踪到 author/email/commit；
- 默认确认人来自 `newgit` blame；
- 人工 reviewer 永远优先；
- 多仓库同名文件不串。

### LCOV 函数范围

必须同时满足：

- 完整可信范围进入 Code Detail；
- 不可信范围自动回退；
- 不改变最终覆盖行集合；
- 不产生重叠/越界 Region；
- Sidecar/缓存身份包含范围变化。

### 主干架构不回退

必须确认：

- `LCOVPathLookupIndex` 仍是 canonical path resolver；
- `InjectService.parse_once` 仍是真实 runtime 路径；
- Sidecar v2 仍是第一读取顺序；
- OverlayCache 仍工作；
- 无新增 DB migration；
- 不引入第二套重复 parser/provider。

## 10. 主要风险与处理

### P1：路径映射串文件

风险：分支中的 `endswith()` 被直接抄回主干。

处理：所有新 metadata map 统一走 `LCOVPathLookupIndex`；ambiguous 必须 fail closed。

### P1：Overlay refresh 清空默认 reviewer

风险：首次显示正常，刷新/缓存命中后变空。

处理：`code_detail_service.py` 明确 `DB reviewer or suggested_reviewer`。

### P1：Sidecar/output 复用旧 reviewer

风险：新增行不变但 author 变化，旧 output 被复用。

处理：signature 强制增加 `incremental_reviewer_set_hash`。

### P1：部分 LCOV range 被误认为完整

风险：某些函数用 LCOV，其他函数消失，Lazy Collapse 分区错误。

处理：文件级完整性判断；任何结构冲突都回退源码 scanner。验收必须使用一份当前生产环境实际 `.info` fixture。

### P2：git blame 性能

风险：大 diff、大文件、碎片化行集合增加生成耗时。

处理：

- 只 blame 新增行；
- 连续行合并为 `-L`；
- 一个文件一个 Git 进程；
- 超过范围阈值整文件 blame 后过滤；
- 第一版不做高并发 blame，先测量再决定是否增加 bounded concurrency。

### P2：schema v3 外部兼容

风险：外部脚本硬编码 v2。

处理：合入前 grep 当前仓内消费者；若存在外部消费者，先保持旧字段并做兼容读，再更新版本检查。

## 11. 推荐实施顺序

最稳妥的顺序是：

```text
第一阶段：coverage_check 独立产出正确 schema v3
    ↓
第二阶段：LCOV 函数范围进入 parse_once / SourceContext
    ↓
第三阶段：suggested reviewer 进入 Sidecar + Code Detail
    ↓
第四阶段：UI / Excel / Developer Tasks
    ↓
针对性浏览器验收
    ↓
再进入平滑升级/生产发布治理
```

这样每一阶段都有独立的可验证输出，出现问题时容易定位，不会一次性把 Git、LCOV、Sidecar、前端全部混在一起。

## 12. 最终建议

建议以 `bfa63de` 为唯一开发基线，手工摘取 `be349add` 的**算法和测试意图**，不要 cherry-pick 整个 commit。

本次最关键的三个实现点是：

1. **`coverage_check.py` 负责生成行级 attribution 和可信 LCOV range；**
2. **`InjectService.parse_once` 是唯一静态元数据进入 SourceContext 的入口；**
3. **`CodeDetailService` 只叠加动态数据库状态，并始终保持 `DB reviewer > Git suggestion`。**

按此方式移植，可以得到 `be349add` 的新功能，同时保留 `bfa63de` 已经完成的路径安全、缓存、Sidecar、性能和运行时治理能力。