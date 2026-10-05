# 024 Agent 关键配置与失败出口

## 当前配置入口

正式 HTTP 服务在 [`api.py`](../../src/gogo_agent/api.py) 的 lifespan 中读取模型网关和 Agent 参数，缺少 `GOGO_MODEL_API_KEY`、`GOGO_MODEL_NAME` 或 `GOGO_MODEL_BASE_URL` 时启动失败；显式设置空白 `GOGO_STABLE_MODEL_NAME` 也失败。这一步只校验配置，不请求模型或 Qdrant。001 的 `gogo-agent --health` 仍可在无凭证环境检查安装。聊天主/子 Agent 共享 [`chat/config.py`](../../src/gogo_agent/chat/config.py) 的提示词与运行参数；模型网关沿用 017 的 `load_intent_runtime_settings()`，026 起按主档和稳定档选模型名。

| 参数 | 默认或规则 | 运行位置 |
| --- | --- | --- |
| `GOGO_MODEL_API_KEY`、`GOGO_MODEL_NAME`、`GOGO_MODEL_BASE_URL` | 正式 API 必填；报错只列变量名，不输出密钥 | 启动校验、模型工厂、意图流水线 |
| `GOGO_STABLE_MODEL_NAME` | 默认 `glm-5.2`；显式空白值无效 | 改写、L3 意图识别和 InfoAgent |
| `GOGO_MASTER_MAX_ITERS` | 15，正整数；对应 Java Master 的 15 轮 | `GoGo` 的 `ReActConfig.max_iters` |
| `GOGO_INFO_MAX_ITERS` | 5，正整数；对应 Java Info 的 5 轮 | `InfoAgent` 的 `ReActConfig.max_iters` |
| `GOGO_INFO_TOOL_TIMEOUT_SECONDS` | 60，有限正数；对应 Java Info 的单次工具 1 分钟 | `info_agent` 工具闭包的 `asyncio.timeout` |
| `GOGO_AGENT_MODEL_TIMEOUT_SECONDS` | 60，有限正数 | 主/子 Agent 的模型 HTTP 客户端 |

主 Agent 和 InfoAgent 显式关闭 AgentScope 层与模型客户端层的自动重试。当前只有一个只读 `info_agent` 工具，它明确允许调用且标记为不并发安全；AgentScope 2.0.8 的 `Toolkit` 没有通用工具超时或并行开关，因此超时放在应用工具闭包。Java Master 的工具配置为 15 分钟、最多 3 次尝试；Java Info 的工具配置为 1 分钟、最多 3 次尝试且只对 `IOException` 重试。Python 023 的 InfoAgent 尚无网络/业务工具，024 不把 Java 的重试复制到这个只读代理调用中。相关 Java 位置：`MasterAgent.java:128-149`、`InfoAgent.java:61-76`、`BaseSubAgent.java:192-202`。

## 失败和状态

| 触发 | JSON / SSE 可见结果 | 保存行为 |
| --- | --- | --- |
| 启动缺模型配置或 Agent 参数非法 | ASGI 启动失败，错误只指出配置名 | 尚未接收用户消息 |
| 主模型调用失败 | JSON 503 / SSE `event: error` | 已保存的用户消息保留，不保存成功助手回复或新 AgentState |
| `info_agent` 超时或其他失败 | JSON 503 / SSE `event: error`，可观察错误类型 | 不把主模型随后产生的文本误存为成功回复 |
| 主 Agent 达到最大推理轮次 | JSON 503 / SSE `event: error` | 不保存成功助手回复或新 AgentState |
| 请求在 Agent 推理中取消 | 取消向上传播；SSE 无完成事件 | 关闭模型客户端，不保存成功助手回复或新 AgentState |

AgentScope 2.0.8 到达 `max_iters` 后可能再做一次强制最终回答调用；因此 `max_iters=1` 的固定模型测试会观察到两次模型调用。应用检查最终消息的 `finished_reason=exceed_max_iters`，不把兜底文本当作完成。当前 `GoGo` 的 AgentState 继续按可信用户/会话键持久化，`InfoAgent` 每次工具调用新建状态并在返回后释放模型；跨轮子 Agent 记忆尚未启用。

## 可运行验收

```bash
.venv/bin/python scripts/demo_024_agent_config.py
.venv/bin/python -m pytest -q tests/test_024_agent_config.py
```

脚本依次展示环境变量改变轮次、缺配置启动失败、信息工具超时和主 Agent 轮次耗尽。可在 `load_chat_agent_settings`、`ChatAgentExecutor._build_agent`、闭包 `ask_info_agent` 和 `_completion_error` 打断点。脚本使用真实 FastAPI 启动校验、AgentScope Agent/Toolkit 和执行器状态边界；意图命中、主/子模型响应及信息内容来自固定替身，不连接真实网关。相关测试还覆盖两种 HTTP 输出、非法参数、模型重试配置与取消后不保存状态；原有 `tests/test_016_017_pipeline.py` 覆盖模型调用错误出口。
