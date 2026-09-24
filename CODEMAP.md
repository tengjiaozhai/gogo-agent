# CODEMAP.md

本文件只说明文件职责，帮助定位下一份应该读取的内容。实际文件内容是当前行为的最终事实。

## 根目录

- [`AGENTS.md`](AGENTS.md) — 规定仓库工作规则、权威来源及任务路由映射。
- [`.gitignore`](.gitignore) — 配置 Git 忽略规则，排除虚拟环境、IDE 配置及缓存临时文件。
- [`.python-version`](.python-version) — 指定已验证的 Python 3.12.13 运行时。
- [`pyproject.toml`](pyproject.toml) — 定义 Python 项目元数据、运行时版本要求及依赖配置。
- [`uv.lock`](uv.lock) — 固定 001 已解析的 Python 依赖版本以供重复安装。
- [`README.md`](README.md) — 说明 001 的安装、终端 Agent、健康检查和测试命令。
- [`.env.example`](.env.example) — 列出 001 模型网关所需的环境变量名。

## src/gogo_agent

- [`src/gogo_agent/__init__.py`](src/gogo_agent/__init__.py) — 标记 GoGo Agent 的 Python 包。
- [`src/gogo_agent/cli.py`](src/gogo_agent/cli.py) — 装配仅含日期工具的 Agent 并提供终端入口。
- [`src/gogo_agent/tools.py`](src/gogo_agent/tools.py) — 提供不依赖模型的本地日期工具。
- [`src/gogo_agent/health.py`](src/gogo_agent/health.py) — 提供不依赖模型凭证的 HTTP 健康检查。

## docs

- [`docs/AgentScope-Python-迁移路线图.md`](docs/AgentScope-Python-迁移路线图.md) — 规定从 Java 版 GoGo Agent 迁移到 AgentScope Python 2.x 的长期阶段规划与迁移边界。

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
- [`tests/test_001.py`](tests/test_001.py) — 验证 001 配置、日期工具、健康检查及模拟模型网关调用链。
