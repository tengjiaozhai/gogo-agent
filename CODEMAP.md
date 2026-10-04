# CODEMAP.md

本文件只说明文件职责，帮助定位下一份应该读取的内容。实际文件内容是当前行为的最终事实。

## 根目录

- [`AGENTS.md`](AGENTS.md) — 规定仓库工作规则、权威来源及任务路由映射。
- [`.gitignore`](.gitignore) — 配置 Git 忽略规则，排除虚拟环境、IDE 配置及缓存临时文件。
- [`.python-version`](.python-version) — 指定已验证的 Python 3.12.13 运行时。
- [`pyproject.toml`](pyproject.toml) — 定义 Python 项目元数据、运行时版本要求及依赖配置。
- [`uv.lock`](uv.lock) — 固定 001 已解析的 Python 依赖版本以供重复安装。
- [`README.md`](README.md) — 说明安装、终端 Agent、FastAPI 健康检查和测试命令。
- [`.env.example`](.env.example) — 列出模型网关、Agent 轮次/超时、意图向量索引及存储连接的环境变量。
- [`main.py`](main.py) — 根目录便捷入口，供 PyCharm 右键一键运行或调试 API 后台。

## src/gogo_agent

- [`src/gogo_agent/__init__.py`](src/gogo_agent/__init__.py) — 标记 GoGo Agent 的 Python 包。
- [`src/gogo_agent/cli.py`](src/gogo_agent/cli.py) — 装配仅含日期工具的 Agent 并提供终端入口。
- [`src/gogo_agent/tools.py`](src/gogo_agent/tools.py) — 提供不依赖模型的本地日期工具。
- [`src/gogo_agent/api.py`](src/gogo_agent/api.py) — 提供唯一 FastAPI 入口、启动配置校验、认证与会话路由及 API 文档和健康检查。
- [`src/gogo_agent/request_context.py`](src/gogo_agent/request_context.py) — 定义服务端可信请求参数及只承载日志追踪号的异步任务作用域。

## src/gogo_agent/auth

- [`src/gogo_agent/auth/__init__.py`](src/gogo_agent/auth/__init__.py) — 导出登录认证数据模型、依赖注入函数与核心服务。
- [`src/gogo_agent/auth/models.py`](src/gogo_agent/auth/models.py) — 定义用户账户模型以及登录、登出、用户信息的请求响应模式。
- [`src/gogo_agent/auth/security.py`](src/gogo_agent/auth/security.py) — 实现 PBKDF2 密码哈希安全校验、旧明文自动升级迁移与基于 Redis / 内存的 30 天跨会话 Token 管理。
- [`src/gogo_agent/auth/repository.py`](src/gogo_agent/auth/repository.py) — 提供用户账户查询、密码哈希更新及预置测试账号数据。
- [`src/gogo_agent/auth/service.py`](src/gogo_agent/auth/service.py) — 编排用户登录验证、会话注销及密码透明哈希迁移。
- [`src/gogo_agent/auth/dependencies.py`](src/gogo_agent/auth/dependencies.py) — 解析 Authorization 请求头并校验服务端 Token 提取可信用户上下文。
- [`src/gogo_agent/auth/router.py`](src/gogo_agent/auth/router.py) — 定义 /api/auth 下的登录、退出与当前用户信息 HTTP 端点。

## src/gogo_agent/chat

- [`src/gogo_agent/chat/__init__.py`](src/gogo_agent/chat/__init__.py) — 导出会话消息服务、执行器与路由入口。
- [`src/gogo_agent/chat/config.py`](src/gogo_agent/chat/config.py) — 集中主/信息 Agent 提示词、轮次、模型与只读工具超时配置及启动校验。
- [`src/gogo_agent/chat/continuation.py`](src/gogo_agent/chat/continuation.py) — 定义接收可信请求上下文的活跃 Agent 续跑接口，并判定完整信号或无效记录的入口。
- [`src/gogo_agent/chat/models.py`](src/gogo_agent/chat/models.py) — 定义会话、消息领域模型及请求响应视图 DTO。
- [`src/gogo_agent/chat/repository.py`](src/gogo_agent/chat/repository.py) — 实现内存与 SQL 的业务历史限量查询、AgentState 保存读取及删除。
- [`src/gogo_agent/chat/service.py`](src/gogo_agent/chat/service.py) — 编排会话创建、鉴权后的全量或最近历史读取、标题提取及反馈。
- [`src/gogo_agent/chat/executor.py`](src/gogo_agent/chat/executor.py) — 按可信请求和集中配置运行主/信息 Agent，处理 JSON/SSE 成功与失败出口并保存状态和工具记录。
- [`src/gogo_agent/chat/dependencies.py`](src/gogo_agent/chat/dependencies.py) — 提供会话仓储、L2 记忆库与执行器的 FastAPI 依赖注入。
- [`src/gogo_agent/chat/router.py`](src/gogo_agent/chat/router.py) — 定义 /api/chat 的会话接口，并在 SSE 建立前完成对话预处理。

## src/gogo_agent/db

- [`src/gogo_agent/db/__init__.py`](src/gogo_agent/db/__init__.py) — 导出数据库 Base、模型类、连接引擎与会话工厂。
- [`src/gogo_agent/db/base.py`](src/gogo_agent/db/base.py) — 定义统一的 SQLAlchemy DeclarativeBase 声明式基类。
- [`src/gogo_agent/db/models.py`](src/gogo_agent/db/models.py) — 定义用户账号、会话、消息及 agentscope_session 内部状态的 SQLAlchemy ORM 数据模型。
- [`src/gogo_agent/db/session.py`](src/gogo_agent/db/session.py) — 管理数据库连接池、会话生成及 FastAPI 依赖注入。
- [`src/gogo_agent/db/init_db.py`](src/gogo_agent/db/init_db.py) — 幂等初始化数据库表结构与填充初始脱敏种子用户。

## src/gogo_agent/intent

- [`src/gogo_agent/intent/__init__.py`](src/gogo_agent/intent/__init__.py) — 导出改写、识别、流水线和顺序执行的公开类型及入口。
- [`src/gogo_agent/intent/models.py`](src/gogo_agent/intent/models.py) — 定义改写、意图及三层识别结果的 Pydantic 契约和跨字段校验。
- [`src/gogo_agent/intent/context.py`](src/gogo_agent/intent/context.py) — 构造经会话归属校验和长度裁剪的最近历史与当前问题快照。
- [`src/gogo_agent/intent/execution.py`](src/gogo_agent/intent/execution.py) — 显式传递可信上下文，按事项顺序委派可注入子 Agent，聚合结果与追踪并模拟写去重。
- [`src/gogo_agent/intent/pipeline.py`](src/gogo_agent/intent/pipeline.py) — 用可信请求上下文读取本轮历史，统一快筛、短追问守卫、条件改写和完整识别。
- [`src/gogo_agent/intent/rules.py`](src/gogo_agent/intent/rules.py) — 执行 L0/L1 优先级规则、复合弃权及明确否定动作的排除。
- [`src/gogo_agent/intent/runtime.py`](src/gogo_agent/intent/runtime.py) — 从共用环境配置装配聊天模型、embedding、Qdrant 与一次请求的识别流水线。
- [`src/gogo_agent/intent/seed.json`](src/gogo_agent/intent/seed.json) — 保存从 Java YAML 移植的 16 类 68 条运行时意图种子。
- [`src/gogo_agent/intent/service.py`](src/gogo_agent/intent/service.py) — 执行单次模型改写与 L3 分类，验证动作保真并编排规则、向量和 LLM 短路。
- [`src/gogo_agent/intent/vector.py`](src/gogo_agent/intent/vector.py) — 校验意图种子、构建版本化 Qdrant 索引并执行 L2 Top-2 阈值匹配。

## docs

- [`docs/AgentScope-Python-迁移路线图.md`](docs/AgentScope-Python-迁移路线图.md) — 规定从 Java 版 GoGo Agent 迁移到 AgentScope Python 2.x 的长期阶段规划与迁移边界。

## docs/契约样例

- [`docs/契约样例/002-用户旅程静态契约.md`](docs/契约样例/002-用户旅程静态契约.md) — 记录 Java 版登录、对话、差旅、规划、审核、审批与预订的脱敏静态契约及风险边界。
- [`docs/契约样例/008-问题改写与意图契约.md`](docs/契约样例/008-问题改写与意图契约.md) — 说明改写和意图数据字段、上下文补全样例及 Java 到 Python 的契约差异。
- [`docs/契约样例/008-两轮对话.json`](docs/契约样例/008-两轮对话.json) — 提供固定日期下的两轮对话、缺上下文、多意图和未知意图验收样例。
- [`docs/契约样例/010-011-单次调用与上下文.md`](docs/契约样例/010-011-单次调用与上下文.md) — 记录单次模型参数、近期历史构造、三轮改写样例及模拟验收边界。
- [`docs/契约样例/012-三层识别与短路.md`](docs/契约样例/012-三层识别与短路.md) — 说明 L1/L2/L3 注入接口、候选与阈值字段、歧义短路和假匹配器验收。
- [`docs/契约样例/013-规则识别.md`](docs/契约样例/013-规则识别.md) — 记录 L0/L1 Java 规则顺序、排除词与读写歧义样例。
- [`docs/契约样例/014-意图向量索引.md`](docs/契约样例/014-意图向量索引.md) — 记录 Qdrant 构建方法、Java 阈值基线与真实 Top-2 结果。
- [`docs/契约样例/015-多意图快速层弃权.md`](docs/契约样例/015-多意图快速层弃权.md) — 记录 L1/L2 多意图弃权、Java 基线盲区及真实模型对照。
- [`docs/契约样例/016-017-执行与流水线.md`](docs/契约样例/016-017-执行与流水线.md) — 说明 HTTP 识别流水线、假 Master 顺序执行、断点 demo 与运行边界。
- [`docs/契约样例/018-意图回归语料.json`](docs/契约样例/018-意图回归语料.json) — 保存误路由与正确样例的固定历史、期望改写和命中层。
- [`docs/契约样例/018-意图误路由修复.md`](docs/契约样例/018-意图误路由修复.md) — 记录否定动作、短追问和三亚出差等修复前后差异与验收。
- [`docs/契约样例/019-主协调入口与直跳取舍.md`](docs/契约样例/019-主协调入口与直跳取舍.md) — 记录不启用高置信直跳的依据、当前协调入口验收及后续候选条件。
- [`docs/契约样例/020-执行顺序与续跑边界.md`](docs/契约样例/020-执行顺序与续跑边界.md) — 对照新请求与活跃 Agent 续跑的序列、标题/历史时点及当前验收边界。
- [`docs/契约样例/021-请求上下文与信任边界.md`](docs/契约样例/021-请求上下文与信任边界.md) — 区分认证上下文与模型可见业务线索，记录传参、伪造身份及追踪验收。
- [`docs/契约样例/024-Agent关键配置与失败出口.md`](docs/契约样例/024-Agent关键配置与失败出口.md) — 对照主/信息 Agent 的配置来源、Java 差异、失败出口与运行验收。

## docs/架构

- [`docs/架构/003-整体架构与HTTP入口.md`](docs/架构/003-整体架构与HTTP入口.md) — 记录 Java 请求链、Python 目标调用图、HTTP 入口取舍及 Java→Python 接口映射。
- [`docs/架构/006-多智能体职责边界.md`](docs/架构/006-多智能体职责边界.md) — 记录 006 的多智能体职责取舍、Java 工具注册现状与 Python 目标写入边界。
- [`docs/架构/007-Agent职责清单.md`](docs/架构/007-Agent职责清单.md) — 记录实际 Agent 的输入、输出、工具、状态和失败处理，以及 Java 装配与 Python 迁移范围的对应关系。
- [`docs/架构/009-改写与识别调用边界.md`](docs/架构/009-改写与识别调用边界.md) — 记录独立改写/识别的取舍及当前 HTTP 接线状态。

## docs/学习路线

- [`docs/学习路线/README.md`](docs/学习路线/README.md) — 汇总从零用 Python 和 AgentScope 重构 GoGo Agent 的学习阶段索引与版本规则。
- [`docs/学习路线/010-011-真实模型实操.md`](docs/学习路线/010-011-真实模型实操.md) — 指导用真实模型观察三轮改写与意图识别，区分固定响应和语义验收。
- [`docs/学习路线/阶段A-001-005.md`](docs/学习路线/阶段A-001-005.md) — 指导阶段 A（启动、演示、架构、登录鉴权及会话持久化）的学习与实施任务。
- [`docs/学习路线/阶段B-006-022.md`](docs/学习路线/阶段B-006-022.md) — 指导阶段 B（多智能体分工与多层意图流水线）的学习与实施任务。
- [`docs/学习路线/阶段C-023-028.md`](docs/学习路线/阶段C-023-028.md) — 指导阶段 C（Master 智能体、模型统一配置与身份传递）的学习与实施任务。
- [`docs/学习路线/阶段D-029-041.md`](docs/学习路线/阶段D-029-041.md) — 指导阶段 D（差旅管理智能体与确定性业务工具）的学习与实施任务。
- [`docs/学习路线/阶段E-042-060.md`](docs/学习路线/阶段E-042-060.md) — 指导阶段 E（行程规划 Plan-and-Execute、审核与受控外部能力）的学习与实施任务。
- [`docs/学习路线/阶段F-061-067.md`](docs/学习路线/阶段F-061-067.md) — 指导阶段 F（预订智能体、幂等性与工具可靠性）的学习与实施任务。
- [`docs/学习路线/阶段G-068-085.md`](docs/学习路线/阶段G-068-085.md) — 指导阶段 G（提示词工程、记忆分层与上下文成本控制）的学习与实施任务。
- [`docs/学习路线/阶段H-086-094.md`](docs/学习路线/阶段H-086-094.md) — 指导阶段 H（执行闭环、安全隔离与多实例中断恢复）的学习与实施任务。
- [`docs/学习路线/阶段I-095-102.md`](docs/学习路线/阶段I-095-102.md) — 指导阶段 I（知识库/协议整合、前端契约联调与生产部署）的学习与实施任务。

## docs/research

- [`docs/research/agentscope-2.0.8-context-evaluation.md`](docs/research/agentscope-2.0.8-context-evaluation.md) — 对照 AgentScope Python 2.0.8 与项目的身份传递、改写历史和 Agent 状态管理边界。
- [`docs/research/python-3.14-compatibility-assessment.md`](docs/research/python-3.14-compatibility-assessment.md) — 评估 Python 3.14 稳定性及对阶段 A 至阶段 I 实施的潜在兼容性影响。

## exercises

- [`exercises/__init__.py`](exercises/__init__.py) — 标记 000 练习代码目录为 Python 包。
- [`exercises/ex000_a_types.py`](exercises/ex000_a_types.py) — 000-A 练习：Pydantic 领域模型定义、日期/字段格式校验及异常捕获。
- [`exercises/ex000_b_async.py`](exercises/ex000_b_async.py) — 000-B 练习：异步 I/O 串行与并发耗时对比及 contextvars 协程上下文隔离。
- [`exercises/ex000_c_api.py`](exercises/ex000_c_api.py) — 000-C 练习：最小 FastAPI 服务端点实现及 httpx 客户端请求演示。

## scripts

- [`scripts/demo_010_011_real_model.py`](scripts/demo_010_011_real_model.py) — 使用项目网关和内存历史演示三轮真实改写、意图识别与语义检查。
- [`scripts/build_intent_index.py`](scripts/build_intent_index.py) — 从统一运行配置构建或复用版本化意图索引并输出固定句探针。
- [`scripts/demo_016_017_pipeline.py`](scripts/demo_016_017_pipeline.py) — 用真实规则、Qdrant、模型和内存假子 Agent 演示四层分支与顺序执行。
- [`scripts/evaluate_018_intents.py`](scripts/evaluate_018_intents.py) — 用真实模型和 Qdrant 逐条评估 018 固定语料并打印预期差异。
- [`scripts/demo_020_execution_order.py`](scripts/demo_020_execution_order.py) — 用离线模型和内存状态演示新请求顺序、同会话恢复及活跃 Agent 续跑回退。
- [`scripts/demo_021_request_context.py`](scripts/demo_021_request_context.py) — 用本地 Token、假子 Agent 和假工具演示可信身份传递及多步骤追踪。
- [`scripts/demo_022_async_context.py`](scripts/demo_022_async_context.py) — 用离线双请求与后台任务演示显式身份传递、追踪隔离和空任务上下文。
- [`scripts/demo_023_master_agent.py`](scripts/demo_023_master_agent.py) — 用离线 HTTP、固定模型和真实 AgentScope Toolkit 演示主 Agent 委派与未注册工具拒绝。
- [`scripts/demo_024_agent_config.py`](scripts/demo_024_agent_config.py) — 用离线模型演示启动配置、信息工具超时及主 Agent 轮次耗尽。

## output

- [`output/l0_l1_intent_routing.html`](output/l0_l1_intent_routing.html) — 提供 L0/L1 意图路由和规则否决条件的独立交互图解。
- [`output/show_me_023_execution_flow.html`](output/show_me_023_execution_flow.html) — 提供 023 从 HTTP 请求到 MasterModel._call_api 调用链路与 Java 工程化映射图解。

## tests

- [`tests/__init__.py`](tests/__init__.py) — 标记测试目录为 Python 包。
- [`tests/test_exercises.py`](tests/test_exercises.py) — 验证 000 系列小练习的输入校验、并发性能与 HTTP 端点行为。
- [`tests/test_001.py`](tests/test_001.py) — 验证 001 配置、日期工具、HTTP 健康检查及模拟模型网关调用链。
- [`tests/test_004_auth.py`](tests/test_004_auth.py) — 验证 004 登录、登出、用户信息隔离、401 拦截与密码哈希自动迁移。
- [`tests/test_005_chat.py`](tests/test_005_chat.py) — 隔离意图模型后验证会话持久化、AgentState 恢复、用户隔离和 SSE 输出。
- [`tests/test_008_intent.py`](tests/test_008_intent.py) — 验证 008 数据契约的有效样例、非法输入、跨字段一致性和中文 schema 说明。
- [`tests/test_010_011_intent.py`](tests/test_010_011_intent.py) — 用固定响应和内存 HTTP 验证单次调用、错误出口与历史窗口、身份隔离及裁剪。
- [`tests/test_012_routing.py`](tests/test_012_routing.py) — 用假规则与向量匹配器验证三层短路、候选阈值、歧义及 L3 回退调用次数。
- [`tests/test_013_rules.py`](tests/test_013_rules.py) — 验证 Java L0/L1 类别、规则优先级、排除词、复合弃权与无效输入。
- [`tests/test_014_vector.py`](tests/test_014_vector.py) — 验证意图种子、真实 Qdrant SDK 幂等索引与 Java Top-2 阈值分差基线。
- [`tests/test_015_multi_intent.py`](tests/test_015_multi_intent.py) — 验证复合句跳过 L2、L3 有序双意图输出和纯单意图快速路径。
- [`tests/test_016_017_pipeline.py`](tests/test_016_017_pipeline.py) — 验证条件改写、假子 Agent 顺序和去重、HTTP JSON/SSE 与失败出口。
- [`tests/test_018_intent_regression.py`](tests/test_018_intent_regression.py) — 验证否定规则、上下文短追问守卫和改写动作保真。
- [`tests/test_019_routing.py`](tests/test_019_routing.py) — 验证 L1/L2/L3 结果均进入 GoGo 协调入口，不因高置信绕过会话归属或提示词。
- [`tests/test_020_execution_order.py`](tests/test_020_execution_order.py) — 记录新请求与 GoGo 状态恢复顺序，并用假活跃 Agent 验证实际执行器续跑和回退。
- [`tests/test_021_request_context.py`](tests/test_021_request_context.py) — 用真实登录 Token 和假工具验收伪造用户 ID 隔离及子步骤追踪。
- [`tests/test_022_async_context.py`](tests/test_022_async_context.py) — 用双用户交错 HTTP、跨任务 SSE 和后台任务验证身份与追踪上下文隔离及清理。
- [`tests/test_023_master_agent.py`](tests/test_023_master_agent.py) — 验证 HTTP JSON/SSE 中主 Agent 调用只读信息子 Agent 及未注册工具拒绝。
- [`tests/test_024_agent_config.py`](tests/test_024_agent_config.py) — 验证启动配置、Agent 轮次/超时、模型重试和错误取消出口。
- [`tests/test_mariadb_integration.py`](tests/test_mariadb_integration.py) — 验证真实 MariaDB 数据库连接、用户密码迁移、会话消息与 agentscope_session 存取。
- [`tests/test_redis_integration.py`](tests/test_redis_integration.py) — 验证真实 172.22.22.123 Redis 连接、30 天 TTL、跨实例 Token 持久化与平滑降级。
