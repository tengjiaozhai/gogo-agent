# CODEMAP.md

本文件只说明文件职责，帮助定位下一份应该读取的内容。实际文件内容是当前行为的最终事实。

## 根目录

- [`AGENTS.md`](AGENTS.md) — 规定仓库工作规则、权威来源及任务路由映射。
- [`.gitignore`](.gitignore) — 配置 Git 忽略规则，排除虚拟环境、IDE 配置及缓存临时文件。
- [`.python-version`](.python-version) — 指定已验证的 Python 3.12.13 运行时。
- [`pyproject.toml`](pyproject.toml) — 定义 Python 项目元数据、运行时版本要求及依赖配置。
- [`uv.lock`](uv.lock) — 固定 001 已解析的 Python 依赖版本以供重复安装。
- [`README.md`](README.md) — 说明安装、终端 Agent、FastAPI 健康检查和测试命令。
- [`.env.example`](.env.example) — 列出 001 模型网关所需的环境变量名。
- [`main.py`](main.py) — 根目录便捷入口，供 PyCharm 右键一键运行或调试 API 后台。

## src/gogo_agent

- [`src/gogo_agent/__init__.py`](src/gogo_agent/__init__.py) — 标记 GoGo Agent 的 Python 包。
- [`src/gogo_agent/cli.py`](src/gogo_agent/cli.py) — 装配仅含日期工具的 Agent 并提供终端入口。
- [`src/gogo_agent/tools.py`](src/gogo_agent/tools.py) — 提供不依赖模型的本地日期工具。
- [`src/gogo_agent/api.py`](src/gogo_agent/api.py) — 提供后端唯一 FastAPI 应用入口、挂载认证与会话路由、Scalar 交互式 API 文档及健康检查。

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
- [`src/gogo_agent/chat/models.py`](src/gogo_agent/chat/models.py) — 定义会话、消息领域模型及请求响应视图 DTO。
- [`src/gogo_agent/chat/repository.py`](src/gogo_agent/chat/repository.py) — 实现 L1 业务历史（内存与 SQL）及 L2 AgentState 状态（agentscope_session）仓储。
- [`src/gogo_agent/chat/service.py`](src/gogo_agent/chat/service.py) — 编排会话惰性创建、消息归属鉴权校验、标题自动提取及点赞反馈。
- [`src/gogo_agent/chat/executor.py`](src/gogo_agent/chat/executor.py) — 驱动 AgentScope 智能体推理、跨轮次恢复/保存 AgentState 并支持 SSE 流式与 JSON 输出。
- [`src/gogo_agent/chat/dependencies.py`](src/gogo_agent/chat/dependencies.py) — 提供会话仓储、L2 记忆库与执行器的 FastAPI 依赖注入。
- [`src/gogo_agent/chat/router.py`](src/gogo_agent/chat/router.py) — 定义 /api/chat 下的会话增删查、历史消息获取、标题更新及反馈端点。

## src/gogo_agent/db

- [`src/gogo_agent/db/__init__.py`](src/gogo_agent/db/__init__.py) — 导出数据库 Base、模型类、连接引擎与会话工厂。
- [`src/gogo_agent/db/base.py`](src/gogo_agent/db/base.py) — 定义统一的 SQLAlchemy DeclarativeBase 声明式基类。
- [`src/gogo_agent/db/models.py`](src/gogo_agent/db/models.py) — 定义用户账号、会话、消息及 agentscope_session 内部状态的 SQLAlchemy ORM 数据模型。
- [`src/gogo_agent/db/session.py`](src/gogo_agent/db/session.py) — 管理数据库连接池、会话生成及 FastAPI 依赖注入。
- [`src/gogo_agent/db/init_db.py`](src/gogo_agent/db/init_db.py) — 幂等初始化数据库表结构与填充初始脱敏种子用户。

## docs

- [`docs/AgentScope-Python-迁移路线图.md`](docs/AgentScope-Python-迁移路线图.md) — 规定从 Java 版 GoGo Agent 迁移到 AgentScope Python 2.x 的长期阶段规划与迁移边界。

## docs/契约样例

- [`docs/契约样例/002-用户旅程静态契约.md`](docs/契约样例/002-用户旅程静态契约.md) — 记录 Java 版登录、对话、差旅、规划、审核、审批与预订的脱敏静态契约及风险边界。

## docs/架构

- [`docs/架构/003-整体架构与HTTP入口.md`](docs/架构/003-整体架构与HTTP入口.md) — 记录 Java 请求链、Python 目标调用图、HTTP 入口取舍及 Java→Python 接口映射。

## docs/学习路线

- [`docs/学习路线/README.md`](docs/学习路线/README.md) — 汇总从零用 Python 和 AgentScope 重构 GoGo Agent 的学习阶段索引与版本规则。
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

- [`docs/research/python-3.14-compatibility-assessment.md`](docs/research/python-3.14-compatibility-assessment.md) — 评估 Python 3.14 稳定性及对阶段 A 至阶段 I 实施的潜在兼容性影响。

## exercises

- [`exercises/__init__.py`](exercises/__init__.py) — 标记 000 练习代码目录为 Python 包。
- [`exercises/ex000_a_types.py`](exercises/ex000_a_types.py) — 000-A 练习：Pydantic 领域模型定义、日期/字段格式校验及异常捕获。
- [`exercises/ex000_b_async.py`](exercises/ex000_b_async.py) — 000-B 练习：异步 I/O 串行与并发耗时对比及 contextvars 协程上下文隔离。
- [`exercises/ex000_c_api.py`](exercises/ex000_c_api.py) — 000-C 练习：最小 FastAPI 服务端点实现及 httpx 客户端请求演示。

## tests

- [`tests/__init__.py`](tests/__init__.py) — 标记测试目录为 Python 包。
- [`tests/test_exercises.py`](tests/test_exercises.py) — 验证 000 系列小练习的输入校验、并发性能与 HTTP 端点行为。
- [`tests/test_001.py`](tests/test_001.py) — 验证 001 配置、日期工具、HTTP 健康检查及模拟模型网关调用链。
- [`tests/test_004_auth.py`](tests/test_004_auth.py) — 验证 004 登录、登出、用户信息隔离、401 拦截与密码哈希自动迁移。
- [`tests/test_005_chat.py`](tests/test_005_chat.py) — 验证 005 会话与消息持久化、AgentState 记忆跨重启恢复、用户隔离 403 拦截与 SSE 输出。
- [`tests/test_mariadb_integration.py`](tests/test_mariadb_integration.py) — 验证真实 MariaDB 数据库连接、用户密码迁移、会话消息与 agentscope_session 存取。
- [`tests/test_redis_integration.py`](tests/test_redis_integration.py) — 验证真实 172.22.22.123 Redis 连接、30 天 TTL、跨实例 Token 持久化与平滑降级。

