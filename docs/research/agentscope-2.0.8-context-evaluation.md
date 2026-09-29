# AgentScope 2.0.8 上下文能力与 GoGo Agent 的取舍

> 调研日期：2026-09-29。范围是本仓库锁定并安装的 **AgentScope Python 2.0.8**、当前工作树的 020/021 实现，以及尚未实施的 022/023；不把 AgentScope Java 或 Python `main` 分支的接口当作本项目现有能力。版本见 [`pyproject.toml`](../../pyproject.toml)。

## 结论

**保留应用层 `RequestContext` 和意图改写的受限历史构造；继续使用已经接入的 AgentScope `AgentState` 与 Agent 内置上下文压缩。现在不做整体替换。** 三者解决的问题不同。真正的子 Agent 与业务工具尚未接入 HTTP，不能把假工具的 021 验收视为框架自动完成了身份传播。[021 学习要求](../学习路线/阶段B-006-022.md)与[023 学习要求](../学习路线/阶段C-023-028.md)也把可信身份及真实工具验收分别列明。

| 上下文 | 当前运行路径 | AgentScope Python 2.0.8 的对应能力 | 判断 |
| --- | --- | --- | --- |
| 可信请求身份、会话、消息 ID、追踪 ID | Token 解析后由服务端创建冻结的 [`RequestContext`](../../src/gogo_agent/request_context.py)，再显式传给流水线；消息不能改写身份 | [`Agent.reply()`](https://github.com/agentscope-ai/agentscope/blob/v2.0.8/src/agentscope/agent/_agent.py)没有调用级 `RuntimeContext` 参数；[`AgentState`](https://github.com/agentscope-ai/agentscope/blob/v2.0.8/src/agentscope/state/_state.py)也没有认证 `user_id` 字段 | 保留显式参数。权限判断仍由服务端和仓储执行，`RequestContext` 本身只传递已校验的值 |
| 改写与意图识别使用的近期业务历史 | [`RewriteContextBuilder`](../../src/gogo_agent/intent/context.py)在会话归属校验后按本轮消息 ID 取快照，最多保留此前 10 条、合计 8000 字符 | Agent 的压缩针对其自己的模型对话状态；独立的改写模型调用并不经过 Agent 对话循环 | 保留此快照构造。只有实际测出误路由、超限或成本问题时再比较裁剪策略 |
| `GoGo` 的推理历史与恢复 | [`ChatAgentExecutor`](../../src/gogo_agent/chat/executor.py)加载 `AgentState`，调用 `Agent.reply()`，随后由[`会话仓储`](../../src/gogo_agent/chat/repository.py)保存状态 | [`AgentState.context`/`summary`](https://github.com/agentscope-ai/agentscope/blob/v2.0.8/src/agentscope/state/_state.py)及 [`ContextConfig`](https://github.com/agentscope-ai/agentscope/blob/v2.0.8/src/agentscope/agent/_config.py)已提供历史和压缩 | 这部分已经采用框架；没有第二套自制 Agent 推理记忆需要迁移 |

## 证据与边界

1. **项目实际链路。** [`chat/router.py`](../../src/gogo_agent/chat/router.py)把认证用户交给执行器；执行器先保存用户消息，再创建 `RequestContext`，随后选择续跑或意图流水线。改写历史来自业务消息仓储，`GoGo` 的模型历史来自另存的 `AgentState`。这是两个用途不同的读取路径，不宜合成一个可被模型修改的消息对象。相关位置：`src/gogo_agent/chat/executor.py:161-168,255-295`、`src/gogo_agent/intent/context.py:28-75`。
2. **框架压缩已经在 Agent 调用链中。** 2.0.8 的 `Agent` 默认创建 `ContextConfig`；推理前调用 `compress_context()`，达到模型上下文阈值时生成摘要，并保留最近的未压缩消息。默认阈值为模型上下文的 80%。这处理的是 token 窗口；摘要内容不能作为身份、审批或计划归属的授权凭据。[源码：Agent](https://github.com/agentscope-ai/agentscope/blob/v2.0.8/src/agentscope/agent/_agent.py)、[源码：配置](https://github.com/agentscope-ai/agentscope/blob/v2.0.8/src/agentscope/agent/_config.py)、[v2.0.8 发布记录](https://github.com/agentscope-ai/agentscope/releases/tag/v2.0.8)。本次未用长会话触发压缩，尚未评估其对 GoGo 业务事实保留的效果。
3. **工具注入不等于认证传播。** [`Toolkit.call_tool()`](https://github.com/agentscope-ai/agentscope/blob/v2.0.8/src/agentscope/tool/_toolkit.py)只在本地工具声明需要状态注入时传 `_agent_state`，且排除 MCP 与外部工具；该状态没有可信用户字段。AgentScope App 层的工具工厂另以 `(user_id, agent_id, session_id)` 显式收参，但其示例认证依赖声明为临时方案的 `X-User-ID` 请求头，不能替代本项目的 Token 校验。[源码：工具工厂](https://github.com/agentscope-ai/agentscope/blob/v2.0.8/src/agentscope/app/_types.py)、[源码：示例身份依赖](https://github.com/agentscope-ai/agentscope/blob/v2.0.8/src/agentscope/app/deps.py)。
4. **别跨语言套 API。** AgentScope [Java 2.x 的 `RuntimeContext`](https://github.com/agentscope-ai/agentscope-java/blob/main/docs/v2/en/docs/building-blocks/context.md)确实能在 `agent.call(msgs, ctx)` 时携带调用级元数据；本项目运行的是 AgentScope **Python** 2.0.8，其 `Agent.reply()` 签名不同。未版本化旧教程里的 `ReActAgent`、`MemoryBase`、`JSONSession` 也不能用来推断锁定版本的接口。Python 2.0.8 安装包中未检出同名 `RuntimeContext` 实现。

## 后续采用条件

- **022：** 沿用 `RequestContext` 显式传递用户、会话和资源引用；如果日志追踪需要隐式传播，只把 `trace_id` 放入 `contextvars`，测试两个用户并发交错、后台任务和清理时点。阶段出口要求“不串用户”，框架压缩不能代替这个测试。[阶段 B 022](../学习路线/阶段B-006-022.md)
- **023：** 真正接入 Master、子 Agent 与业务工具时，在每条工具入口显式取得服务端上下文，并由仓储按认证用户校验资源归属；测伪造 `userId`、跨用户会话和子步骤追踪。`AgentState` 只负责 Agent 可恢复的推理状态。[阶段 C 023](../学习路线/阶段C-023-028.md)
- **长对话优化：** 等真实长会话数据出现，再对比现有改写快照、AgentScope 默认压缩和必要的配置调整，至少测意图正确率、关键业务事实保留、token、延迟与成本。压缩摘要不能进入权限判定。[阶段 G 上下文成本控制](../AgentScope-Python-迁移路线图.md)

## 验证与未验证项

- 本次核对了锁定安装包与 [v2.0.8 标签源码](https://github.com/agentscope-ai/agentscope/tree/v2.0.8)，并运行 `.venv/bin/python -m pytest -q tests/test_021_request_context.py tests/test_020_execution_order.py`：**18 passed**。该结果验证当前 020/021 的定向回归，不证明真实 Master/业务工具已实现。
- 未运行长会话压缩、真实业务工具、实际数据库重启或双用户异步压力验证。AgentScope App 层的会话存储和本项目 `agentscope_session` 的迁移收益、格式兼容性尚无实测依据，不建议在 022 顺手替换。
