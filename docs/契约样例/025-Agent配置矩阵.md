# 025 Agent 配置矩阵：Java 基线与 Python 当前实现

> 核对日期：2026-10-05。Java 路径以 `/Users/shenmingjie/workSpace/LLMentor-teach/gogo-agent/src/main/java/com/gogo/travel/` 为根；Python 以本仓库源码和锁定的 AgentScope Python 2.0.8 为准。表中的“已注册”指运行时 Toolkit，不能只凭 Bean、提示词或意图枚举判断。

## Java：六个 ReAct 构造与实际注册

`src/main/java` 中有六处 `ReActAgent.builder()`：Master、Manage、Plan、Info、Booking、Review。Master 的 Toolkit **只注册 Manage、Plan、Info、Booking 四个子 Agent**及 `UserInteractionTools`；Review 是带 `@Deprecated` 的独立 Bean，可由调试入口构造，报销 Agent 的 `build()` 返回 `null`。六个 ReAct 构造均未声明强类型 structured-output schema，最终回复是 `Msg` 文本与工具事件；不能把提示词要求的 JSON 当作框架校验。依据：`agent/MasterAgent.java:79-149`、`agent/ItineraryReviewAgent.java:33-69`、`agent/ReimbursementAgent.java:19-23`。

| Agent / Master 注册 | 模型与提示词 | Toolkit、知识与业务写入能力 | 状态和输出 | 轮次 / 工具超时 | 源码 |
| --- | --- | --- | --- | --- | --- |
| `MasterAgent` / 主入口 | `strongModel` (`qwen3.7-max`)；`master-agent-system.md` | `ask_user` 加四个子 Agent 工具；自身不直接写差旅数据，**能委派**有写能力的角色 | AutoContext、按用户长期记忆、Session 持久化与待续工具恢复；完成时汇总为 `Msg`，`ask_user` 可挂起 | 15 轮；15 分钟，最多 3 次尝试 | `agent/MasterAgent.java:79-149` |
| `ItineraryManageAgent` / `itinerary_manage_agent` | `strongModel`；`itinerary-manage-agent-system.md` | 差旅单与用户信息读写、查询和冲突检查；可提交、修改、取消差旅申请 | AutoContext、Session 和 active-agent 持久化；办理结果为 `Msg` | 10 轮；1 分钟，最多 3 次尝试 | `agent/ItineraryManageAgent.java:37-73` |
| `ItineraryPlanAgent` / `itinerary_plan_agent` | `strongModelWithThinking` (`qwen3.7-plus`)；`itinerary-plan-agent-system.md` | 政策/差旅单/用户资料读取，候选搜索、规划、方案、HTML、审核工具及受限 MCP；还注册用户信息写工具和可读写 Shell。无 `BookingWriteTools`，**整个 Agent 仍不能称为纯只读** | AutoContext、按用户长期记忆、Session 和 active-agent 持久化；方案及回复为 `Msg` | 30 轮；3 分钟，最多 3 次尝试 | `agent/ItineraryPlanAgent.java:41-134` |
| `InfoAgent` / `info_agent` | `stableModel` (`glm-5.2`)；`info-agent-system.md` | `PolicyTools`、目的地查询、天气/签证 MCP 白名单和三个 Knowledge；无差旅单或预订写工具 | AutoContext、Session 持久化；查询回复为 `Msg` | 5 轮；1 分钟，最多 3 次尝试 | `agent/InfoAgent.java:27-77` |
| `BookingAgent` / `booking_agent` | `strongModelWithThinking`；`booking-agent-system.md` | 方案/差旅单读取、`BookingWriteTools`、用户信息写工具与供应商 CLI；可执行预订和取消 | AutoContext、按用户长期记忆、Session 和 active-agent 持久化；预订执行回复为 `Msg` | 10 轮；1 分钟，最多 3 次尝试 | `agent/BookingAgent.java:45-116` |
| `ItineraryReviewAgent` / **未注册** | `stableModel`；`itinerary-review-agent-system.md` | 政策、目的地与 `ItineraryReviewTools`；可读取规划结果生成审核意见，无预订写工具 | `@Deprecated`；AutoContext 与长期记忆，**未挂 Session 持久化 Hook**；审核回复为 `Msg` | 8 轮；1 分钟，最多 3 次尝试 | `agent/ItineraryReviewAgent.java:33-69` |

`strongModel`、`strongModelWithThinking`、`stableModel` 的实际模型名来自 `agent/config/ModelConfig.java:36-68`。Manage、Info、Booking、Review 的 1 分钟工具配置来自 `agent/BaseSubAgent.java:192-202`：最多 3 次尝试、初始退避 3 秒且只对 `IOException` 重试；Plan 覆盖为 3 分钟。Master 自行配置 15 分钟/3 次，不能套用子 Agent 的退避规则。Plan、Booking 显式启用并行工具；Master、Info 的构建代码没有显式开启并行。五个常规 Agent 的 `SessionPersistenceHook` 以 `sessionId:agentName` 存取状态；Review 未挂该 Hook。见[007 职责清单](../架构/007-Agent职责清单.md)。

## Python：当前实际运行入口

| 角色 / 入口 | 模型与提示词 | 工具、权限及写入 | 状态与输出 | 轮次 / 超时 | 源码 |
| --- | --- | --- | --- | --- | --- |
| HTTP `GoGo` 主协调 Agent | `GOGO_MODEL_NAME` 指定的 OpenAI 兼容 `ChatModel`；`MASTER_SYSTEM_PROMPT` 注入本轮有序意图 | 仅当意图含 `GENERAL_INFO` 时注册只读 `info_agent`，权限 `ALLOW` 且不并发；没有业务写工具，但会保存聊天回复与 AgentState | 认证用户+会话组成状态键，跨轮加载/保存 `GoGo` `AgentState`；文本经 JSON/SSE 返回，助手消息 `extra` 记录识别与实际工具调用 | 默认 15 轮；模型 HTTP 60 秒、两层自动重试 0；`info_agent` 调用超时 60 秒 | [`chat/config.py`](../../src/gogo_agent/chat/config.py)、[`chat/executor.py`](../../src/gogo_agent/chat/executor.py) |
| HTTP `InfoAgent` 子 Agent | `GOGO_STABLE_MODEL_NAME` 的稳定档（默认 `glm-5.2`），与 GoGo 共用网关和密钥但独立选模型名；`INFO_SYSTEM_PROMPT` 限定普通公共信息 | 无 Toolkit、知识库、差旅单或订单工具；只向主 Agent 返回文本 | 每次调用新建内存 `AgentState`，不在子 Agent 仓储中跨轮保存；回复交给 `GoGo` 汇总 | 默认 5 轮；模型 HTTP 60 秒，外层工具调用超时 60 秒；自动重试 0 | [`chat/config.py`](../../src/gogo_agent/chat/config.py)、[`chat/executor.py`](../../src/gogo_agent/chat/executor.py) |
| 001 终端日期 Agent | `GOGO_MODEL_*` 网关的 `DeepSeekChatModel(stream=False)`；提示词在 CLI 中要求调用日期工具 | 唯一 `get_current_date`，只读并 `ALLOW`；不在 HTTP 装配中 | 一次终端调用，无应用层 AgentState 仓储；返回回答与 `tool_used` | 未显式设置轮次、模型超时或重试，沿用锁定 SDK 默认 | [`cli.py`](../../src/gogo_agent/cli.py) |
| `QueryRewriter` | `GOGO_STABLE_MODEL_NAME` 的一次请求模型；`_REWRITE_INSTRUCTIONS`，temperature 0、max_tokens 2048 | 直接模型调用，`tools=None`，无业务写能力 | 从已鉴权业务消息构造最多十条历史；无独立 AgentState；输出校验为 `RewriteResult` | 每次至多一次生成；模型客户端 60 秒、重试 0 | [`intent/service.py`](../../src/gogo_agent/intent/service.py)、[`intent/context.py`](../../src/gogo_agent/intent/context.py) |
| `IntentRecognizer` | L1 规则、L2 Qdrant 向量，未命中才用稳定档模型与 `_INTENT_INSTRUCTIONS` 做 L3 | 无工具或业务写能力 | 无 AgentState；模型 JSON 解析为 `IntentResult`，对外组合成 `RecognitionDecision` | L3 至多一次生成；模型客户端 60 秒、重试 0 | [`intent/service.py`](../../src/gogo_agent/intent/service.py)、[`intent/runtime.py`](../../src/gogo_agent/intent/runtime.py) |

Java Master 对应当前 Python `GoGo` 的统一入口，Java Info 只对应 Python 当前无知识库的 `InfoAgent`；Manage、Plan、Booking、独立 Review 目前没有 Python 业务 Agent。`GoGo` 的 AgentState 在配置数据库时存 SQL，否则使用内存仓储；InfoAgent 的状态不进入这两种仓储。Python 的 L2 `OpenAIEmbeddingModel + QdrantStore` 是识别器依赖，不是单独 Agent。HTTP 的 `_FallbackMockModel` 只由测试与学习脚本显式注入；正式启动缺少模型配置会失败，不会静默退回模拟回答。因而 **Java Info 能查知识** 不代表 **Python Info 已具备知识库查询**。见 [`api.py`](../../src/gogo_agent/api.py)、[`chat/config.py`](../../src/gogo_agent/chat/config.py)、[`chat/repository.py`](../../src/gogo_agent/chat/repository.py)和[024 配置记录](024-Agent关键配置与失败出口.md)。

## 轻量能力：逐项对应

下表中的 Java 能力不属于六个 ReAct 构造，也不在 Master 的四个子 Agent 工具名单中；不能因为名称带 `Agent` 就给它配置独立工具循环或会话状态。Java 现状见 `agent/QueryRewritingAgent.java:24-86`、`agent/IntentRecognitionAgent.java:68-130`、`business/chat/service/ConversationTitleService.java:37-107`、`agent/service/QuestionRecommendationService.java:24-120`。

| Java 调用 | Python 当前对应 | 模型、提示词与输出 | 工具、状态、写入和时限 |
| --- | --- | --- | --- |
| `QueryRewritingAgent`（`AgentBase`） | `QueryRewriter`，独立一次模型改写 | Java `stableModel` 输出文本 `Msg`，流水线尝试提取 JSON；Python 以 `_REWRITE_INSTRUCTIONS` 调同一请求的聊天模型，校验为 `RewriteResult` | 两侧均无业务工具或独立 ReAct 会话；Python 读经归属校验的历史，模型客户端超时 60 秒且重试 0 |
| `IntentRecognitionAgent`（`AgentBase`） | `IntentRecognizer`，L1/L2 快筛与 L3 兜底 | Java 快路输出意图 JSON 文本、未命中才调用 `stableModel`；Python L3 用 `_INTENT_INSTRUCTIONS`，解析为 `IntentResult` 并组成 `RecognitionDecision` | 两侧不提供订单写工具或独立 AgentState；Python L3 至多一次生成、客户端超时 60 秒且重试 0 |
| `ConversationTitleService` | `ChatHistoryService.save_user_message()` | Java 用 `fastModel` 生成并清洗最多 24 字标题；Python 从首条非空用户消息直接截取最多 24 字，无标题模型 | 写入业务会话标题而非 AgentState；Java 后台模型等待 30 秒，Python 无模型超时步骤，见 [`chat/service.py`](../../src/gogo_agent/chat/service.py) |
| `QuestionRecommendationService` | **尚无 HTTP 对应实现** | Java 用 `fastModel` 从问题与回答生成最多四条推荐问题；Python 目前没有该输出或推荐事件 | Java 无独立记忆/业务写工具；生成或解析失败返回空列表，Python 不应凭规划文档把它标为已实现 |

## 三个容易混淆的权限边界

1. **信息查询与差旅单写入。** Java Info 的 Toolkit 是政策、目的地、MCP 白名单与知识库，缺少 `TravelOrderWriteTools`/`BookingWriteTools`；Python Info 当前没有任何业务工具。两者均不能靠模型说“已修改差旅单”来产生真实写入。Java `PolicyTools` 会更新本轮请求上下文的政策缓存，这不是差旅单写入。依据：`agent/InfoAgent.java:52-75`、`agent/tools/PolicyTools.java:57-69`。
2. **审核与预订。** Java Plan 注册 `ItineraryReviewTools.review_itinerary`，该工具读取已保存的 `plan_itinerary` 规划结果并返回审核报告；独立 ReviewAgent 也注册该审核工具，但没有 `BookingWriteTools`，且不在 Master 的四个 provider 中。审核工具不能下单；Plan 自身另有可写 Shell/用户信息工具，不能从“审核工具只读”推出“整个 Plan 纯只读”。Python 尚无 Review、Plan 或 Booking 角色。依据：`agent/ItineraryPlanAgent.java:58-79`、`agent/tools/ItineraryReviewTools.java:94-100`、`agent/ItineraryReviewAgent.java:43-68`。
3. **意图标签与可执行角色。** Java 的 `ReimbursementAgent.build()` 返回 `null`，Master 未注册报销工具；枚举/提示词出现 reimbursement 只是识别或路由声明。Python `IntentCategory.REIMBURSEMENT` 也可被识别，但不产生报销权限。依据：`agent/ReimbursementAgent.java:19-23`、`agent/MasterAgent.java:83-126`、[`intent/models.py`](../../src/gogo_agent/intent/models.py)。

## 验证范围

本项是源码配置盘点，没有新增运行时 Agent 或调度代码。用 `rg -n 'ReActAgent\.builder\(' /Users/shenmingjie/workSpace/LLMentor-teach/gogo-agent/src/main/java` 可核对六个 Java 构造入口；用 `rg -n 'subAgent\(' /Users/shenmingjie/workSpace/LLMentor-teach/gogo-agent/src/main/java/com/gogo/travel/agent/MasterAgent.java` 可核对四个真实注册。Python 工具可见范围和 JSON/SSE 调用由现有 [`test_023_master_agent.py`](../../tests/test_023_master_agent.py) 验收；轮次、超时和失败出口由 [`test_024_agent_config.py`](../../tests/test_024_agent_config.py) 验收。本文的 Java 结论来自静态源码，未启动 Java 服务；外部 MCP/供应商可用性未在 025 验证。
