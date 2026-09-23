# 从零到一：用 Python 和 AgentScope 重建 GoGo Agent

这是一份**动手学习清单**，配合上一级的[长期路线图](../AgentScope-Python-迁移路线图.md)使用。章节严格跟随[原始 001–102 目录](/Volumes/PortableSSD/docs/tengjiaozhai.github.io/目录.md)。Java 项目是行为对照，Python 工作区是练习和最终后端；本清单是后续实现的任务，不代表已写好代码。

## 如何使用

1. 从 000 的基础练习开始；若已熟悉 Python，可做完小练习直接进入 001。之后按编号推进。每项按“理解 → 动手 → 验收”执行，验收通过再勾选。
2. 每次只完成一个编号或自然耦合的相邻编号。先在 Java 源码找实际入口，再在 Python 写最小对应实现；用固定输入、隔离数据库、模拟模型/供应商比较行为。把“源码已实现”和“文章建议”分开记录。
3. 阶段文档给的是**建议产物路径**，新文件按步骤需要才创建，不要求先造空目录。每项的测试应验证行为或权限边界，避免只测试函数存在。
4. 外部 API、真实订单、审批回调和生产数据不要作为学习练习的第一次验证对象。先用假数据和模拟服务；实际集成单独记录结果。
5. 每完成一项，在本清单或实施记录补四行：`Java 入口`、`Python 入口`、`验证命令与结果`、`差异/待办`。未通过时保留未勾选状态。

## 000：开始前的 Python 小练习

这部分不改变原目录编号，只补足从零学习所需的语言基础。

| 小练习 | 动手内容 | 完成标志 |
|---|---|---|
| 000-A 类型与数据 | 用 `dataclass` 或 Pydantic 定义 `TravelRequest(user_id, destination, start_at)`；写一个函数校验目的地和日期；练习 `None`、列表、字典、异常。 | 合法输入得到结构化对象，缺字段和错误日期有明确错误。 |
| 000-B 异步与并发 | 写两个模拟外部搜索的 `async def`，分别串行和 `asyncio.gather` 调用；用 `contextvars` 或显式参数传递请求 ID。 | 能解释 `await`、并发与串行的顺序；两个并发请求的用户信息不串。 |
| 000-C 测试与 HTTP | 用 `pytest` 测 000-A 的有效/无效输入；用最小 FastAPI 路由返回 JSON；用 HTTP 客户端访问。 | 本机可运行针对性测试、能看懂请求/响应、状态码和 JSON。 |

参考：[Python 官方教程](https://docs.python.org/3/tutorial/)与 [asyncio 文档](https://docs.python.org/3/library/asyncio.html)。只学到能完成上面三个练习即可，不必先通读语言手册。

## 学习顺序

| 阶段 | 清单 | 这一阶段结束时你能做什么 |
|---|---|---|
| A | [001–005 启动、认证、会话](阶段A-001-005.md) | 从零运行一个 AgentScope 2.x Agent，并在 Python 后端保存一段登录后的对话。 |
| B | [006–022 多智能体与意图](阶段B-006-022.md) | 写出规则/检索/模型识别流水线，解释一次请求如何到达正确 Agent。 |
| C | [023–028 Master 与身份](阶段C-023-028.md) | 建立主 Agent、只读子 Agent 与可靠的用户身份传递。 |
| D | [029–041 差旅管理与工具](阶段D-029-041.md) | 完成差旅单、政策、审批等确定性业务工具。 |
| E | [042–060 规划、审核、外部能力](阶段E-042-060.md) | 产生可复查的行程方案，并接入受控外部查询和审核。 |
| F | [061–067 预订与可靠性](阶段F-061-067.md) | 在模拟供应商中完成确认、下单、取消、重试与防重复。 |
| G | [068–085 提示词与上下文](阶段G-068-085.md) | 用回归样例控制结构化输出、记忆与上下文成本。 |
| H | [086–094 运行保障](阶段H-086-094.md) | 验证中断恢复、权限隔离、日志与执行安全。 |
| I | [095–102 对接与部署](阶段I-095-102.md) | 复刻 SSE/HITL 契约，直接复制前端完成端到端运行。 |

## AgentScope 版本规则

本计划学习 **AgentScope Python 2.x**。官方明确说明 2.x 与 1.x 有破坏性差异；当前 Python 项目要求 `>=3.14`，其 `.venv` 是 3.14.0，且还未装 AgentScope。第 001 项要先做实际安装与最小调用，固定 AgentScope、Python 和依赖版本。后续只参照**锁定版本**的[AgentScope 官方文档](https://docs.agentscope.io/)与本地 API。不要把旧版 `doc.agentscope.io/tutorial/` 的 `ReActAgent`/`formatter` 示例混进 2.x 实现。

第 001 项建议先写一个终端中的 `Agent` + `FunctionTool` 最小例子，之后才接 HTTP、数据库与多 Agent。AgentScope 2.x 官方[项目首页](https://github.com/agentscope-ai/agentscope)和[Agent 文档](https://docs.agentscope.io/latest/en/building-blocks/agent/overview)提供入口；官方示例的 Bash/文件工具只是演示，GoGo 差旅 Agent 不应因此获得无关的本地命令权限。
