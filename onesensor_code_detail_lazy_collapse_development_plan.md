# OneSensor 代码详情页「待分析函数优先 + 懒加载折叠」详细开发方案

## 0. 文档定位

本方案用于指导开发人员在 **不改变现有待分析判定逻辑、不改变当前源码数据来源、不破坏现有代码行交互能力** 的前提下，对代码详情页进行性能改造。

本次第一阶段仅改造代码详情页，但后端区间计算、源码区间读取、标准行数据、前端 Renderer 均按可复用能力设计，便于后续其他源码展示页面接入。

---

# 1. 需求目标

## 1.1 核心目标

当前代码详情页如果一次性加载并渲染大文件，会存在：

- HTML / JSON 响应体较大；
- 初始 DOM 节点过多；
- 浏览器布局、绘制压力大；
- 页面首次打开耗时长；
- 滚动、筛选、点击等交互容易卡顿；
- 大段与当前分析无关的代码占用屏幕空间。

本次改造目标：

1. 默认只加载并展示真正需要分析的代码区域；
2. 大段非待分析源码初始不下载、不渲染；
3. 折叠区域由用户点击后再从后端按需加载；
4. 已加载代码继续复用现有行级功能；
5. 大区域和“展开全部”必须分批渲染，避免长时间阻塞主线程；
6. 用户当前分析过程中不因状态变化而突然重排页面；
7. 所有折叠逻辑统一由后端计算，前端只负责显示和加载。

---

# 2. 已确认的业务规则

## 2.1 待分析判定

完全复用系统当前已有“待分析行”判定。

禁止在本次功能中额外定义：

- 新的 coverage 判断；
- 新的 analysis status 判断；
- 新的“是否需要展示”规则。

代码详情页、统计数据、筛选结果、折叠展开逻辑应尽量使用同一个事实来源。

---

## 2.2 默认展开范围

### 场景 A：待分析行能够映射到函数

只要函数中存在至少一条待分析行：

```text
整个函数完整展开
```

不限制函数大小。

例如：

```text
foo():
  start_line = 200
  end_line   = 1520

待分析行 = 866
```

最终默认展开：

```text
200 - 1520
```

即使函数超过 1000 行，也不自动截断。

---

### 场景 B：待分析行无法识别函数边界

采用保守兜底：

```text
[line - 20, line + 20]
```

并裁剪文件边界。

例如：

```text
文件总行数 = 2000
待分析行   = 10
```

展开：

```text
1 - 30
```

待分析行：

```text
1995
```

展开：

```text
1975 - 2000
```

---

## 2.3 默认展开区间合并

后端统一对展开区间排序和合并。

规则：

```text
两个展开区间之间 gap <= 20 行
=> 合并
```

例如：

```text
100 - 180  展开
181 - 192  无待分析
193 - 260  展开
```

中间间隔 12 行：

```text
最终：100 - 260 整体展开
```

如果：

```text
100 - 180
181 - 220   共 40 行
221 - 300
```

则最终：

```text
100 - 180   expanded
181 - 220   collapsed
221 - 300   expanded
```

`20` 为固定值，不配置化。

---

## 2.4 无待分析行文件

如果整个文件没有待分析行：

```text
1 - total_lines
```

全部为折叠区域。

页面默认显示：

```text
该文件暂无待分析代码

第 1 - 2386 行
已折叠 2386 行
点击展开
```

---

## 2.5 手动展开 / 折叠

用户可以：

- 点击折叠区展开；
- 已展开区域再次收起；
- 默认展开的分析区域也允许手工折叠。

如果多个函数由于 `gap <= 20` 合并成同一个默认展开 Region：

```text
按合并后的 Region 整体折叠
```

不再细分为函数级折叠状态。

---

## 2.6 状态不持久化

用户手工展开 / 折叠状态：

- 不入数据库；
- 不保存用户偏好；
- 不写 localStorage；
- 仅存在于当前页面。

刷新或重新进入：

```text
重新由后端根据最新待分析状态计算
```

---

## 2.7 用户完成分析后的行为

用户在当前页面完成最后一条待分析行后：

```text
当前页面不立即折叠
```

目的：

- 防止代码突然消失；
- 防止滚动位置变化；
- 防止用户误以为操作失败。

刷新 / 重新进入后，再重新计算默认折叠。

---

# 3. 总体架构

推荐架构：

```text
┌──────────────────────────────┐
│ 当前待分析行判定逻辑          │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ 当前函数 / Block 识别能力     │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ CodeRegionBuilder             │
│ 计算 expanded / collapsed     │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ 页面初始化仅返回 Region Layout│
└──────────────┬───────────────┘
               │
               ▼
┌───────────────────────────────────────┐
│ 前端：一次 Batch 请求加载默认展开区域 │
└──────────────┬────────────────────────┘
               │
               ▼
┌──────────────────────────────┐
│ 标准 Line DTO                 │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ 唯一 CodeLine Renderer        │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ 分批生成 DOM                  │
└──────────────────────────────┘
```

折叠区点击：

```text
Placeholder
   │
   │ click
   ▼
Range API
   │
   ▼
Line DTO
   │
   ▼
缓存
   │
   ▼
分批 DOM Renderer
```

---

# 4. 后端开发方案

# 4.1 新增：CodeRegionBuilder

建议新增独立模块。

职责：

```text
输入：
- total_lines
- pending_lines
- function_ranges

输出：
- 完整、连续、无重叠的 regions
```

建议接口：

```python
build_code_regions(
    total_lines: int,
    pending_lines: list[int],
    function_ranges: list[FunctionRange],
) -> list[CodeRegion]
```

---

## 4.2 数据结构

### FunctionRange

```python
class FunctionRange:
    start_line: int
    end_line: int
    name: str | None
```

### CodeRegion

```python
class CodeRegion:
    region_id: str
    start_line: int
    end_line: int
    default_state: str
    kind: str
    label: str | None
```

建议：

```text
default_state:
- expanded
- collapsed
```

```text
kind:
- analysis
- collapsed
```

不要把过多 UI 概念写入后端 DTO。

---

# 4.3 Region Builder 算法

伪代码：

```python
def build_code_regions(total_lines, pending_lines, function_ranges):

    expanded_ranges = []

    for pending_line in pending_lines:

        function = find_function_containing_line(
            pending_line,
            function_ranges
        )

        if function:
            expanded_ranges.append(
                (function.start_line, function.end_line)
            )
        else:
            expanded_ranges.append(
                (
                    max(1, pending_line - 20),
                    min(total_lines, pending_line + 20)
                )
            )

    expanded_ranges = sort_ranges(expanded_ranges)

    expanded_ranges = merge_overlap_ranges(
        expanded_ranges
    )

    expanded_ranges = merge_small_gap_ranges(
        expanded_ranges,
        max_gap=20
    )

    regions = fill_collapsed_gaps(
        total_lines,
        expanded_ranges
    )

    return regions
```

---

## 4.4 函数匹配性能

不要对每个 pending line 都遍历全部函数。

错误实现：

```python
for line in pending_lines:
    for fn in functions:
        ...
```

如果：

```text
10000 pending lines
5000 functions
```

会产生大量无意义比较。

推荐：

### 方案 1：函数区间按 start_line 排序 + 二分

```text
O(P log F)
```

其中：

```text
P = pending line 数
F = function 数
```

### 方案 2：已有数据库索引能力

如果系统当前已经能直接：

```text
line -> function/block
```

则直接复用，不重复做解析。

优先复用已有结果。

---

# 4.5 Region 合并实现

先合并重叠：

```text
[100, 200]
[150, 300]

=>

[100, 300]
```

再合并 gap <= 20：

```text
current = [100, 200]
next    = [218, 300]

gap = 17

=>

[100, 300]
```

注意 gap 计算：

```python
gap = next.start_line - current.end_line - 1
```

所以：

```text
current end = 180
next start  = 201

gap = 20
```

应该合并。

而：

```text
next start = 202

gap = 21
```

不合并。

此处属于典型 off-by-one 风险，必须单测。

---

# 4.6 补齐 collapsed regions

例如 expanded：

```text
100-200
500-600
```

文件：

```text
1-1000
```

最终 regions：

```text
1-99       collapsed
100-200    expanded
201-499    collapsed
500-600    expanded
601-1000   collapsed
```

必须保证：

```text
regions[0].start_line == 1
regions[-1].end_line == total_lines
```

且：

```text
previous.end_line + 1 == current.start_line
```

---

# 4.7 Region ID

不要依赖数组 index。

建议：

```text
r-1-99
r-100-200
r-201-499
```

或者服务端生成稳定字符串：

```python
f"region-{start_line}-{end_line}"
```

当前页面生命周期足够稳定。

不需要持久化 UUID。

---

# 5. 源码读取层改造

当前代码详情页已有完整源码读取能力。

本次不要替换底层源码来源，只抽象出：

```python
read_source_lines(
    source_context,
    start_line,
    end_line
)
```

以及：

```python
read_source_ranges(
    source_context,
    ranges
)
```

---

## 5.1 单区间读取

推荐：

```python
read_source_lines(start_line=100, end_line=500)
```

必须支持：

```text
start == end
start == 1
end == total_lines
```

并校验：

```text
1 <= start <= end <= total_lines
```

---

## 5.2 批量读取

初始化默认展开区域：

```python
read_source_ranges([
    (100, 260),
    (900, 1050),
    (3000, 4500),
])
```

目标：

```text
一次请求
一次后端调用链
```

不要由前端一个 region 发一个请求。

---

# 6. API 设计

具体 URL 根据现有项目风格落地。

---

## 6.1 页面 Layout

### GET

```text
/api/.../code-layout
```

或者继续跟随原页面初始化接口返回。

响应示例：

```json
{
  "file_id": 123,
  "total_lines": 2386,
  "regions": [
    {
      "region_id": "region-1-96",
      "start_line": 1,
      "end_line": 96,
      "default_state": "collapsed",
      "kind": "collapsed",
      "label": null
    },
    {
      "region_id": "region-97-284",
      "start_line": 97,
      "end_line": 284,
      "default_state": "expanded",
      "kind": "analysis",
      "label": "foo()"
    }
  ]
}
```

---

## 6.2 默认展开区域 Batch API

### POST

```text
/api/.../code-lines/batch
```

请求：

```json
{
  "ranges": [
    {
      "start_line": 97,
      "end_line": 284
    },
    {
      "start_line": 900,
      "end_line": 1050
    }
  ]
}
```

返回：

```json
{
  "ranges": [
    {
      "start_line": 97,
      "end_line": 284,
      "lines": [...]
    },
    {
      "start_line": 900,
      "end_line": 1050,
      "lines": [...]
    }
  ]
}
```

---

## 6.3 单区域加载 API

### GET 或 POST

```text
/api/.../code-lines?start_line=285&end_line=899
```

返回标准：

```json
{
  "start_line": 285,
  "end_line": 899,
  "lines": [...]
}
```

但前端公共 loader 最好支持 chunk。

---

# 7. Line DTO 统一

这是本次改造的核心工程要求之一。

不要出现：

```text
旧页面 Line 数据
新懒加载 Line 数据
```

两套结构。

应该：

```text
一个 Line DTO
```

示意：

```json
{
  "line_no": 128,
  "source": "if (foo) {",
  "coverage_state": "...",
  "analysis_state": "...",
  "is_pending_analysis": true,
  "analysis_result": null,
  "reason": null,
  "remark": null
}
```

实际字段直接复用现有代码详情页。

---

# 8. 前端架构

建议拆成 4 个模块：

```text
CodeRegionStore
CodeRegionLoader
CodeLineRenderer
CodeRegionController
```

---

## 8.1 CodeRegionStore

负责纯状态：

```javascript
{
    id,
    startLine,
    endLine,
    defaultState,
    currentState,
    loaded,
    loading,
    lines,
    error
}
```

推荐状态：

```text
collapsed-unloaded
loading
expanded-loaded
collapsed-loaded
error
```

---

## 8.2 CodeRegionLoader

负责：

- Batch 加载；
- 单区间加载；
- 大区间 chunk 加载；
- 请求去重；
- 错误处理；
- 缓存写入。

不要负责 DOM。

---

## 8.3 CodeLineRenderer

唯一职责：

```text
Line DTO -> DOM
```

例如：

```javascript
renderCodeLine(lineData)
renderCodeLines(lines, container)
```

必须优先抽取当前代码详情页已有的行渲染实现。

当前所有：

- coverage 色彩；
- 待分析状态；
- 行号；
- 点击；
- 分析动作；
- 高亮；
- 备注；
- tooltip；

都通过同一个 Renderer。

---

## 8.4 CodeRegionController

负责：

- 点击展开；
- 点击折叠；
- 展开全部；
- 恢复默认；
- 批量 render 调度；
- Placeholder 更新。

---

# 9. 页面首次加载流程

## Step 1

获取：

```text
Layout
```

只渲染 Region 骨架。

例如：

```html
<div data-region="region-1-96">
    collapsed placeholder
</div>

<div data-region="region-97-284">
    loading placeholder
</div>
```

此时不加载折叠源码。

---

## Step 2

前端收集：

```javascript
regions.filter(r => r.defaultState === "expanded")
```

生成一个 batch request。

---

## Step 3

后端一次返回所有默认展开区域。

---

## Step 4

写入 Region cache。

---

## Step 5

分批 Renderer。

目标：

```text
每批约 300 - 500 行
```

但不要把数字硬编码散落各处。

建议：

```javascript
const RENDER_BATCH_SIZE = 400;
```

它是实现参数，不是业务规则。

---

# 10. 分批 DOM 渲染

不要：

```javascript
container.innerHTML = hugeHtml;
```

直接一次塞数万行。

推荐：

```javascript
async function renderLinesInBatches(lines, container) {
    const batchSize = 400;

    for (let i = 0; i < lines.length; i += batchSize) {
        const batch = lines.slice(i, i + batchSize);

        const fragment = document.createDocumentFragment();

        for (const line of batch) {
            fragment.appendChild(renderCodeLine(line));
        }

        container.appendChild(fragment);

        await yieldToBrowser();
    }
}
```

yield：

```javascript
function yieldToBrowser() {
    return new Promise(resolve => {
        requestAnimationFrame(() => resolve());
    });
}
```

如果当前浏览器兼容要求允许，可以后续评估：

```text
scheduler.yield()
requestIdleCallback()
```

第一版优先用成熟、安全方式。

---

# 11. 单个折叠区展开

用户点击：

```text
第 1000 - 6000 行
已折叠 5001 行
```

逻辑：

```text
if loaded:
    直接从 cache render
else:
    设置 loading
    分 chunk 加载
    cache
    分批 render
```

---

# 12. 超大区域分 Chunk

建议：

```javascript
const LOAD_CHUNK_SIZE = 500;
```

例如：

```text
1000 - 6000
```

内部：

```text
1000 - 1499
1500 - 1999
2000 - 2499
...
```

用户仍只点一次。

UI：

```text
正在展开 1500 / 5001 行
```

不要要求用户点多次“继续加载”。

---

# 13. 前端缓存

Region：

```javascript
region.loaded = true
region.lines = [...]
```

收起：

```text
移除行 DOM
保留 region.lines
```

再次展开：

```text
不访问后端
直接重新 render
```

这样减少：

- 重复网络请求；
- 后端压力；
- 用户等待。

---

# 14. 默认展开区域也允许折叠

每个 expanded Region 顶部建议增加轻量操作。

例如：

```text
▾ foo() 等分析区域 · 第 100-260 行
```

点击：

```text
移除 Region 内代码行 DOM
保留 cache
显示 collapsed placeholder
```

如果是多个函数合并：

```text
整体收起
```

不要拆分成函数级独立状态。

---

# 15. 展开全部

顶部提供：

```text
展开全部
恢复默认折叠
```

---

## 15.1 展开全部逻辑

不能：

```text
一次下载整个文件
一次创建所有 DOM
```

推荐：

```text
按文件顺序遍历所有 Region
```

对尚未 loaded：

```text
chunk load
```

对已 loaded：

```text
直接 render
```

每次 render：

```text
300 - 500 行
```

期间更新进度：

```text
正在展开 6400 / 18500 行
```

按钮进入：

```text
disabled / busy
```

避免重复启动。

---

## 15.2 展开全部错误处理

如果某一个 Region 加载失败：

不要：

```text
整个展开任务彻底中断
```

建议：

```text
当前 region 标记 error
继续后面的 region
```

最后提示：

```text
已展开 17 个区域，1 个区域加载失败
```

失败 region 保留：

```text
点击重试
```

---

# 16. 恢复默认折叠

恢复默认不是刷新页面。

根据：

```javascript
region.defaultState
```

进行：

```text
default expanded
=> 展开

default collapsed
=> 收起
```

已 loaded 的数据继续放在内存 cache。

因此：

```text
恢复默认
-> 用户再次展开
```

不重复访问后端。

---

# 17. Placeholder UI

推荐文案：

普通折叠：

```text
第 285 - 899 行 · 已折叠 615 行 · 点击展开
```

无待分析：

```text
该文件暂无待分析代码
第 1 - 2386 行 · 已折叠 2386 行
```

默认展开区域被手动收起：

```text
分析区域 · 第 100 - 260 行 · 已折叠 161 行
```

加载中：

```text
正在加载…
```

失败：

```text
加载失败 · 点击重试
```

整个 placeholder 区域都应可点击。

---

# 18. 搜索行为

已确认：

```text
只搜索当前已加载代码
```

不请求后端全文搜索。

因此需要避免当前搜索代码依赖：

```text
document 全量源码必然存在
```

改造后：

```text
仅遍历当前 loaded region
```

或当前渲染 DOM。

建议搜索加载过但当前收起的数据时：

### 第一阶段最简单实现

只搜索：

```text
当前已经渲染出来的代码
```

如果现有搜索实现天然如此，可保持。

不要为本次需求额外扩大搜索能力。

---

# 19. 行号跳转 / 锚点

如果目标位于 collapsed region：

```text
只滚动到 placeholder
```

不自动加载。

例如目标：

```text
line 8520
```

查找：

```text
region.start <= 8520 <= region.end
```

然后：

```javascript
placeholder.scrollIntoView()
```

可以在 placeholder 显示：

```text
目标行 8520 位于此折叠区域
```

但不是必须。

---

# 20. 当前分析操作兼容

懒加载区域展开后，必须完整支持原有分析能力。

重点验证：

- 点击某行分析；
- 状态更新；
- 备注；
- 原因；
- coverage 标签；
- 人工确认；
- 行高亮；
- 统计联动。

原则：

```text
懒加载只改变“代码什么时候出现”
```

不能改变：

```text
代码出现以后能做什么
```

---

# 21. 页面生命周期

页面第一次加载：

```text
Backend Layout
↓
Initial Batch
↓
Render
```

用户操作：

```text
expand / collapse / analyze / search
```

当前页面不重新计算 Region。

刷新：

```text
重新调用 Backend Layout
```

这样：

```text
新的待分析状态
=> 新的 Region
```

---

# 22. 错误处理

至少处理以下情况。

## 22.1 Layout 获取失败

页面显示：

```text
代码布局加载失败
重新加载
```

不要自动退化成把整份源码一次性塞回 DOM，避免隐藏性能问题。

---

## 22.2 Initial Batch 失败

每个默认展开 Region：

```text
显示加载失败
点击重试
```

页面其余区域仍可操作。

---

## 22.3 单区间失败

Region 状态：

```text
error
```

UI：

```text
加载失败 · 点击重试
```

---

## 22.4 Batch 中部分失败

后端最好返回：

```json
{
  "ranges": [...],
  "errors": [...]
}
```

允许成功部分继续显示。

---

# 23. 请求并发控制

“展开全部”时不能几十个请求同时打后端。

建议第一版：

```text
顺序加载
```

或者：

```text
并发数 <= 2
```

优先稳定。

不建议第一版做高并发预取。

---

# 24. 后端安全

所有新区间读取接口：

必须继续执行当前详情页相同的：

- 登录校验；
- 项目权限；
- Scan / 文件权限；
- 文件合法性验证。

客户端不能直接自由读取服务器任意路径。

禁止：

```text
GET /source?path=/etc/passwd
```

接口应通过现有：

```text
file_id / scan_id / coverage file identity
```

确定数据源。

---

# 25. 输入校验

必须拒绝：

```text
start_line <= 0
end_line <= 0
start_line > end_line
end_line > total_lines
超大异常 ranges 数组
重复恶意 ranges
```

Batch API 需要设置合理最大 ranges 数。

例如：

```text
<= 100 regions / request
```

实际阈值根据当前数据规模调整。

这属于安全保护，不改变正常页面行为。

---

# 26. 性能日志

建议为此次功能增加轻量性能日志。

后端记录：

```text
layout_build_ms
pending_line_count
region_count
expanded_region_count
expanded_line_count
collapsed_line_count
batch_load_ms
```

前端开发环境或可控日志记录：

```text
layout received
initial batch received
first lines rendered
initial render complete
```

便于实际确认瓶颈到底在：

```text
DB
源码读取
网络
DOM
```

---

# 27. 测试方案

# 27.1 Region Builder 单元测试

至少包含：

### Case 1

```text
无 pending line
```

预期：

```text
whole file collapsed
```

### Case 2

一个待分析函数。

### Case 3

多个待分析函数。

### Case 4

重复 pending line 位于同一函数。

预期：

```text
只有一个 expanded range
```

### Case 5

两个函数 gap = 20。

预期：

```text
merge
```

### Case 6

gap = 21。

预期：

```text
not merge
```

### Case 7

待分析行不属于函数。

预期：

```text
±20
```

### Case 8

待分析行在第一行附近。

### Case 9

待分析行在最后一行附近。

### Case 10

两个 fallback range 重叠。

### Case 11

fallback 与 function range 重叠。

### Case 12

超大函数。

预期：

```text
whole function expanded
```

### Case 13

函数范围异常数据。

需要明确：

```text
忽略非法范围 + 日志
```

不要导致页面 500。

### Case 14

最终 regions：

```text
无空洞
无重叠
连续
覆盖 1-total_lines
```

---

# 27.2 API 测试

### Batch

- 单 range；
- 多 range；
- 首行；
- 尾行；
- 非法范围；
- 空 ranges；
- 权限不足；
- 文件不存在；
- 数据和原详情页一致。

### Single range

同上。

---

# 27.3 前端测试

重点：

1. 初始 collapsed 源码不在 DOM；
2. 默认 expanded 一次 batch 加载；
3. Batch 不出现 N+1；
4. 点击 collapsed 后才请求；
5. 第二次展开不请求；
6. collapse 后 cache 保留；
7. 默认 expanded 可手动 collapse；
8. merged region 整体收起；
9. expand all 分批；
10. restore default 正确；
11. 单区域失败可重试；
12. initial batch 部分失败页面不崩；
13. 搜索不加载折叠代码；
14. 跳转折叠行只定位 placeholder；
15. 分析功能在 lazy region 正常。

---

# 28. 性能测试数据集

至少准备：

```text
A. 1000 行
B. 10000 行
C. 50000 行
D. 100000 行
```

分别模拟：

### 数据集 1

```text
0 个待分析行
```

### 数据集 2

```text
少量待分析
约 1% 代码需展开
```

### 数据集 3

```text
多个大函数待分析
约 30% 展开
```

### 数据集 4

```text
极端情况
90% 代码默认展开
```

场景 4 用于验证：

```text
虽然业务规则要求完整展开
但分批 DOM 是否仍能避免长时间冻结
```

---

# 29. 性能验收指标

这些作为目标，不作为脱离机器环境的绝对门槛。

## 初始 DOM

关键指标：

```text
初始代码行 DOM 数
≈
默认展开代码行数
```

不能：

```text
≈ 文件总行数
```

---

## 首次可操作

目标：

```text
约 <= 2 秒
```

生产环境以真实数据为准。

---

## 普通折叠块展开

期望：

```text
约 500 ms 内开始看到内容
```

不要求所有内容 500 ms 内全部完成。

---

## 超大区间

要求：

```text
持续渐进显示
```

并且：

```text
页面不能数秒完全无响应
```

---

## Expand All

展开期间：

- 页面按钮仍有响应；
- 滚动不应完全冻结；
- 无持续数秒的单个 Long Task。

---

# 30. 浏览器 Performance 检查

重点观察：

```text
Scripting
Rendering
Painting
Long Task
DOM node count
```

改造前后对比：

```text
打开同一个大文件
```

记录：

- DOM nodes；
- JS 执行时间；
- Layout；
- 首屏耗时；
- 展开操作耗时。

---

# 31. 实施阶段

推荐拆成 6 个阶段。

---

## Phase 1：后端基础能力

开发：

- CodeRegionBuilder；
- region 单测；
- range source reader；
- range API；
- batch API。

此阶段不修改前端页面逻辑。

验收：

```text
后端接口可以稳定返回正确 region 和标准行数据
```

---

## Phase 2：抽取统一 Renderer

从现有代码详情页抽取：

```text
Line DTO -> DOM
```

保证原页面行为不变。

这是低风险重构。

验收：

```text
旧展示方式下所有现有功能完全正常
```

---

## Phase 3：Region 页面骨架

前端改为：

```text
先渲染 region placeholder
```

但默认 expanded 仍可先一次性调用 batch。

验收：

```text
collapsed 区域不出现代码 DOM
```

---

## Phase 4：懒加载

实现：

- 点击加载；
- cache；
- 再折叠；
- 重试；
- loading 状态。

验收：

```text
未点击 collapsed 区域不发送源码请求
```

---

## Phase 5：大文件优化

实现：

- chunk loader；
- batch renderer；
- yield；
- Expand All；
- Restore Default。

验收：

```text
50000 行文件不会因一次 DOM 插入长时间冻结
```

---

## Phase 6：兼容与回归

检查：

- 搜索；
- 跳转；
- 分析；
- 高亮；
- 所有当前行能力。

---

# 32. 建议开发任务拆分

可以直接用于开发任务单。

## Backend-01

实现 `CodeRegionBuilder`

## Backend-02

实现函数区间快速映射

## Backend-03

实现 `read_source_lines`

## Backend-04

实现 `read_source_ranges`

## Backend-05

实现 layout API

## Backend-06

实现 batch lines API

## Backend-07

实现 single/chunk range API

## Backend-08

增加 range 权限与参数校验

## Backend-09

后端单元测试

---

## Frontend-01

抽取 `CodeLineRenderer`

## Frontend-02

实现 `CodeRegionStore`

## Frontend-03

实现 Region Placeholder

## Frontend-04

实现 Initial Batch Loader

## Frontend-05

实现分批 Renderer

## Frontend-06

实现单 Region Lazy Load

## Frontend-07

实现 Cache + Re-expand

## Frontend-08

实现默认展开 Region 手动折叠

## Frontend-09

实现 Expand All

## Frontend-10

实现 Restore Default

## Frontend-11

适配 Search

## Frontend-12

适配 Line Jump / Anchor

## Frontend-13

前端错误处理

## Frontend-14

性能测试与调优

---

# 33. 推荐代码组织

以现有项目实际目录为准。

概念结构：

```text
backend/
  coverage/
    code_region.py
    source_reader.py
    code_detail_service.py
    api/
      code_detail.py

frontend/
  code-detail/
    codeRegionStore.js
    codeRegionLoader.js
    codeRegionController.js
    codeLineRenderer.js
    codeDetail.js
```

不要为了严格匹配这个目录而强行大规模移动现有代码。

核心是：

```text
职责边界清楚
```

---

# 34. 回滚设计

本次建议保留 Feature Flag：

```text
CODE_DETAIL_LAZY_COLLAPSE
```

用途：

```text
新代码上线
↓
发生严重问题
↓
快速恢复旧页面行为
```

Feature Flag 只是发布安全开关。

不是业务配置。

业务规则：

```text
gap=20
fallback=±20
```

仍然固定。

如果项目目前没有成熟 Feature Flag 系统，可以使用现有配置方式实现一个临时开关。

---

# 35. 发布策略

推荐：

### Step 1

开发环境：

```text
开启
```

### Step 2

测试环境：

```text
开启
```

执行功能 + 性能回归。

### Step 3

生产：

先保持：

```text
可快速关闭
```

### Step 4

确认稳定后：

```text
默认开启
```

---

# 36. 数据库影响

按当前方案：

```text
不需要新增业务表
不需要保存折叠状态
```

如果现有函数 / Block 数据已经存在，则数据库不需要结构变更。

如果函数边界当前只在页面运行时临时解析，也建议优先复用，而不是为了本次折叠功能新增复杂数据库模型。

---

# 37. 对现有性能的影响

## 正面

明显减少：

- 初始源码读取量；
- 初始 HTML / JSON；
- DOM；
- layout；
- paint；
- 大文件滚动压力。

---

## 可能新增的成本

后端新增：

```text
Region 计算
```

但通常只是：

```text
排序 + 区间合并
```

复杂度很低。

浏览器新增：

```text
Region 状态管理
```

也远小于一次维护数万 DOM 节点的成本。

---

# 38. 关键风险

## 风险 1：函数边界识别不准确

处理：

```text
无法映射 => ±20
```

保证待分析行不会被完全折叠掉。

---

## 风险 2：新旧行渲染不一致

处理：

```text
唯一 Renderer
```

禁止复制 HTML 模板。

---

## 风险 3：Batch response 太大

本阶段接受：

```text
一次 batch 返回全部默认展开区域
```

因为这是已确认产品要求。

先通过：

```text
分批 DOM 渲染
```

解决主要前端卡顿。

如果后续实测网络响应体成为瓶颈，再做第二阶段流式 / 分页优化。

---

## 风险 4：展开全部重新制造卡顿

处理：

```text
chunk request
+
batch render
+
yield main thread
```

---

## 风险 5：用户操作后区域突然消失

处理：

```text
当前页面不动态重算 Region
```

---

# 39. 不属于本次范围

第一阶段明确不做：

- 全文后端搜索；
- 虚拟滚动；
- 用户折叠偏好持久化；
- 其他代码展示页面全面迁移；
- 新的源码版本管理；
- 新的待分析规则；
- 自动展开搜索命中区域；
- 超大函数截断；
- 自动动态重新折叠。

这些可以根据上线效果进入后续版本。

---

# 40. 建议验收清单

开发完成后逐项确认：

- [ ] 默认展开只来自现有待分析规则
- [ ] 待分析行所在整个函数展开
- [ ] 无函数边界使用 ±20
- [ ] gap <= 20 合并
- [ ] gap = 21 不合并
- [ ] 无待分析文件全部折叠
- [ ] collapsed 代码初始不在 DOM
- [ ] collapsed 代码初始未请求
- [ ] 初始 expanded 只发一个 batch request
- [ ] 默认 expanded 完整支持原行级功能
- [ ] lazy loaded 行完整支持原行级功能
- [ ] collapsed 区可点击展开
- [ ] 已 loaded 区再次展开不请求后端
- [ ] 默认 expanded 区可手动收起
- [ ] merged region 整体收起
- [ ] 超大区域内部 chunk
- [ ] 超大区域分批 render
- [ ] 展开全部分批执行
- [ ] 恢复默认正确
- [ ] 搜索不触发未加载区请求
- [ ] 行号跳转不自动展开
- [ ] 分析完成当前页面不突然重排
- [ ] 刷新后按最新状态重新计算
- [ ] API 具有权限校验
- [ ] API 具有行号边界校验
- [ ] 50000 行真实文件完成性能测试
- [ ] 有生产快速回滚开关

---

# 41. 推荐验收场景

最终上线前至少使用以下真实场景：

### 场景 A

```text
50000 行文件
无待分析代码
```

预期：

```text
页面只显示一个/少量 collapsed placeholder
```

### 场景 B

```text
50000 行文件
只有一个 100 行函数待分析
```

预期：

```text
初始源码 DOM 约 100 行，而非 50000 行
```

### 场景 C

```text
两个待分析函数间隔 20 行
```

预期：

```text
自动合并
```

### 场景 D

```text
间隔 21 行
```

预期：

```text
中间折叠
```

### 场景 E

```text
待分析行位于无法识别函数的宏/全局代码
```

预期：

```text
±20
```

### 场景 F

```text
一个待分析函数 5000 行
```

预期：

```text
完整加载该函数
但 DOM 分批生成
```

### 场景 G

```text
点击 10000 行 collapsed region
```

预期：

```text
一次用户操作
内部多批加载
逐渐显示
页面保持可响应
```

---

# 42. 最终实现原则

本次改造最重要的四条原则：

## 原则 1

```text
后端决定哪些代码默认展示
```

## 原则 2

```text
未展开代码不进入 DOM
```

## 原则 3

```text
所有代码行只使用一个 Renderer
```

## 原则 4

```text
所有大规模 DOM 操作必须分批执行
```

只要实现过程中始终遵守这四条原则，本次改造就能在不改变现有分析业务逻辑的情况下，真正解决代码详情页大文件带来的渲染性能和视觉噪音问题。
