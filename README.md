# 覆盖率增强与持久化套件跨工程通用化部署指南

本增强套件采用**完全无侵害式 (Non-intrusive) 架构设计**。其他任何工程无需修改其现有的一行 C 语言源代码，也无需改动既有的 LCOV 覆盖率报告生成流水线，仅需在现成的 LCOV HTML 报告成果上执行“非破坏性注入”，即可获得具有 scope 感知、等宽列对齐、高度自适应多行文本框以及 MySQL 并发持久化的顶级覆盖率审计体验。

---

## 1. 快速集成结论

| 准备项 (Preparation) | 动作 (Action) | 说明 (Explanation) |
| :--- | :--- | :--- |
| **工具拷贝** | 复制套件的 4 个核心文件到新工程的任意目录中。 | `enhance_coverage.py`, `coverage_enhance.js`, `coverage_enhance.css`, `coverage_config.json` |
| **参数修改** | 编辑新工程的 `coverage_config.json` 配置文件。 | 指定新工程的名字 `project_name`，以及专用的 MySQL 连接及 API 端口。 |
| **步骤 A: 无损注入** | 跑一次 `inject` 命令行分发静态资源到 HTML 目录。 | `python enhance_coverage.py inject --dir <输入只读目录> --out <输出增强目录>` |
| **步骤 B: 启动存储** | 启动轻量级持久化微服务接收 Save 提交。 | `python enhance_coverage.py server` （会自动完成新库表的建表与热升级） |

---

## 2. 详细通用化操作步骤

### 第一步：组件迁移与文件拷贝
将当前工程 `scripts` 目录下的以下四个核心组件完整复制到您的新工程（例如新建一个 `coverage_tool` 目录）：

1. **`enhance_coverage.py`**：主控 Python 脚本。集成并提供了非侵害式 HTML 静态资源注入（`inject` 命令）以及支持高并发 CORS 的极轻量 API 桥接存储服务（`server` 命令）。
2. **`coverage_enhance.js`**：前端交互与对齐引擎。负责 scope 边界大括号精准切分、跨多行 Block 文本框自动 textarea 升级与高度自适应，以及 121 列等宽绝对对齐核心算法。
3. **`coverage_enhance.css`**：浮动隔离与 UI 视觉样式表。提供毛玻璃面板、多行文本框高度填充、动态表头高亮及 ch 绝对定位。
4. **`coverage_config.json`**：数据库与端口配置文件。

---

### 第二步：环境依赖准备
1. **Python 环境**：确保目标机器安装了 Python 3.x 环境。
2. **MySQL 数据库**：确保有可用的 MySQL 实例。
3. **驱动安装**：在执行存储服务器前，安装极轻量的驱动（两秒即可完成）：
   ```bash
   pip install pymysql
   ```

---

### 第三步：修改新工程配置 (`coverage_config.json`)
在新工程的配置文件中调整参数，使其独立于原工程：
```json
{
  "mysql": {
    "host": "localhost",
    "port": 3306,
    "user": "root",
    "password": "您的数据库密码",
    "database": "coverage"
  },
  "server": {
    "host": "0.0.0.0",
    "port": 9528
  },
  "project_name": "您的新工程名字 (例如 Gemini-Next)"
}
```
> [!TIP]
> *   多个工程可以**共用同一个 MySQL 数据库**（套件会自动在 `coverage_analysis` 表中通过 `project_name` 做物理数据区隔）；
> *   您也可以为新工程**指定一个全新的 `database` 名字**，`enhance_coverage.py` 在检测到该数据库不存在时，会**极其聪明地在 MySQL 中自动执行 CREATE DATABASE 和建表逻辑**，无需手动导表结构！

---

### 第四步：非破坏性注入 (`inject`)
在您的新工程生成原生 LCOV HTML 覆盖率报告（假设目录在 `build/coverage`）后，在命令行运行：
```bash
python enhance_coverage.py inject --dir build/coverage --out build/coverage_review
```
> [!IMPORTANT]
> *   `--dir` 参数传入原生只读覆盖率报告目录（套件不会改动其任意字节，保护原始数据）；
> *   `--out` 参数为全新增强版报告的输出目录；
> *   套件会自动在 `build/coverage_review/html` 下注入样式，并将最新的 JS 和 CSS 复制过去。您只需双击打开 `build/coverage_review/html/index.html` 即可畅游新工程报告！

---

### 第五步：启动持久化数据服务器 (`server`)
在后台/终端启动本地 API 数据存储桥梁：
```bash
python enhance_coverage.py server
```
*   服务器启动后将自动监听配置的 `9528` 端口；
*   点击新页面右侧的 `Save` 时，前端会发起并发批量 API 提交，将确认人、状态及原因落盘入库；再次刷新页面时，套件将自动从数据库读取并优雅回显！

---

## 3. 套件的工业级高级特性
*   **多工程平滑共存**：一张表容纳成百上千个工程的数据，极佳地支持了全公司覆盖率审计的集中化管理。
*   **全自动热升级**：一旦您在 `config` 中指定了库，微服务会自动校验并补齐类似 `reviewer` 等核心字段，无需 DBA 协助。
*   **不占用渲染带宽**：所有渲染均由浏览器客户端解析 JS 完成，API 交互采取极轻量 JSON 数据，零开销，极佳适配超大型工程。
