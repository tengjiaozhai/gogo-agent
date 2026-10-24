# CODEMAP.md

本文件只说明文件职责，帮助定位下一份应该读取的内容。实际文件内容是当前行为的最终事实。

## 根目录

- [`AGENTS.md`](AGENTS.md) — 规定仓库工作规则、权威来源及任务路由映射。
- [`.gitignore`](.gitignore) — 配置 Git 忽略规则，排除虚拟环境、IDE 配置及缓存临时文件。
- [`.python-version`](.python-version) — 指定已验证的 Python 3.12.13 运行时。
- [`pyproject.toml`](pyproject.toml) — 定义 Python 项目元数据、运行时版本要求及依赖配置。
- [`uv.lock`](uv.lock) — 固定 001 已解析的 Python 依赖版本以供重复安装。
- [`README.md`](README.md) — 说明安装、终端 Agent、FastAPI 健康检查、前端入口和测试命令。
- [`.env.example`](.env.example) — 列出模型网关、Agent 轮次/超时、意图向量索引及存储连接的环境变量。
- [`main.py`](main.py) — 根目录便捷入口，供 PyCharm 右键一键运行或调试 API 后台。

## frontend

- [`frontend/.gitignore`](frontend/.gitignore) — 排除前端依赖、构建结果和本地配置。
- [`frontend/README.md`](frontend/README.md) — 说明提前迁入的前端范围、启动和检查命令。
- [`frontend/eslint.config.js`](frontend/eslint.config.js) — 配置 React 与 TypeScript 的静态检查规则。
- [`frontend/index.html`](frontend/index.html) — 提供 React 页面根节点、标题和图标入口。
- [`frontend/package.json`](frontend/package.json) — 定义前端依赖与开发、消息状态测试、构建和 Lint 脚本。
- [`frontend/package-lock.json`](frontend/package-lock.json) — 锁定前端 npm 依赖版本。
- [`frontend/pnpm-lock.yaml`](frontend/pnpm-lock.yaml) — 锁定现有 pnpm 前端依赖解析结果。
- [`frontend/tsconfig.app.json`](frontend/tsconfig.app.json) — 配置浏览器端 TypeScript 编译检查。
- [`frontend/tsconfig.json`](frontend/tsconfig.json) — 组织前端 TypeScript 项目引用。
- [`frontend/tsconfig.node.json`](frontend/tsconfig.node.json) — 配置 Vite 运行配置的 TypeScript 检查。
- [`frontend/vite.config.ts`](frontend/vite.config.ts) — 启用 React 并将开发期 `/api` 代理到 Python 服务。
- [`frontend/tests/reply-state.test.ts`](frontend/tests/reply-state.test.ts) — 验证消息保存确认、失败摘要、迟到事件与跨会话状态隔离。

## frontend/public

- [`frontend/public/favicon.svg`](frontend/public/favicon.svg) — 提供浏览器标签图标。

## frontend/src

- [`frontend/src/App.css`](frontend/src/App.css) — 定义沿用源项目的登录、侧栏、聊天和消息样式。
- [`frontend/src/App.tsx`](frontend/src/App.tsx) — 验证已有 token、处理临时验证失败并在登录页与聊天布局间切换。
- [`frontend/src/index.css`](frontend/src/index.css) — 设置页面基础重置样式。
- [`frontend/src/main.tsx`](frontend/src/main.tsx) — 挂载 React 应用。

## frontend/src/api

- [`frontend/src/api/auth.ts`](frontend/src/api/auth.ts) — 调用 Python 登录、登出和当前用户接口。
- [`frontend/src/api/admin.ts`](frontend/src/api/admin.ts) — 调用 Python 管理员审批接口并将 snake_case 审批响应转换为页面契约。
- [`frontend/src/api/chat.ts`](frontend/src/api/chat.ts) — 调用会话与反馈接口，解析聊天 SSE 并携带历史执行时间轴。
- [`frontend/src/api/config.ts`](frontend/src/api/config.ts) — 选择同源或显式配置的 API 地址。
- [`frontend/src/api/request.ts`](frontend/src/api/request.ts) — 提供阶段 D 页面共用的带认证 JSON 请求和 HTTP 错误类型。
- [`frontend/src/api/time.ts`](frontend/src/api/time.ts) — 将 Python 无时区 UTC ISO 时间转换为浏览器时间戳。
- [`frontend/src/api/travel.ts`](frontend/src/api/travel.ts) — 调用本人差旅、冲突、订单写入和内部预订接口。

## frontend/src/components

- [`frontend/src/components/AgentMessageBlock.tsx`](frontend/src/components/AgentMessageBlock.tsx) — 渲染思考、主子委派、可展开工具详情、执行状态及 Markdown 回复与反馈。
- [`frontend/src/components/AdminApprovalPage.tsx`](frontend/src/components/AdminApprovalPage.tsx) — 提供管理员审批筛选、详情、备注、同意和拒绝页面。
- [`frontend/src/components/BookingResultCard.tsx`](frontend/src/components/BookingResultCard.tsx) — 定义后续预订结果卡片展示。
- [`frontend/src/components/ChatWindow.tsx`](frontend/src/components/ChatWindow.tsx) — 连接聊天 SSE、执行事件与历史时间轴，处理反馈并在离开页面时终止旧流。
- [`frontend/src/components/LoginPage.tsx`](frontend/src/components/LoginPage.tsx) — 登录并加载服务端用户身份。
- [`frontend/src/components/MyTravelPage.tsx`](frontend/src/components/MyTravelPage.tsx) — 提供本人差旅列表、审批历史、预订记录和确认后的申请、修改、取消操作。
- [`frontend/src/components/ProgressCard.tsx`](frontend/src/components/ProgressCard.tsx) — 定义后续 Agent 规划步骤的卡片展示。
- [`frontend/src/components/Sidebar.tsx`](frontend/src/components/Sidebar.tsx) — 展示当前聊天入口、会话列表和退出登录。
- [`frontend/src/components/TravelResultCard.tsx`](frontend/src/components/TravelResultCard.tsx) — 定义后续旅行搜索结果卡片展示。

## frontend/src/store

- [`frontend/src/store/authStore.ts`](frontend/src/store/authStore.ts) — 保存登录 token 和用户摘要，并在账号切换时清空聊天内存。
- [`frontend/src/store/chatStore.ts`](frontend/src/store/chatStore.ts) — 管理当前会话、消息、反馈和流式渲染状态。
- [`frontend/src/store/uiStore.ts`](frontend/src/store/uiStore.ts) — 管理聊天、我的差旅和管理员审批三个顶层页面视图。

## frontend/src/types

- [`frontend/src/types/index.ts`](frontend/src/types/index.ts) — 定义聊天消息和后续丰富事件卡片的数据类型。

## src/gogo_agent

- [`src/gogo_agent/__init__.py`](src/gogo_agent/__init__.py) — 标记 GoGo Agent 的 Python 包。
- [`src/gogo_agent/cli.py`](src/gogo_agent/cli.py) — 装配仅含日期工具的 Agent 并提供终端入口。
- [`src/gogo_agent/tools.py`](src/gogo_agent/tools.py) — 提供不依赖模型的本地日期工具。
- [`src/gogo_agent/api.py`](src/gogo_agent/api.py) — 提供唯一 FastAPI 入口、启动配置校验、认证、会话、差旅及审批路由和健康检查。
- [`src/gogo_agent/model.py`](src/gogo_agent/model.py) — 为主/信息/意图调用统一创建 OpenAI 兼容模型并记录单次耗时与可用 token。
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
- [`src/gogo_agent/chat/config.py`](src/gogo_agent/chat/config.py) — 集中主/信息/管理 Agent 提示词、轮次、模型与工具超时配置及启动校验。
- [`src/gogo_agent/chat/continuation.py`](src/gogo_agent/chat/continuation.py) — 用可信请求和精确续聊词恢复活跃 InfoAgent，处理过期与无效状态回退。
- [`src/gogo_agent/chat/models.py`](src/gogo_agent/chat/models.py) — 定义会话、消息领域模型及请求响应视图 DTO。
- [`src/gogo_agent/chat/repository.py`](src/gogo_agent/chat/repository.py) — 实现内存与 SQL 的业务历史、AgentState 与活跃路由存取，原子提交回复及状态，并处理已删除会话 ID 的重新归属。
- [`src/gogo_agent/chat/service.py`](src/gogo_agent/chat/service.py) — 编排会话创建、归属校验后的历史读取和用户消息保存、标题及反馈。
- [`src/gogo_agent/chat/executor.py`](src/gogo_agent/chat/executor.py) — 按可信请求运行主/信息/管理 Agent 与活跃续聊，处理 JSON/SSE、推荐及状态记录。
- [`src/gogo_agent/chat/delegation.py`](src/gogo_agent/chat/delegation.py) — 记录本轮子 Agent 委派及显式重试关系，并复用服务端保留的原始参数。
- [`src/gogo_agent/chat/trace.py`](src/gogo_agent/chat/trace.py) — 观察真实主子 Agent 事件，脱敏后生成可回放时间轴并以有界队列合并 SSE。
- [`src/gogo_agent/chat/recommendation.py`](src/gogo_agent/chat/recommendation.py) — 在主答复完成后解析并去重轻量模型生成的后续问题建议。
- [`src/gogo_agent/chat/dependencies.py`](src/gogo_agent/chat/dependencies.py) — 提供会话仓储、L2 记忆库与执行器的 FastAPI 依赖注入。
- [`src/gogo_agent/chat/router.py`](src/gogo_agent/chat/router.py) — 定义 /api/chat 的会话接口，并在 SSE 建立前完成对话预处理。

## src/gogo_agent/db

- [`src/gogo_agent/db/__init__.py`](src/gogo_agent/db/__init__.py) — 导出数据库 Base、模型类、连接引擎与会话工厂。
- [`src/gogo_agent/db/base.py`](src/gogo_agent/db/base.py) — 定义统一的 SQLAlchemy DeclarativeBase 声明式基类。
- [`src/gogo_agent/db/models.py`](src/gogo_agent/db/models.py) — 定义用户账号、会话、AgentState 与差旅/审批/档案/预订/审计的 SQLAlchemy ORM 表。
- [`src/gogo_agent/db/session.py`](src/gogo_agent/db/session.py) — 管理数据库连接池、会话生成及 FastAPI 依赖注入。
- [`src/gogo_agent/db/init_db.py`](src/gogo_agent/db/init_db.py) — 仅幂等初始化阶段 A 四表与初始脱敏账号，不隐式迁移阶段 D 业务表。

## src/gogo_agent/intent

- [`src/gogo_agent/intent/__init__.py`](src/gogo_agent/intent/__init__.py) — 导出改写、识别、流水线和顺序执行的公开类型及入口。
- [`src/gogo_agent/intent/models.py`](src/gogo_agent/intent/models.py) — 定义改写、意图及三层识别结果的 Pydantic 契约和跨字段校验。
- [`src/gogo_agent/intent/context.py`](src/gogo_agent/intent/context.py) — 构造经会话归属校验和长度裁剪的最近历史与当前问题快照。
- [`src/gogo_agent/intent/execution.py`](src/gogo_agent/intent/execution.py) — 显式传递可信上下文，按事项顺序委派可注入子 Agent，聚合结果与追踪并模拟写去重。
- [`src/gogo_agent/intent/pipeline.py`](src/gogo_agent/intent/pipeline.py) — 用可信历史统一快筛、短追问与确认选择的上下文守卫、条件改写和完整识别。
- [`src/gogo_agent/intent/rules.py`](src/gogo_agent/intent/rules.py) — 执行 L0/L1 动作与对象匹配、类别优先级、复合弃权及明确否定动作的排除。
- [`src/gogo_agent/intent/runtime.py`](src/gogo_agent/intent/runtime.py) — 从共用环境配置装配聊天模型、embedding、Qdrant 与一次请求的识别流水线。
- [`src/gogo_agent/intent/seed.json`](src/gogo_agent/intent/seed.json) — 保存从 Java YAML 移植的 16 类 68 条运行时意图种子。
- [`src/gogo_agent/intent/service.py`](src/gogo_agent/intent/service.py) — 执行单次模型改写与 L3 分类，验证动作保真并编排规则、向量和 LLM 短路。
- [`src/gogo_agent/intent/vector.py`](src/gogo_agent/intent/vector.py) — 校验意图种子、构建版本化 Qdrant 索引并执行 L2 Top-2 阈值匹配。

## src/gogo_agent/travel

- [`src/gogo_agent/travel/__init__.py`](src/gogo_agent/travel/__init__.py) — 标记阶段 D 差旅业务包。
- [`src/gogo_agent/travel/models.py`](src/gogo_agent/travel/models.py) — 定义差旅单、审批、政策、资料、冲突、预订的请求与响应契约。
- [`src/gogo_agent/travel/repository.py`](src/gogo_agent/travel/repository.py) — 提供用户归属过滤、事务、订单锁与差旅业务 SQL 仓储。
- [`src/gogo_agent/travel/service.py`](src/gogo_agent/travel/service.py) — 编排本人差旅、审批、政策、联系人和预订的确定性业务规则。
- [`src/gogo_agent/travel/router.py`](src/gogo_agent/travel/router.py) — 暴露已鉴权的本人差旅接口、管理员审批接口和禁用的外部回调。
- [`src/gogo_agent/travel/dependencies.py`](src/gogo_agent/travel/dependencies.py) — 注入差旅业务服务并在数据库缺业务表时明确拒绝业务请求。
- [`src/gogo_agent/travel/confirmation.py`](src/gogo_agent/travel/confirmation.py) — 签发与校验用户、会话、签发轮次和申请摘要绑定的确认令牌，限制在后续用户轮次提交。
- [`src/gogo_agent/travel/tools.py`](src/gogo_agent/travel/tools.py) — 为管理 Agent 装配带可信身份的查询工具与两段式差旅申请提交工具（先签发摘要与令牌，带令牌才写库）。
- [`src/gogo_agent/travel/policy.py`](src/gogo_agent/travel/policy.py) — 按职级、城市、有效期选择结构化政策并确定性检查金额和舱位。
- [`src/gogo_agent/travel/policy_seed.json`](src/gogo_agent/travel/policy_seed.json) — 保存版本化政策规则、城市档位与交通衔接时长种子。
- [`src/gogo_agent/travel/knowledge.py`](src/gogo_agent/travel/knowledge.py) — 离线导入 DOCX 政策片段并按词面检索带来源版本的证据。
- [`src/gogo_agent/travel/policy_corpus.sample.json`](src/gogo_agent/travel/policy_corpus.sample.json) — 提供可离线检索的脱敏政策片段样例。
- [`src/gogo_agent/travel/conflict.py`](src/gogo_agent/travel/conflict.py) — 检测本人行程的日期重叠、跨城衔接和未知交通时长。
- [`src/gogo_agent/travel/cache.py`](src/gogo_agent/travel/cache.py) — 缓存按用户、参数、版本和日期隔离的短时政策查询结果。

## src/gogo_agent/planning

- [`src/gogo_agent/planning/__init__.py`](src/gogo_agent/planning/__init__.py) — 标记阶段 E 行程规划包。
- [`src/gogo_agent/planning/models.py`](src/gogo_agent/planning/models.py) — 定义交通与酒店候选、政策、方案、审核结论及带版本的计划状态契约。
- [`src/gogo_agent/planning/scoring.py`](src/gogo_agent/planning/scoring.py) — 确定性地组合去程、住宿、返程，过滤、归一化评分并选出代表方案，不读写存储。
- [`src/gogo_agent/planning/steps.py`](src/gogo_agent/planning/steps.py) — 维护 Plan-and-Execute 步骤状态、失败次数、条件变化失效与已确认写入去重。
- [`src/gogo_agent/planning/review.py`](src/gogo_agent/planning/review.py) — 执行客观六维审核、有限次主观评估、确定性仲裁，并把结论绑定计划内容版本。
- [`src/gogo_agent/planning/service.py`](src/gogo_agent/planning/service.py) — 编排计划创建、搜索、计算、审核与版本递增，所有操作限定于本人计划。
- [`src/gogo_agent/planning/store.py`](src/gogo_agent/planning/store.py) — 以用户与计划标识隔离保存计划快照，并用版本号拒绝并发覆盖。
- [`src/gogo_agent/planning/tools.py`](src/gogo_agent/planning/tools.py) — 为规划智能体装配只读候选、计算、审核工具，身份只来自服务端上下文。
- [`src/gogo_agent/planning/agent.py`](src/gogo_agent/planning/agent.py) — 组装行程规划 Agent 与其系统提示词，不包含差旅单写入或供应商下单工具。
- [`src/gogo_agent/planning/simulated.py`](src/gogo_agent/planning/simulated.py) — 提供固定模拟候选、模拟政策与确定性主观评估替身，仅用于练习和测试。
- [`src/gogo_agent/planning/search.py`](src/gogo_agent/planning/search.py) — 经途牛 CLI 只读搜索去程、返程与目的地酒店，只使用当前用户自己的 API Key，缺 Key 或搜索失败时显式报错。
- [`src/gogo_agent/planning/assessors.py`](src/gogo_agent/planning/assessors.py) — 053 的三个主观评估器：通过稳定模型角色各调用一次模型，输出结构化结论，无效输出记为不可用。
- [`src/gogo_agent/planning/dependencies.py`](src/gogo_agent/planning/dependencies.py) — 提供进程内唯一的规划服务；有数据库时把计划持久化到 `itinerary_plan` 表（跨重启/多 worker 可读回），无数据库或 `GOGO_PLAN_STORE=memory` 时回落内存实现。

## src/gogo_agent/booking

- [`src/gogo_agent/booking/__init__.py`](src/gogo_agent/booking/__init__.py) — 标记阶段 F 预订与工具可靠性包。
- [`src/gogo_agent/booking/service.py`](src/gogo_agent/booking/service.py) — 作为唯一下单入口，执行审核、差旅审批、同行程段唯一与幂等门禁，并处理未知结果与酒店取消顺序。
- [`src/gogo_agent/booking/supplier.py`](src/gogo_agent/booking/supplier.py) — 定义供应商下单、查询与取消接口，提供可脚本化的模拟供应商和接入阶段 D 取消的适配器。
- [`src/gogo_agent/booking/reliability.py`](src/gogo_agent/booking/reliability.py) — 提供只读重试、写入不重试、有界并行搜索与按工具名套用的熔断状态机。
- [`src/gogo_agent/booking/tools.py`](src/gogo_agent/booking/tools.py) — 为预订 Agent 装配门禁检查与只读查询工具，不包含下单或取消工具。
- [`src/gogo_agent/booking/agent.py`](src/gogo_agent/booking/agent.py) — 组装预订 Agent 与其系统提示词，限定其不能自行下单或修改审核结论。

## src/gogo_agent/integrations

- [`src/gogo_agent/integrations/__init__.py`](src/gogo_agent/integrations/__init__.py) — 说明阶段 E 外部能力包：安全运行外部 CLI、途牛只读搜索、实时查询、天气 MCP 与能力清单。
- [`src/gogo_agent/integrations/skill_runner.py`](src/gogo_agent/integrations/skill_runner.py) — 以参数列表安全运行外部 CLI，执行命令白名单、输出与超时限制、用户隔离目录、环境变量白名单和密钥屏蔽。
- [`src/gogo_agent/integrations/tuniu.py`](src/gogo_agent/integrations/tuniu.py) — 通过安全运行器做途牛机票、火车、酒店只读搜索，并规范化为规划候选；拒绝所有下单与取消命令。
- [`src/gogo_agent/integrations/destination_live.py`](src/gogo_agent/integrations/destination_live.py) — 提供目的地实时查询的客户端、带来源与有效期的返回结构，以及区分查到数据与未验证信息的文本渲染。
- [`src/gogo_agent/integrations/weather_mcp.py`](src/gogo_agent/integrations/weather_mcp.py) — 用 AgentScope `MCPClient` 封装天气 MCP 的连接生命周期、工具发现、调用与错误转换。
- [`src/gogo_agent/integrations/catalog.py`](src/gogo_agent/integrations/catalog.py) — 记录 Java 当前启用的旅行 Skill 与 MCP 能力清单，标明是否迁移、只读或写入及凭证类型，并校验每个行为只有一个权威入口。

## src/gogo_agent/security

- [`src/gogo_agent/security/__init__.py`](src/gogo_agent/security/__init__.py) — 说明阶段 E 凭证与第三方鉴权包的边界：凭证不进入提示词、日志、工具响应或模型上下文。
- [`src/gogo_agent/security/errors.py`](src/gogo_agent/security/errors.py) — 定义凭证相关稳定错误码，文本固定且不含密钥、密文或 Token。
- [`src/gogo_agent/security/masking.py`](src/gogo_agent/security/masking.py) — 对输出、日志和异常文本做最小必要脱敏，并提供日志过滤器。
- [`src/gogo_agent/security/cipher.py`](src/gogo_agent/security/cipher.py) — 以 AES-256-GCM 加密凭证，密文带密钥版本，从环境变量读取多密钥并支持轮换；缺失密钥时明确失败。
- [`src/gogo_agent/security/apikey.py`](src/gogo_agent/security/apikey.py) — 管理用户级第三方 API Key：仅本人可增删查，列表脱敏，撤销后旧 Key 不再可取用。
- [`src/gogo_agent/security/credentials.py`](src/gogo_agent/security/credentials.py) — 记录各集成的凭证持有者、资源与撤销方式，决定每次供应商请求使用谁的授权且不回退。
- [`src/gogo_agent/security/token_header.py`](src/gogo_agent/security/token_header.py) — 从服务端读取配置型 Token，仅在请求目标供应商域名时注入请求头，缺失或跨域时在发出前失败。
- [`src/gogo_agent/security/pkce.py`](src/gogo_agent/security/pkce.py) — 提供 PKCE 与一次性 state 的教学练习，含模拟授权方和模拟 CLI，不是生产登录实现。
- [`src/gogo_agent/security/exercises/__init__.py`](src/gogo_agent/security/exercises/__init__.py) — 标记 050/051 独立练习包，练习不接入正式规划路径。
- [`src/gogo_agent/security/exercises/flight_manager_key.py`](src/gogo_agent/security/exercises/flight_manager_key.py) — 用假 Key 练习航班管家调用契约与占位符替换，标注为当前未启用。
- [`src/gogo_agent/security/exercises/rgh_workspace.py`](src/gogo_agent/security/exercises/rgh_workspace.py) — 用模拟 CLI 练习 Rolling Go 按用户隔离工作空间与登录态，标注为当前未启用。

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
- [`docs/契约样例/018-确认提交与方案选择调试.md`](docs/契约样例/018-确认提交与方案选择调试.md) — 提供确认提交误路由的离线运行、Java/Python 对照、断点、浏览器路线和提交边界。
- [`docs/契约样例/019-主协调入口与直跳取舍.md`](docs/契约样例/019-主协调入口与直跳取舍.md) — 记录不启用高置信直跳的依据、当前协调入口验收及后续候选条件。
- [`docs/契约样例/020-执行顺序与续跑边界.md`](docs/契约样例/020-执行顺序与续跑边界.md) — 对照新请求与活跃 Agent 续跑顺序，并提供面向 Java 工程师的逐步调试与验收指南。
- [`docs/契约样例/021-请求上下文与信任边界.md`](docs/契约样例/021-请求上下文与信任边界.md) — 区分认证上下文与模型可见业务线索，记录传参、伪造身份及追踪验收。
- [`docs/契约样例/024-Agent关键配置与失败出口.md`](docs/契约样例/024-Agent关键配置与失败出口.md) — 对照主/信息 Agent 的配置来源、Java 差异、失败出口与运行验收。
- [`docs/契约样例/025-Agent配置矩阵.md`](docs/契约样例/025-Agent配置矩阵.md) — 对照 Java 六个 ReAct Agent、轻量调用和 Python 当前角色的模型、工具、状态、写权限及运行限制。
- [`docs/契约样例/026-模型分工与观测.md`](docs/契约样例/026-模型分工与观测.md) — 对照 Java/Python 模型角色、环境配置、单次调用日志与离线验收边界。
- [`docs/契约样例/027-活跃InfoAgent续跑.md`](docs/契约样例/027-活跃InfoAgent续跑.md) — 对照 Java/Python 活跃路由语义，提供 InfoAgent 续聊的逐步调试和验收指南。
- [`docs/契约样例/028-可信身份与会话归属.md`](docs/契约样例/028-可信身份与会话归属.md) — 说明模型工具参数、认证上下文和 SQL 会话归属的信任边界与负例。
- [`docs/契约样例/阶段D-029-041-调试指南.md`](docs/契约样例/阶段D-029-041-调试指南.md) — 提供阶段 D 的 Java/Python 对照、可复制命令、逐断点验收、预测练习与限制。
- [`docs/契约样例/阶段D-前端与Scalar逐步调试.md`](docs/契约样例/阶段D-前端与Scalar逐步调试.md) — 指导用本地浏览器练习库、React 聊天页、Scalar、PyCharm 断点逐项学习 029–041。
- [`docs/契约样例/阶段F-061-067-调试指南.md`](docs/契约样例/阶段F-061-067-调试指南.md) — 提供预订门禁、唯一下单、未知结果对账、取消、只读重试、并行搜索与熔断的 Java/Python 对照、断点、练习与未验证边界。
- [`docs/契约样例/048-旅行Skill与MCP清单.md`](docs/契约样例/048-旅行Skill与MCP清单.md) — 核对 Java 旅行 Skill 与 MCP 的启用状态、凭证与写权限，并给出 Python 迁移去向。
- [`docs/契约样例/阶段E-046-049-058-调试指南.md`](docs/契约样例/阶段E-046-049-058-调试指南.md) — 提供外部能力的 Java/Python 对照、零准备运行、断点、预测练习、已知缺陷与未验证边界。
- [`docs/契约样例/055-第三方鉴权授权图.md`](docs/契约样例/055-第三方鉴权授权图.md) — 用“用户、服务、供应商”授权图和集成清单说明各凭证的持有者、资源、更新与撤销方式。
- [`docs/契约样例/阶段E-050-059-调试指南.md`](docs/契约样例/阶段E-050-059-调试指南.md) — 提供凭证与鉴权练习的 Java/Python 对照、零准备运行、断点、预测练习与未验证边界。
- [`docs/契约样例/阶段E-042-060-调试指南.md`](docs/契约样例/阶段E-042-060-调试指南.md) — 提供阶段 E 规划核心的 Java/Python 对照、零准备运行、逐断点验收、预测练习与未验证边界。
- [`docs/契约样例/阶段E-前端触发规划Agent逐步调试.md`](docs/契约样例/阶段E-前端触发规划Agent逐步调试.md) — 指导用练习库、React 聊天页、Scalar、断点和日志逐步调试 042–060 的规划 Agent 触发链路。
- [`docs/契约样例/执行过程时间轴-调试指南.md`](docs/契约样例/执行过程时间轴-调试指南.md) — 说明 Java 组件复用、Python 事件协议、离线浏览器运行、逐断点观察和回放边界。

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
- [`docs/research/issue-compute-plan-参数契约缺失.md`](docs/research/issue-compute-plan-参数契约缺失.md) — 记录 compute_plan 参数契约缺失缺陷的根因、责任归属、修复内容与验收结果。

## docs/迁移

- [`docs/迁移/前端提前迁移.md`](docs/迁移/前端提前迁移.md) — 记录原 React 前端已接入的 Python HTTP/SSE 能力及后续页面的恢复条件。

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
- [`scripts/demo_020_execution_order.py`](scripts/demo_020_execution_order.py) — 用离线模型和内存状态演示新请求、同会话恢复及续跑回退，并标出教学断点与前置轮次。
- [`scripts/demo_021_request_context.py`](scripts/demo_021_request_context.py) — 用本地 Token、假子 Agent 和假工具演示可信身份传递及多步骤追踪。
- [`scripts/demo_022_async_context.py`](scripts/demo_022_async_context.py) — 用离线双请求与后台任务演示显式身份传递、追踪隔离和空任务上下文。
- [`scripts/demo_023_master_agent.py`](scripts/demo_023_master_agent.py) — 用离线 HTTP、固定模型和真实 AgentScope Toolkit 演示主 Agent 委派与未注册工具拒绝。
- [`scripts/demo_024_agent_config.py`](scripts/demo_024_agent_config.py) — 用离线模型演示启动配置、信息工具超时及主 Agent 轮次耗尽。
- [`scripts/demo_026_model_roles.py`](scripts/demo_026_model_roles.py) — 用固定模型响应演示主/稳定档调用、token 日志和切换模型后的身份仓储不变。
- [`scripts/demo_027_active_info.py`](scripts/demo_027_active_info.py) — 用离线 HTTP 演示真实 InfoAgent 活跃记录、精确续聊、回退和跨用户拒绝。
- [`scripts/demo_028_trusted_identity.py`](scripts/demo_028_trusted_identity.py) — 用固定工具调用和 SQLite 演示伪造身份拒绝与删除会话复用后的归属。
- [`scripts/demo_execution_trace.py`](scripts/demo_execution_trace.py) — 用固定模型与真实规划工具提供执行时间轴的离线命令和隔离浏览器后端。
- [`scripts/demo_confirmation_routing.py`](scripts/demo_confirmation_routing.py) — 用固定模型、真实主子 Agent 和隔离 SQLite 演示确认路由、委派重试、两轮提交与幂等。
- [`scripts/init_stage_d_test_db.py`](scripts/init_stage_d_test_db.py) — 仅在隔离 SQLite 中创建阶段 D 业务表并展示建表前后结果。
- [`scripts/migrate_stage_d_tables.py`](scripts/migrate_stage_d_tables.py) — 预检并显式创建 `.env` MySQL 中缺失的阶段 D 项目表，核验项目字段注释与关键唯一约束。
- [`scripts/prepare_stage_d_browser_lab.py`](scripts/prepare_stage_d_browser_lab.py) — 创建不接触 `.env` 数据库的临时 SQLite、测试账号职级与模拟预订，供浏览器调试。
- [`scripts/prepare_stage_e_browser_lab.py`](scripts/prepare_stage_e_browser_lab.py) — 创建含用户级途牛 Key 表的阶段 E 练习库、3 个账号、假途牛命令与保密变量文件，供前端触发规划 Agent 调试。
- [`scripts/demo_029_040_stage_d.py`](scripts/demo_029_040_stage_d.py) — 用离线模型、真实管理 Toolkit 与隔离数据库逐项演示 029–040 分支。
- [`scripts/build_policy_corpus.py`](scripts/build_policy_corpus.py) — 将指定 DOCX 离线切分为带来源和 SHA-256 版本的政策索引。
- [`scripts/demo_034_policy_knowledge.py`](scripts/demo_034_policy_knowledge.py) — 用临时脱敏 DOCX 演示政策索引导入、检索与更新重建。
- [`scripts/benchmark_041_thinking.py`](scripts/benchmark_041_thinking.py) — 用固定题集对照默认和高推理模型的耗时、JSON 有效率及工具误调用。
- [`scripts/demo_042_060_planning.py`](scripts/demo_042_060_planning.py) — 用模拟候选、固定政策和确定性评估器演示步骤推进、方案计算、审核仲裁与版本归属，不访问网络或数据库。
- [`scripts/demo_050_059_security.py`](scripts/demo_050_059_security.py) — 用隔离内存库、假授权方和假 CLI 演示 API Key 加密脱敏、授权清单、Token 注入与 PKCE 边界，不访问真实服务。
- [`scripts/demo_061_067_booking.py`](scripts/demo_061_067_booking.py) — 用模拟供应商、内存库和固定时钟演示预订门禁、响应丢失对账、取消、只读重试、并行搜索与熔断。
- [`scripts/demo_046_058_integrations.py`](scripts/demo_046_058_integrations.py) — 用假 CLI、假天气 MCP 服务与模拟传输演示安全运行器、途牛规范化、实时查询与天气 MCP 的生命周期。
- [`scripts/fake_tuniu_cli.py`](scripts/fake_tuniu_cli.py) — 纯标准库模拟途牛 CLI，可按环境变量返回成功、失败、非 JSON、挂起、超大输出，或把样例日期改为请求日期（`FAKE_TUNIU_ECHO_DATES=1`）。
- [`scripts/fake_weather_mcp_server.py`](scripts/fake_weather_mcp_server.py) — 用 FastMCP 提供 stdio 天气 forecast 工具，可按环境变量模拟工具错误、坏响应、崩溃与挂起。

## output

- [`output/l0_l1_intent_routing.html`](output/l0_l1_intent_routing.html) — 提供 L0/L1 意图路由和规则否决条件的独立交互图解。
- [`output/show_me_023_execution_flow.html`](output/show_me_023_execution_flow.html) — 提供 023 从 HTTP 请求到 MasterModel._call_api 调用链路与 Java 工程化映射图解。
- [`output/show_me_fastapi_depends_quiz.html`](output/show_me_fastapi_depends_quiz.html) — 提供 FastAPI Depends 核心机制、DAG 拓扑求解与 Java 对照的通关测评交互页面。
- [`output/show_me_027_active_routing.html`](output/show_me_027_active_routing.html) — 提供 027 快速路由到上次子 Agent 的执行链路与两轮交互深度暗黑图解。
- [`output/show_me_027_active_agent_validation.html`](output/show_me_027_active_agent_validation.html) — 提供 027 ActiveAgentRecord 三重门禁校验与惰性淘汰清理图解。

## tests

- [`tests/__init__.py`](tests/__init__.py) — 标记测试目录为 Python 包。
- [`tests/test_exercises.py`](tests/test_exercises.py) — 验证 000 系列小练习的输入校验、并发性能与 HTTP 端点行为。
- [`tests/test_execution_trace.py`](tests/test_execution_trace.py) — 验证真实主子事件、SQLite 回放与权限、脱敏、等待/失败状态、背压和断连清理。
- [`tests/test_confirmation_routing.py`](tests/test_confirmation_routing.py) — 验证确认与选择的上下文、动作保真及 HTTP JSON/SSE 管理委派、两轮提交和令牌边界。
- [`tests/test_delegation_retry.py`](tests/test_delegation_retry.py) — 验证显式委派重试、原参数复用、令牌状态保存及未恢复失败的 JSON/SSE 出口。
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
- [`tests/test_026_model_roles.py`](tests/test_026_model_roles.py) — 验证主/稳定模型装配、缺失配置、逐次调用日志和身份仓储隔离。
- [`tests/test_027_active_info.py`](tests/test_027_active_info.py) — 验证真实 InfoAgent 的 JSON/SSE 续聊、过期回退、跨用户隔离及 SQLite 活跃记录。
- [`tests/test_028_identity_boundary.py`](tests/test_028_identity_boundary.py) — 验证模型伪造工具身份无效和 SQL 已删除会话重新归属的隔离。
- [`tests/test_029_041_stage_d.py`](tests/test_029_041_stage_d.py) — 验证管理路由、工具身份、政策索引、缓存、推荐顺序及差旅 HTTP 权限。
- [`tests/test_030_038_travel_business.py`](tests/test_030_038_travel_business.py) — 验证模型、政策、资料、差旅事务、冲突、预订与审批状态边界。
- [`tests/conftest.py`](tests/conftest.py) — 测试全局隔离：默认置 `GOGO_PLAN_STORE=memory`，避免用例写入 `.env` 指向的真实库。
- [`tests/test_042_060_planning.py`](tests/test_042_060_planning.py) — 验证规划步骤推进、确定性方案计算、审核仲裁、计划版本与归属隔离、跨实例持久化及规划工具边界。
- [`tests/test_050_059_security.py`](tests/test_050_059_security.py) — 验证脱敏与日志无明文、加密轮换、用户级 Key 隔离、授权归属不回退、Token 注入边界和 PKCE 负例。
- [`tests/test_046_049_058_integrations.py`](tests/test_046_049_058_integrations.py) — 验证外部 CLI 的白名单、限额、超时清理、用户隔离、密钥屏蔽，途牛规范化，实时查询三态，以及天气 MCP 的真实 stdio 连接与错误映射。
- [`tests/test_061_067_booking.py`](tests/test_061_067_booking.py) — 验证预订门禁、唯一下单与幂等、未知结果不重复下单、取消顺序、预订工具边界、只读重试、并行隔离与熔断状态。
- [`tests/test_043_planning_chat.py`](tests/test_043_planning_chat.py) — 验证途牛候选只用当前用户的 Key、搜索失败与缺 Key 显式报错、聊天只在规划意图下注册规划工具、规划 Agent 工具面不含预订与取消。
- [`tests/test_053_subjective_review.py`](tests/test_053_subjective_review.py) — 验证模型主观评估的结构化输出、无效输出与异常转为不可用、三维度与客观结论的仲裁。
- [`tests/test_mariadb_integration.py`](tests/test_mariadb_integration.py) — 验证真实 MariaDB 数据库连接、用户密码迁移、会话消息与 agentscope_session 存取。
- [`tests/test_redis_integration.py`](tests/test_redis_integration.py) — 验证真实 172.22.22.123 Redis 连接、30 天 TTL、跨实例 Token 持久化与平滑降级。
