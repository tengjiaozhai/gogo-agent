# 007 Agent 职责清单与装配对照

> 记录日期：2026-09-27。Java 基线：`/Users/shenmingjie/workSpace/LLMentor-teach/gogo-agent`，HEAD `9b5a8ec`。下文 Java 类路径相对 `src/main/java/com/gogo/travel/`，提示词路径相对 `src/main/resources/`。本项完成源码静态盘点，未启动 Java、调用模型或执行业务工具。职责取舍见 [006](006-多智能体职责边界.md)。

## 1. 先认清实际装配

源码有 **6 处 `ReActAgent.builder()`**：Master、Manage、Plan、Info、Booking，以及已弃用的 Review。普通业务路径包含一个 Master 和四个可按需调用的业务子 Agent；这不表示每次请求都会运行五个 Agent。改写、识别是另外两个 `AgentBase` 单次调用，报销是返回 `null` 的占位，不能把原文的“9 个智能体”理解成 9 个已运行的 ReActAgent。

### Master 的四项真实注册

| Master 工具名 | Spring Bean | 实例名称 | 副作用与迁移定位 |
| --- | --- | --- | --- |
| `itinerary_manage_agent` | `itineraryManageAgent` | `ItineraryManageAgent` | 写差旅申请、修改/取消差旅单及维护用户资料；涉及申请提交，不作管理员审批决定。 |
| `itinerary_plan_agent` | `itineraryPlanAgent` | `ItineraryPlanAgent` | 保存候选、方案和 HTML；Java shell 能力还存在订单写入暴露，见 006。 |
| `info_agent` | `infoAgent` | `InfoAgent` | **Python 首个只读业务子 Agent 的目标**：政策/公共信息查询；“只读”指业务数据，仍会调用模型及保存对话状态。 |
| `booking_agent` | `bookingAgent` | `BookingAgent` | 真实供应商下单/取消及本地预订记录写入；应在隔离环境落实确认、审批和幂等条件。 |

注册证据：`agent/MasterAgent.java:83-126`。Master 另直接注册 `UserInteractionTools`（`ask_user`），没有直接注册 `InfoQueryTools`、差旅单写工具或预订写工具。高置信直跳子 Agent 的调用仍被注释，普通新请求经过 Master；续跑活跃 Agent 属于另一条路径。

## 2. 常规路径的五项职责清单

以下每个 Agent 均按**输入、输出、工具、状态、失败处理**五栏记录。子 Agent 注册说明中的 `message`、可选 `session_id` 是内部委派描述，不是新增 HTTP 接口；可信用户身份来自服务端 `AgentSessionContext`。外层返回值是 Agent 的 `Msg`，不把工具内部的 JSON 结果等同于 Agent 已有固定输出 schema。

### 2.1 MasterAgent：协调与结果整合

| 输入 | 输出 | 工具 | 状态 | 失败处理 |
| --- | --- | --- | --- | --- |
| 原始消息，加上原句/改写句和意图 JSON 两条系统上下文；服务端注入用户与会话。 | 整合后的 `Msg`；缺信息时可经 `ask_user` 请求补充。提示词要求多意图按 `intents` 顺序执行，并保留子 Agent 的方案和确认问句。 | 上表四个子 Agent 入口及 `ask_user`。 | `AutoContextMemory`；可选按用户构建的长期记忆；注册会话持久化和 pending-tool 恢复 Hook。没有注册活跃 Agent 持久化 Hook。 | 子工具配置超时 15 分钟、最多 3 次尝试；最终执行异常由外层执行器处理。多意图没有在此处声明数据库事务，不能把部分执行失败当作全部回滚。 |

依据：`agent/MasterAgent.java:68-149`、`agent/service/AgentPipelineService.java:393-411`、`prompts/master-agent-system.md:26-42`。注册描述中 Plan 仍包含“预订”一词，而 Booking 有独立下单入口；Python 按 006 的职责边界收敛，不能照搬这段描述扩大 Plan 权限。

### 2.2 ItineraryManageAgent：差旅单生命周期

| 输入 | 输出 | 工具 | 状态 | 失败处理 |
| --- | --- | --- | --- | --- |
| 申请、修改、取消或查询任务；地点、日期、事由、单号等业务信息；可信用户上下文。 | `Msg` 中的缺项询问、差旅单/审批状态或操作结果；工具内部返回业务 JSON。 | `TravelOrderWriteTools`、`TravelOrderReadTools`、`BookingReadTools`、`TravelOrderConflictTools`、`UserInfoReadTools`、`UserInfoWriteTools`。 | 短期记忆、会话持久化、活跃 Agent 记录和 pending-tool 恢复；差旅单/审批/用户资料由业务仓储负责。builder 未配置长期记忆。 | 缺字段/非法日期由提交工具返回 `INVALID_PARAM`/`INVALID_DATE_RANGE`，不创建申请；执行配置最多 3 次尝试，仅对 `IOException` 重试。提示词要求把工具错误转成可读说明。 |

依据：`agent/ItineraryManageAgent.java:41-73`、`agent/tools/TravelOrderWriteTools.java:126-167`、`agent/BaseSubAgent.java:193-202`、`prompts/itinerary-manage-agent-system.md:96-98`。实际未注册 `PolicyTools` 和 `BookingWriteTools`，不能按原文工具列表给它增加政策查询或取消供应商预订入口。

### 2.3 ItineraryPlanAgent：候选搜索、规划与审核修复

| 输入 | 输出 | 工具 | 状态 | 失败处理 |
| --- | --- | --- | --- | --- |
| 规划任务、出发地/目的地/日期、偏好、差旅单与政策信息；可信用户上下文。审批前置要求来自业务流程与提示词，不能视为已验证的统一工具门禁。 | `Msg` 中的方案摘要、比较结果及确认问句；另有保存的结构化方案、HTML 和计划进度事件。 | 政策、差旅单/预订读取、用户信息读写、API Key、目的地、天气/签证 MCP、`ItineraryPlannerTools`、`ItineraryPlanReadTools`、`ItineraryReviewTools`、`PlanHtmlTools`；`tuniu-cli`/`html-plan` Skill 与 shell。 | 短期与可选用户长期记忆、`PlanNotebook`、会话持久化、活跃 Agent 记录；候选、方案和审核报告有各自的存储，不能用聊天文本代替。 | 无搜索候选时 `plan_itinerary` 返回错误；非法 JSON、无法规划等返回明确失败。工具超时 3 分钟，最多 3 次尝试，仅重试 `IOException`；目的地实况工具配置熔断分组。 |

依据：`agent/ItineraryPlanAgent.java:64-133`、`agent/tools/ItineraryPlannerTools.java:113-150`。审核走 `review_itinerary` 工具，没有嵌套注册独立 ReviewAgent；Java shell 允许 `tuniu` 且启用写能力，提示词的“只搜索”尚不能形成下单权限隔离。

### 2.4 InfoAgent：首个只读业务子 Agent

| 输入 | 输出 | 工具 | 状态 | 失败处理 |
| --- | --- | --- | --- | --- |
| 政策、目的地、天气、签证等查询问题；涉及个人差标时需要可信用户身份和目的地。 | 根据工具/知识库结果整理的中文 `Msg`；区分精确个人差标与通用政策说明。 | `PolicyTools`、`DestinationLiveTools`、天气/签证 MCP；景点、差旅政策、差旅指南三个知识库，`RAGMode.AGENTIC`。 | 短期记忆、会话持久化与 pending-tool 恢复；builder 没有长期记忆或活跃 Agent 持久化 Hook。 | 默认工具配置为 1 分钟、最多 3 次尝试、仅重试 `IOException`；目的地实况工具熔断。结果为空/异常时如实说明、不得编造是提示词要求，不能声称代码已保证模型遵守。 |

依据：`agent/InfoAgent.java:30-77`、`agent/BaseSubAgent.java:193-202`、`prompts/info-agent-system.md:19-57`。阶段 C 先接入它验证 Master → 只读子 Agent 与身份传递；实际知识库、MCP 等能力仍按对应编号逐步迁移。

### 2.5 BookingAgent：预订与取消执行

| 输入 | 输出 | 工具 | 状态 | 失败处理 |
| --- | --- | --- | --- | --- |
| 用户确认的方案编号/预订要素或取消任务；差旅单、旅客/联系人信息及可信用户上下文。提示词要求从 `get_proposals` 取结构化方案并检查审批。 | `Msg` 中的各项预订/取消结果；成功供应商返回另由 Hook 解析并写入本地记录，可能发送 `booking_result` 事件。 | 差旅单/预订/方案读取、`BookingWriteTools.cancel_booking`、用户信息读写、API Key、`tuniu-cli` 与 shell。没有 `TravelOrderWriteTools`。 | 短期与可选用户长期记忆、会话持久化、活跃 Agent 记录、pending-tool 恢复；预订记录与供应商订单状态由业务持久化路径维护。 | 取消工具明确校验记录存在和用户归属；供应商取消失败时不改内部取消状态。模型下单失败须如实告知是提示词要求；不能用“模型说成功”替代供应商结果和落库证据。 |

依据：`agent/BookingAgent.java:59-116`、`agent/tools/BookingWriteTools.java:61-106`、`prompts/booking-agent-system.md:7-27,64-66`。审批检查与用户确认目前部分依赖提示词，后续实现须落到确定性工具边界；不能把默认重试配置当成订单去重保证。

### 2.6 状态保存与失败出口不能混为一谈

| 状态 | Java 实际归属与生命周期 | 失败或隔离边界 |
| --- | --- | --- |
| Agent 会话记忆 | `SessionPersistenceHook` 以 `sessionId:agentName` 保存，调用前 `loadIfExists`，调用后 `saveTo`；恢复已暂停执行时跳过加载。五个常规 Agent 均注册该 Hook。 | 当前读写路径没有本地异常捕获；持久化失败会向调用方传播，不能声称自动回退到内存。 |
| 活跃 Agent | `ActiveAgentSessionStore` 使用 `sessionId:router` 下的 `activeAgent` 字段。五个常规 builder 中，Manage、Plan、Booking 挂载对应 Hook，Master、Info 未挂载。 | Hook 本身只排除 Master，没有三角色白名单；存储失败记录日志，读取失败返回 `null`。记忆已保存不等于活跃路由已保存。 |
| 搜索候选与规划结果 | Redis 分别用 `planner:search:<userId>`、`planner:result:<userId>`，24 小时 TTL；方案 Hash field 为出发地、目的地、去程日期的组合。 | 键不包含 `sessionId`，同用户不同会话会共用候选/同一行程方案；不能据此声称已按会话隔离。空用户还会落入 `default`，后续 Python 工具入口须依赖可信身份。 |
| 上轮审核报告 | `review_itinerary` 用当前 `sessionId` 读取和保存上轮报告。 | 报告与用户维度的规划缓存是不同存储归属，后续传递方案引用时需一并核对。 |
| 预订记录 | `BookingPersistenceHook` 解析供应商结果，将用户与会话关联到本地预订记录。 | Hook 捕获落库异常、记录日志后返回原事件；主对话成功不证明预订记录已保存。 |

依据：`agent/hook/SessionPersistenceHook.java:73-102`、`agent/hook/ActiveAgentPersistenceHook.java:35-49`、`agent/session/ActiveAgentSessionStore.java:20-59,74-75`、`agent/tools/store/SearchCandidateStore.java:25-54`、`agent/tools/store/ItineraryPlanStore.java:29-73`、`agent/tools/ItineraryReviewTools.java:130-169`、`agent/hook/BookingPersistenceHook.java:84-101,169-205`。

执行错误与等待用户是两种出口：执行异常进入 `ChatAgentExecutor` 的错误订阅回调并调用 `sendError`；`TOOL_SUSPENDED` 且含交互工具时，保存本地 AgentSession 和共享 pending 状态，发送用户交互事件，主流程完成时关闭当前 SSE，等待后续恢复。助手回复落库异常也会被捕获后继续发送正文。证据：`agent/service/ChatAgentExecutor.java:147-169,304-334`。

表中的 `PendingToolRecoveryHook` 来自 AgentScope Java SDK（例如 `agent/MasterAgent.java:14`）；本项只核对其注册，未核验 SDK 内部恢复策略。应用层存在共享状态并不等于本次已验证跨实例恢复，相关运行验收仍在后续编号。

## 3. 轻量调用与特殊入口

### 无需业务工具循环的四项能力

| 能力 | 输入 | 输出 | 工具 | 状态 | 失败处理 |
| --- | --- | --- | --- | --- | --- |
| `QueryRewritingAgent`（`AgentBase`） | 流水线传入的历史消息与本轮问题。 | 单次模型生成的文本 `Msg`，流水线尝试从其中的 JSON 提取 `rewritten_question`。 | 无业务工具；`stableModel.stream(..., null, null)`。 | 本身 `doObserve` 为空，没有配置业务 Agent 的会话记忆；历史由调用方提供。 | 中断返回带 `INTERRUPTED` 标记的“已停止问题改写”；解析失败使用改写 Agent 输出原文，调用异常继续向执行器传播。 |
| `IntentRecognitionAgent`（`AgentBase`） | 最新问题，必要时为改写后的问题。 | L1/L2 的意图 JSON 文本，或单次 L3 模型生成的 JSON 文本 `Msg`；Agent 内没有强类型 JSON 校验。 | 确定性识别路由及模型调用；不提供订单工具。 | `doObserve` 为空，一次性识别器。 | 快速层不命中才调 L3；中断返回 `INTERRUPTED`。流水线直接把输出交给 Master，调用异常传播到执行器，没有这里的错误回退分支。 |
| `ConversationTitleService` | 会话/用户、问题、意图 JSON。 | 清洗并限制到 24 字的标题，更新业务会话。 | 一次 `fastModel` 调用及会话服务，无 ReAct 工具循环。 | 业务标题字段，不是独立 AgentState。 | 后台任务模型等待 30 秒；空结果/会话不存在跳过，异常记录日志。 |
| `QuestionRecommendationService` | 本轮问题和助手最终回答。 | 最多 4 条推荐问题；执行器后续可发送推荐事件。 | 一次 `fastModel` 调用，无业务工具。 | 无独立对话记忆。 | 回答为空、生成失败或 JSON 解析失败返回空列表。 |

依据：`agent/QueryRewritingAgent.java:24-86`、`agent/IntentRecognitionAgent.java:68-130`、`business/chat/service/ConversationTitleService.java:37-107,129-140`、`agent/service/QuestionRecommendationService.java:24-120`。这些能力均不在 Master 的四项子 Agent 注册中。

改写解析和识别传递的依据是 `agent/service/AgentPipelineService.java:175-196,343-357`。其中“解析失败使用原始文本”指改写 Agent 的输出文本，并非回退到用户原问题；后续 008/010 需明确结构化解析失败契约，不能把现状误记成已有安全兜底。

### 已弃用但仍可由调试入口构造的 ReviewAgent

| 输入 | 输出 | 工具 | 状态 | 失败处理 |
| --- | --- | --- | --- | --- |
| 调试接口显式选择 `ItineraryReviewAgent` 并传任务消息，服务端构造会话上下文。 | 审核说明 `Msg`；现行业务规划直接使用 `review_itinerary` 工具的报告。 | 政策、目的地实况、审核工具。 | builder 有短期/可选用户长期记忆，但没有注册 `SessionPersistenceHook` 或 `ActiveAgentPersistenceHook`，不可套用普通子 Agent 的保存假设。 | 使用默认工具执行配置；调试调用由调试入口和执行器处理。未验证这条历史入口的端到端可用性。 |

依据：`agent/ItineraryReviewAgent.java:33-69`、`controller/DebugAgentController.java:59-68,99-118,146-153`。`@Deprecated` 不等于没有 Bean，也不等于已禁用调试；但它确实不在 Master 的注册清单，Plan 也直接注册审核工具。

### 未实现的报销

`ReimbursementAgent.build()` 直接返回 `null`（`agent/ReimbursementAgent.java:19-23`）。原文和 Master 提示词存在报销条目，实际 Master 没有 `reimbursement_agent` 工具。它没有可交付的输入/输出/工具/状态/失败处理契约，标记为**未实现**，不创建 Python 调度项。

## 4. Python 当前状态与 007 验收

Python 目前有两个独立的 `GoGo` 构造位置，职责不同：

- [chat/executor.py](../../src/gogo_agent/chat/executor.py) 的 HTTP 对话 Agent 读取/保存 005 的 AgentState，未注册业务工具或子 Agent。
- [cli.py](../../src/gogo_agent/cli.py) 的 001 练习 Agent 只注册本地日期工具；它不是 HTTP 的主协调 Agent，也不是 InfoAgent。

当前 Python 没有 Master/子 Agent 权威调度表。本清单记录迁移对象，不把 Java 名称或未实现的占位变成可路由能力。后续普通业务子 Agent 候选严格对应四项真实注册；Info 作为第一个只读目标，Manage 的申请写入、Plan 的方案生成及 Booking 的真实下单按后续阶段实施后再启用。Review 保持审核职责的工具入口，报销继续未实现。

007 的静态验收项：

- [x] 六处 ReAct builder 与普通 Master + 四个子 Agent、历史 Review 的口径一致。
- [x] 四个 Master 工具名逐一对应 Bean/实例名称；每个常规 Agent 具备五栏说明。
- [x] 改写、识别、标题、推荐与 ReAct 业务 Agent 分开列出。
- [x] Info 被标为首个只读业务迁移目标；差旅申请、方案产物、供应商订单的副作用已区分。
- [x] Python 当前两个构造入口均无未实现的业务角色注册；本次没有添加占位调度。

本文的“已完成”指 007 职责盘点；模型选路、外部工具成功率、真实下单和多实例恢复均未运行验证。
