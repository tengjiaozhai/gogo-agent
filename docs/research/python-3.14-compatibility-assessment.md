# Python 3.14 稳定性与全阶段迁移兼容性调研报告

> 调研日期：2026-09-23  
> 涉及项目：GoGo Agent (Python 重构版)  
> 核心问题：Python 3.14 是否稳定？是否会影响从阶段 A 到阶段 I 的各阶段实现？

---

## 一、 核心结论速览

1. **基本可用，但“生态成熟度”存在隐性暗礁**：
   * CPython 3.14 解释器本身与核心现代库（AgentScope 2.0.8, FastAPI, Pydantic v2, SQLAlchemy 2.0）已具备 3.14 的运行能力，基础的 Agent、Toolkit 与 HTTP 链路可以跑通。
   * 但是，Python 3.14 引入了 **PEP 649（类型注解惰性求值）**，对重度依赖运行时反射（Reflection）的框架构成了破坏性行为变更；同时彻底不再支持 Pydantic v1。
2. **对后续阶段的直接影响**：
   * **阶段 A ~ F（核心 Agent、API、差旅业务、预订幂等）**：**基本不受阻**，主流依赖（FastAPI, Redis, Asyncmy）测试均已通过安装与基础解析。
   * **阶段 B / I（L2 意图向量检索、RAG 知识库、复杂文档解析）**：**存在较高踩坑风险**。重型向量库、本地嵌入模型（Torch/Sentence-Transformers）以及多格式文档解析器（Unstructured/老版 PDF 工具）对 3.14 的适配尚不完善，容易出现缺少 Pre-built Wheel 或 C-API 兼容报错。
3. **最终建议**：
   * **不要强制绑定 `>=3.14`**。建议将 `pyproject.toml` 调整为 `requires-python = ">=3.11"`。
   * 本地开发推荐使用 **Python 3.11 或 3.12**。避免在学习和迁移过程中，把大量时间浪费在“排查是业务 Bug 还是 3.14 依赖边缘 Bug”的心智消耗上。

---

## 二、 Python 3.14 关键变更与潜在破坏点

### 1. PEP 649：类型注解惰性求值（Deferred Evaluation of Annotations）
* **原理变化**：在 Python 3.14 之前，类型注解在类或函数定义时立即求值；3.14 开始改为在首次访问（通过 `annotationlib`）时惰性求值。
* **对项目的影响**：
  * **Pydantic**：Pydantic v1 彻底无法在 Python 3.14 上运行；必须全面采用 Pydantic v2。
  * **Forward References & Type Hints**：虽然免去了 `from __future__ import annotations`，但如果在代码中存在 `if TYPE_CHECKING:` 下导入的模块且被 Pydantic/FastAPI 运行时解析，会直接抛出 `NameError`。

### 2. C 扩展与 Pre-built Wheels 覆盖率
* Python 3.14 发布时间较近。绝大部分纯 Python 库（Pure Python）无缝兼容，但带有 C/C++/Rust 扩展的库（如部分 MySQL 驱动、分词库、本地编译模块）可能在 macOS 或 Linux 容器中缺少针对 `cp314` 的 Wheel 包，导致安装时被迫走本地源码编译，容易因缺少本地 C 编译链而中断。

---

## 三、 逐阶段（阶段 A ~ 阶段 I）依赖兼容性实测分析

| 阶段 | 核心任务 | 关键第三方依赖 | 3.14 实测与解析结果 | 稳定性评估与风险 |
| :--- | :--- | :--- | :--- | :--- |
| **阶段 A** | 启动、架构、认证与会话 | `agentscope==2.0.8`, `fastapi`, `uvicorn`, `pydantic>=2`, `pyjwt`, `asyncmy`/`pymysql`, `redis` | **通过**。已在本地 3.14 环境实测安装成功，`FunctionTool` 与 AgentScope 基础类运行正常。 | **稳定**。主流 Web/ORM/Redis 库对 3.14 支持较好。建议 MySQL 优先使用纯 Python/异步驱动（`asyncmy`/`pymysql`）而非 C-based 的 `mysqlclient`。 |
| **阶段 B** | 多智能体与意图流水线 | 正则引擎、轻量模型调用、L2 意图向量检索 | **通过**。`uv pip` 可解析 `chromadb`、`tokenizers` 等包。 | **中等风险**。若 L2 RAG 采用云端向量 API（如 DashScope/OpenAI Embeddings），则很稳定；若本地加载小型嵌入模型（如 `sentence-transformers` + `torch`），可能遇到 Apple Silicon / CUDA 加速兼容问题。 |
| **阶段 C** | Master 智能体与身份边界 | AgentScope 2.x Agent 体系、统一上下文 | **通过**。纯 Python 上下文与提示词编排。 | **稳定**。无重型系统级依赖。 |
| **阶段 D** | 差旅管理与业务工具 | 状态机、Pydantic 领域模型、事务管理 | **通过**。标准业务代码与 ORM。 | **稳定**。不涉及底层系统行为。 |
| **阶段 E** | 行程规划与外部能力 | `httpx`, `aiohttp`, Plan-and-Execute 状态 | **通过**。异步 HTTP 库测试正常。 | **稳定**。异步 I/O 在 3.14 下表现稳定。 |
| **阶段 F** | 预订智能体与幂等可靠性 | Redis 分布式锁、重试补偿、事务一致性 | **通过**。`redis==8.1.0` 兼容。 | **稳定**。 |
| **阶段 G** | 提示词工程与上下文控制 | `tiktoken`, 结构化输出校验 | **通过**。`tiktoken==0.14.0` 具备 3.14 wheel。 | **稳定**。 |
| **阶段 H** | 执行闭环与多实例恢复 | `opentelemetry`, 分布式状态持久化 | **通过**。OTel 系列包具备完整 wheel。 | **稳定**。 |
| **阶段 I** | 知识库、协议（MCP）与生产部署 | `mcp`, `pypdf`, `python-docx`, 向量库 | **部分高风险**。`mcp` 协议支持正常，但老牌或重型文档提取库（如 `unstructured`, OCR 相关库）对 3.14 适配滞后。 | **高风险**。文档清洗与私有知识库入库容易遭遇非预期的反射与 C 扩展报错。 |

---

## 四、 结论与实操建议

1. **当前项目能否在 3.14 下继续跑？**
   * **能跑**。核心框架 `AgentScope 2.0.8` 和 `FastAPI` 我们已经在本地实际装上并验证了异步工具调用的跑通。
2. **为什么不建议将其作为唯一限定（`>=3.14`）？**
   * **排错成本高**：社区中针对 Python 3.14 的 AgentScope 和 Agent 落地问答几乎为零。一旦遇到深层次反射或异步锁异常，难以区分是自己代码逻辑问题还是运行时 Edge Case。
   * **部署兼容性差**：生产部署环境（如轻量 Linux 基础镜像、主流云托管容器）通常默认预装或优选支持 3.11 或 3.12，直接使用 3.14 镜像体积较大且缺乏广泛的生产验证。
3. **改造建议**：
   * 将 `pyproject.toml` 中的版本声明放宽为：
     ```toml
     requires-python = ">=3.11"
     ```
   * 优先使用系统或 Conda 现有的 `Python 3.11`（例如你本机的 `/opt/anaconda3/envs/py311`）或者 3.12 作为日常开发的主解释器，将 3.14 仅作为前瞻兼容测试。
