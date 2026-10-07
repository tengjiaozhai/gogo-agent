# 027 活跃 InfoAgent 的可信续聊

## 先运行

在 Python 工程根目录运行；若没有 `.venv/bin/python`，先按[根 README](../../README.md#环境与启动)执行 `uv run --locked gogo-agent --health` 安装锁定依赖。本例不用模型密钥、数据库、Redis 或 Qdrant：

```sh
PYTHONPATH=src .venv/bin/python scripts/demo_027_active_info.py --case all
```

`--case` 可单独选 `continue`、`sse`、`switch`、`expired`、`cross_user`。每个场景都会先以 Alice 的 Token 执行一轮“请介绍一处景点”，使 GoGo 调用 InfoAgent 并产生活跃记录，然后才执行场景中的第二轮。`all` 为每个场景新建独立的内存账号、历史和 AgentState，不互相继承。预期 `continue`/`sse` 的 `pipeline=1`、模型角色依次为 `master, info, info`，最后回复 Agent 是 `InfoAgent`；`switch`/`expired` 的 `pipeline=2`、第二轮重新出现 `master`；`cross_user` 得到 403。演示输出里的固定会话名可预测，真实 HTTP 的请求 ID、追踪 ID、Token 和回复消息 ID 每轮会变化。

PyCharm 调试配置：Script path 填本仓库 `scripts/demo_027_active_info.py`，Working directory 填本仓库根目录，Interpreter 选 `.venv/bin/python`，Environment 填 `PYTHONPATH=src`（若 IDE 不解析相对路径，填绝对的本仓库 `src` 路径），Parameters 先填 `--case continue`。不应把 Java 基线目录设成 Python 工作目录。

## Java 与 Python 的责任对照

| Java 基线 | Python 实际入口 | 差异 |
| --- | --- | --- |
| `ChatController.chat()` 保存用户消息后读 `ActiveAgentSessionStore`，用 `ContinuationSignals.ALL` 整句匹配 | [`ChatAgentExecutor.execute_turn()` / `stream_turn_sse()`](../../src/gogo_agent/chat/executor.py) 保存经归属校验的消息、创建 `RequestContext`，再由 `choose_turn_entry()` 判断 | 两边都只接受完整续聊词，不对普通句子做包含匹配。Python 的 Router 已先用 Token 确定用户。 |
| Java `ActiveAgentSessionStore` 使用 `{sessionId}:router` 的 Session 项，无 TTL | [`AgentSessionStoreProtocol`](../../src/gogo_agent/chat/repository.py) 在 `agentscope_session` 的 `active_agent` 项保存 `agent_name/expires_at`，键同时包含认证用户和会话；内存仓储遵循相同语义 | Python 设置 30 分钟活跃有效期，过期回完整流水线。Java 的本地挂起 Agent 缓存 30 分钟与活跃路由记录不同。 |
| Java `ChatAgentExecutor.executeAgent()` 对已注册业务 Agent 直接 `agent.call`；`MasterAgent`/流水线 Agent 另有分支 | [`InfoAgentContinuation`](../../src/gogo_agent/chat/continuation.py) 目前只注册 `InfoAgent`，恢复其 AgentState 后调用 `Agent.reply()`，成功后刷新活跃记录 | Python 尚无 Manage/Plan/Booking Agent，不能把这些名字标为可续跑。Java 当前 `InfoAgent` 未挂活跃记录 Hook；Python 显式在成功委派后记录 Info，避免依赖“最后一个推理 Agent”的不稳定推断。 |

`RequestContext` 是冻结的请求身份快照，`InfoAgentContinuation` 每轮都用其 `user_id + session_id` 定位状态。`await` 会等待本轮模型/工具完成；它与 Java Reactor `Mono` 的订阅时机不同。示例的 `@asynccontextmanager` 在 `yield` 前装配固定意图流水线，退出后关闭固定模型；生产工厂还管理真实 Qdrant、embedding 和聊天模型客户端。InfoAgent 续聊走其已保存的 AgentState，不加载 GoGo 的 AgentState，也不会打开意图流水线。

## 按真实顺序打断点

| 顺序与命中 | 断点位置 | 进入前看什么 | 单步后预期 |
| --- | --- | --- | --- |
| 首轮第 1 次 | `scripts/demo_027_active_info.py` 的 `run_case()`：`client.post(..."请介绍一处景点")` | `pipeline_calls=[]`，`model_roles=[]`，状态仓储无记录 | 进入 Python auth Router，再到 `_save_user_turn()`；`request.user_id="u001"`，`request.session_id` 为本场景 ID。 |
| 首轮第 1 次 | `ChatAgentExecutor._active_agent_for_turn()` 的 `get_active_agent(request)` | `load_active_agent(key)` 为 `None` | 进入 `_prepare_turn()`，`pipeline_calls` 增至 1；固定 L1 命中 `general_info`。 |
| 首轮第 1 次 | `ChatAgentExecutor._build_agent()` 内 `ask_info_agent` 的 `save_agent_state()` | 模型可见工具只有 `info_agent(question)`；闭包持有 Alice 的 `request` | 保存 `InfoAgent` 的 context；GoGo 汇总成功后，执行器保存 GoGo 状态并调用 `record_completed_agent()`，router 项记录 `InfoAgent` 与到期时间。 |
| 第二轮 `continue`/`sse` | `_active_agent_for_turn()` 的 `choose_turn_entry()` | 新 `request_id/trace_id`，同一个 `user_id/session_id`，消息完整等于“继续” | 返回 `CONTINUE_ACTIVE`；`_prepare_turn()` 不命中，`pipeline_calls` 保持 1。 |
| 第二轮 `continue`/`sse` | `InfoAgentContinuation.continue_turn()` 的 `load_agent_state()` 与 `agent.reply()` | 加载首轮 InfoAgent context，而非 GoGo context | 只新建 Info 模型；回复保存为 `agentName="InfoAgent"`。SSE 发一条完整 `message` 和 `message_id`。 |
| 第二轮 `switch`/`expired` | `InfoAgentContinuation.get_active_agent()`、`ChatAgentExecutor._active_agent_for_turn()` | `switch` 的原文不是续聊词；`expired` 的到期时间早于当前 UTC | 清理活跃记录并回 `_prepare_turn()`；`pipeline_calls=2`，GoGo 模型再次构造。 |
| 第二轮 `cross_user` | `ChatHistoryService.save_user_message()` 的已有会话归属检查 | Bob 的 Token 对应 `u002`，路径仍为 Alice 的 session ID | 先返回 403；不读取 Alice 的 active 记录，不执行模型。 |

预测练习：先不运行，写出 `continue`、`switch`、`expired` 各自的 `pipeline` 次数，以及 Bob 的请求是否会触发 `InfoAgentContinuation.get_active_agent()`；然后逐项运行并核对。答案：`1/2/2`，Bob 在保存用户消息时被 403 拦截，尚未进入活跃查询。`--case sse` 的第二轮先等待 Info 回复，再发完整消息事件；当前未实现逐 token 的子 Agent 续跑流。

本脚本的认证、归属校验、AgentScope 工具循环、状态保存和选择器为实际代码；模型输出、L1 意图结果以及存储后端由固定响应与内存实现替换。SQL 的活跃记录读写由 [`test_027_active_info.py`](../../tests/test_027_active_info.py) 的 SQLite 用例独立验收；正式多实例、业务子 Agent 与真实网关回复未在 027 演示中验证。
