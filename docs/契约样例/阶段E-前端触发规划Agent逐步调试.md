# 阶段 E：用聊天页、Scalar 和断点学会 042–060

本指南面向从 Java 转到 Python AgentScope 的工程师。阶段 D 那份[前端与 Scalar 逐步调试](阶段D-前端与Scalar逐步调试.md)教你在浏览器里驱动**差旅管理**；这一份只解决一件事：**在前端聊天页输入一句话，真的触发 `ItineraryPlanAgent`，并在断点里一步步看清它做了什么**。纯函数与算法级的断点（scoring、review）仍见[042–060 调试指南](阶段E-042-060-调试指南.md)，两份不重复。

## 先认清这条链路

```mermaid
flowchart LR
    UI[React 聊天页 5174] -->|POST /api/chat/{sessionId} SSE| CHAT[ChatAgentExecutor]
    CHAT --> INTENT["IntentPipeline<br/>L0/L1 规则 → L2 向量 → L3 模型"]
    INTENT -->|intents 含 itinerary_planning| MASTER[GoGo 主 Agent]
    MASTER -->|工具 itinerary_plan_agent| PLAN[ItineraryPlanAgent]
    PLAN --> T1[create_plan]
    PLAN --> T2[search_candidates]
    T2 --> SEARCH[TuniuPlanSearch] --> CLI[假途牛 CLI]
    PLAN --> T3[compute_plan]
    PLAN --> T4[review_plan]
    T4 --> REVIEW[客观六维 + 三个主观评估 + 仲裁]
    PLAN --> T5[get_plan]
```

四个必须先记住的事实：

1. **前端代码里没有任何规划专用调用**。浏览器只发一句自然语言，走的是通用聊天接口；判断“这是规划诉求”的是后端意图识别，不是前端。所以你在前端能做和不能做的，全由 `intent` 决定。
2. **L1 命中就够了**。`ITINERARY_PLANNING` 有正则规则（[rules.py:149-152](../../src/gogo_agent/intent/rules.py)），命中后直接进快速分支，不调改写、不调 L3。只有句子没命中规则时才需要改写与向量层。
3. **主 Agent 先看意图，再决定挂不挂工具**。`has_plan` 为真时才把 `itinerary_plan_agent` 注册进本轮 Toolkit（[executor.py:384-439](../../src/gogo_agent/chat/executor.py)）。模型是否真的调用它，仍由模型决定。
4. **规划过程在页面上可见，但不是“计划卡片”**。后端已通过 `chat/trace.py` 把 AgentScope 事件转成 SSE：本轮实测一次真实请求收到 `progress`（20 次）、`thinking`（16 次）、`message`、`message_id`；前端 `ChatWindow.tsx` 的 `progress`/`thinking` 分支会渲染执行时间轴。**`plan_update`（计划任务卡片）仍无服务端发送**。要判断规划是否真的跑了，看时间轴里的 `ItineraryPlanAgent` 节点与工具项、持久消息 `extra.timeline`/`tool_calls`，以及控制台 `role=plan` 日志。

## 0. 十分钟准备：新建阶段 E 浏览器练习库

阶段 D 的准备脚本不建 `user_api_key` 表，而规划搜索**必须有当前用户自己的途牛 Key**。所以这里用专门的脚本，一次把库、账号、假途牛命令和练习变量都准备好。

```sh
cd /Volumes/PortableSSD/workspace/gogo-agent
PYTHONPATH=src .venv/bin/python scripts/prepare_stage_e_browser_lab.py
```

脚本输出形如（路径每次不同）：

```text
已创建阶段 E 浏览器练习库：10 张表、3 个账号、alice 的练习用途牛 Key、假途牛命令。
练习库：/var/folders/.../gogo-stage-e-browser-xxxx/lab.sqlite
假途牛命令：/var/folders/.../gogo-stage-e-browser-xxxx/tuniu
练习变量文件（权限 600，密钥值不打印）：/var/folders/.../gogo-stage-e-browser-xxxx/lab.env
练习账号：alice / bob / admin；密码仅在此临时库中为 123456。
后端命令：
  source /var/folders/.../lab.env && .venv/bin/python -m uvicorn gogo_agent.api:app --host 127.0.0.1 --port 8081
前端命令：
  cd frontend && GOGO_API_PROXY_TARGET=http://127.0.0.1:8081 npm run dev -- --host 127.0.0.1 --port 5174
```

它做了什么、为什么必须这么做：

| 动作 | 原因 |
| --- | --- |
| 建 10 张表（含 `user_api_key`） | 阶段 D 的库只有 9 张表，规划搜索查 Key 会直接找不到表 |
| 账号 alice `u001/P6/北京`、bob `u002/P5/上海`、admin `u_001/P8` | 用来验证“只用本人 Key、不借用他人” |
| 给 alice 预置 `tuniu-cli` 的**假** Key | 假 Key 只喂给假 CLI，不会发往真实途牛 |
| 生成 `tuniu` 包装器（内部指向 `scripts/fake_tuniu_cli.py`，并开 `FAKE_TUNIU_ECHO_DATES=1`） | 让候选项的日期等于你请求的日期，否则样例是固定 3 月数据 |
| 生成 `lab.env` | `GOGO_SECRET_KEYS` 是长密钥，不适合手抄；写进文件权限 600，值不打印 |

**为什么非要 `GOGO_SECRET_KEYS`**：[search.py:93-104](../../src/gogo_agent/planning/search.py) 的 `build_tuniu_key_provider()` 在没有该变量、或数据库不可用时，一律返回 `UnconfiguredTuniuKeys`，所有用户都拿不到 Key。此时工具层 `search_candidates` 统一返回 `errorCode=SEARCH_FAILED`，message 里带“途牛 API Key 服务未配置”（[tools.py:114-118](../../src/gogo_agent/planning/tools.py)）；`TUNIU_KEY_SERVICE_UNCONFIGURED` 是**服务层** `PlanSearchError.code`，只在直接调用搜索服务时出现。这不是 bug，是“不静默降级”的设计。

> ⚠️ **本机必做的一步（与代理有关）**：`lab.env` 里已经写好 `NO_PROXY` / `no_proxy`。原因：当前 `httpx2` 解析不了 `NO_PROXY` 里的 IPv6 字面量 `[::1]`，会在**构造模型客户端**时就抛 `InvalidURL`，表现为聊天接口 503 `意图处理失败（InvalidURL）`。本仓库实测：保留含 `[::1]` 的原值时必失败，把它改成纯 IPv4 后正常。你自己配 PyCharm 环境变量时也要覆盖这一项。

`lab.env` 只包含练习相关变量，**模型网关配置仍从仓库 `.env` 读取**（`load_dotenv` 在 [runtime.py:40](../../src/gogo_agent/intent/runtime.py)）。不要把练习变量写回仓库 `.env`。

### 后端：一个不带 reload 的 PyCharm Debug 配置

- Run/Debug Configurations → 新增 Python；**Module name** 填 `uvicorn`。
- Parameters 填 `gogo_agent.api:app --host 127.0.0.1 --port 8081`。
- Interpreter 选本仓库 `.venv/bin/python`；Working directory 选本仓库根目录。
- Environment 依次加入：`PYTHONPATH=src`、`GOGO_DATABASE_URL=...`、`GOGO_REDIS_HOST=`（空值）、`GOGO_SECRET_KEYS=...`、`GOGO_TUNIU_CLI=...`、`NO_PROXY=localhost,127.0.0.1`、`no_proxy=localhost,127.0.0.1`。后两个密钥/路径的值请从 `lab.env` 复制，**不要贴进对话或笔记**。
- 以 **Debug** 启动。此配置不加 `--reload`，断点才停得住。若直接 Debug 仓库根 `main.py`，它默认 `reload=True`（[main.py:6](../../main.py)），断点会落在重载后的子进程里，需自行确认。

等价终端命令就是脚本打印的那条（`source .../lab.env && ...`）。

### 前端：另起 Vite 实例

```sh
cd /Volumes/PortableSSD/workspace/gogo-agent/frontend
npm ci
GOGO_API_PROXY_TARGET=http://127.0.0.1:8081 npm run dev -- --host 127.0.0.1 --port 5174
```

打开 `http://127.0.0.1:5174`，用 alice 登录。另开 `http://127.0.0.1:8081/scalar`。**不要和已经在跑的 8080/5173 混用**：那两个指向仓库 `.env` 的真实库，且没有练习 Key，规划搜索会失败——这本身也是个值得观察的正确失败。

### 浏览器开发者工具先开好

F12 → Network 勾 **Preserve log**、筛选 Fetch/XHR，然后聊天页发送消息，点 `POST /api/chat/{sessionId}`：

- Request Headers：`Accept: text/event-stream`。
- Response / EventStream：只会出现 `message`、`message_id`，可能还有 `suggestions`。
- `Application → Local Storage → gogo-auth` 是前端登录态，只观察，不复制 token 给 AI。

`sessionId` 形如 `session_<时间戳>`，由前端生成（[chatStore.ts:102](../../frontend/src/store/chatStore.ts)），**后端收到即懒创建会话**（[service.py:90-113](../../src/gogo_agent/chat/service.py)），所以不存在“先建会话”的接口。

## 1. 每一步统一记录四件事

发送前先写预测：**①意图与命中层；②主 Agent 是否挂上 `itinerary_plan_agent`；③规划 Agent 会调哪几个工具；④结果落在哪里（持久消息 `extra`、控制台日志、还是内存计划库）。** 发送后按这个顺序核对。

Java 类比：`Depends(get_current_user)` 相当于服务端认证拦截并注入可信身份；`FunctionTool` 相当于模型可见的 `@Tool`；`PlanProgress` 相当于把 Java `PlanNotebook` 的进度**显式序列化**出来（AgentScope 2.0.8 没有等价模块）；`plan_id` 相当于一次规划会话的句柄，模型只能拿它去查，不能自己拼计划内容。

## 2. 让 Plan Agent 真正被触发：用能命中规则的句子

先看结论（下表已用真实 `IntentRuleMatcher` 逐条跑过，不是猜的）：

| 聊天页输入 | L1 规则结果 | 能否进 Plan Agent |
| --- | --- | --- |
| 请帮我规划北京到上海 2026-11-10 出发、2026-11-12 返回的行程 | `itinerary_planning` | ✅ 稳定 |
| 帮我规划一下北京到上海的出差行程，11月10日出发，12日返回 | `itinerary_planning` | ✅ 稳定 |
| 我帮你把往返交通和住的地方整体捋成一个计划，并规划一下每天的安排 | `itinerary_planning` | ✅ 稳定 |
| 这次去成都三天，帮我把每天的日程都排明白 | 规则 **miss** | ⚠️ 要走改写 + 向量/模型，不稳定 |
| 去广州见两个客户，帮我把这两天的事情统筹一下 | 规则 **miss** | ⚠️ 同上 |
| 帮我把往返交通和住的地方整体捋成一个计划 | 规则 **miss** | ⚠️ 同上 |

规律很简单：**句子里要有 `规划`、`安排…行程`、`行程方案` 这类词**（[rules.py:148-152](../../src/gogo_agent/intent/rules.py) 的两条正则）。注意 `intent/seed.json` 里那几句样例是给 **L2 向量层**用的，它们本来就**不命中 L1**，别拿它们当“一定能触发”的证据。

**第一次务必带上城市 + 往返日期**。实测：只说“帮我规划一下北京到上海的行程”时，规划 Agent 会先返回“请补充去程/返程日期”，这是`create_plan` 的 `NEEDS_INPUT` 正常出口；条件齐全才会一路走到搜索、计算、审核。

## 3. 042–060 逐步断点：前端一次点击，后端六个观察点

统一操作：左侧「新建对话」→ 输入上表第一句完整条件 → 发送。下表按**真实执行顺序**排列。

| 步 | 断点位置 | 触发条件 | 进入前应有的状态 | 看什么与预期 | 下一步 |
| --- | --- | --- | --- | --- | --- |
| P1 | [rules.py:191](../../src/gogo_agent/intent/rules.py) `IntentRuleMatcher.match` 第一行 | 每次发消息 | `query.question` 是你的原句 | 继续执行后，返回的 `FastMatch.status` 为 `HIT`，`result.intents[0].intent`（即 `primary_intent`）为 `itinerary_planning` | 返回 pipeline |
| P2 | [pipeline.py:130](../../src/gogo_agent/intent/pipeline.py) `if fast.result is not None:` | P1 命中 | `fast.result` 非空 | 条件为真 → `branch == "fast"`，**不进入改写**；若为假，说明规则没中，你会去看 L2/L3 | 进 executor |
| P3 | [executor.py:285](../../src/gogo_agent/chat/executor.py) `has_plan = any(...)` | 主 Agent 装配 | `prepared.decision.result.intents` 含 `itinerary_planning` | `has_plan is True`；随后 `tools` 里出现 `itinerary_plan_agent`。**这是“前端这句话能调规划”的唯一开关** | 进模型 |
| P4 | [executor.py:392](../../src/gogo_agent/chat/executor.py) `child, review_model = self._build_plan_agent(request, trace=trace)` | 模型决定调工具 | `tool_calls` 里即将多一条 | `state_session_id` 形如 `4:u001:session_...`；`review_model` 不是空 | 进 P5 |
| P5 | [tools.py:83](../../src/gogo_agent/planning/tools.py) `def create_plan(...)` 第一行 | 规划 Agent 首次调工具 | 参数可能只有城市 | 缺日期时直接返回 `{"success": false, "errorCode": "NEEDS_INPUT"}`，**不会建计划**；齐全时才 `service.create` 得到 `plan_id` | 进 P6 |
| P6 | [tools.py:105](../../src/gogo_agent/planning/tools.py) `async def search_candidates(plan_id)` | 计划已建 | `service.get(user_id, plan_id)` 成功 | 走 `search_provider(user_id, plan.conditions)`；断在 [search.py:73](../../src/gogo_agent/planning/search.py) 可见 `api_key` **只属于当前用户**（`get_api_key(user_id)`） | 进假 CLI |
| P7 | [tuniu.py:167](../../src/gogo_agent/integrations/tuniu.py) `env={_API_KEY_ENV: api_key, ...}` | 调假途牛 | 已通过 `SkillRunner` 白名单 | Key 只经环境变量传入子进程，不进命令行文本；`secrets=(api_key,)` 保证异常文本被脱敏 | 回工具 |
| P8 | [tools.py:127](../../src/gogo_agent/planning/tools.py) `def compute_plan(...)` | 候选已保存 | `plan.candidates` 非空 | `scores_json` 是**模型给的偏好分**；金额、时间全部来自`compute_plan` 纯函数，模型不参与计算 | 进审核 |
| P9 | [service.py:135](../../src/gogo_agent/planning/service.py) `async def review(...)` | 模型请求审核 | `plan.result` 非空 | `plan_version=plan.content_version`：审核绑定内容版本，之后改内容会让它失效 | 进 P10 |
| P10 | [review.py:279](../../src/gogo_agent/planning/review.py) `subjective = list(await asyncio.gather(...))` | 三个主观评估器就绪 | `assessors` 长度 3 | 三个结果都在；异常的那个 `available == False`，不会被当成通过 | 仲裁 |
| P11 | [executor.py:413](../../src/gogo_agent/chat/executor.py) `record = {...}`（`finally` 块） | 工具返回前 | 本轮 `status` 已定 | 这条记录就是前端那份 `extra.tool_calls` 的来源：`tool_name=itinerary_plan_agent`、`agent_name=ItineraryPlanAgent`、`status=completed`、`trace_id` | 写库 |
| P12 | [executor.py:865](../../src/gogo_agent/chat/executor.py) `if plan_states:` | 本轮成功 | `plan_states` 里有一个 `AgentState` | 规划 Agent 的记忆按 `ItineraryPlanAgent` 名字单独存；**并不同步主 Agent 状态** | 结束 |

**观察提示**：`plan_id`、`plan.version`、`progress` 只存在进程内存（[store.py:73-81](../../src/gogo_agent/planning/store.py) 默认建内存 SQLite）。所以“刷新后计划还在不在”这个问题，答案是**不在**；重启后端也会丢。这不是你调试失败。

## 4. 用 Scalar 与数据库核对事实，而不是信 AI 的措辞

AI 回复里出现“已生成方案”“已审核通过”都只是文本。可核对的事实只有三处：

1. **持久消息 `extra`**：Scalar 用 alice token 调 `GET /api/chat/{sessionId}/messages`，找本轮 assistant 消息：

   ```json
   "extra": {
     "intent_pipeline": {"branch": "fast", "hit_layer": "rule", "intents": ["itinerary_planning"]},
     "tool_calls": [{"tool_name": "itinerary_plan_agent", "agent_name": "ItineraryPlanAgent", "status": "completed"}]
   }
   ```

   `tool_calls` **为空**只说明“模型这轮没调工具”，不说明链路坏了。想稳定复现工具调用，用离线脚本（第 6 节）。
2. **控制台日志**：Debug 时 PyCharm 控制台会逐行打印 `model_call role=... duration_ms=... input_tokens=...`。真实一轮规划长这样：

   ```text
   model_call role=master ...      ← 主 Agent 决定委派
   model_call role=plan   ... ×N   ← 规划 Agent 的 ReAct 循环
   model_call role=review ... ×3   ← 三个主观评估各一次（仲裁是确定性纯函数，不调模型）
   model_call role=master ...      ← 主 Agent 汇总回复
   ```

   `role` 白名单在 [executor.py:146](../../src/gogo_agent/chat/executor.py)，真正打日志的语句在 [model.py:26](../../src/gogo_agent/model.py)；出现 `role=plan` 就是“规划 Agent 真的在跑”的硬证据。
3. **跨用户验证**：换 Bob 登录（同一条消息），Scalar 用 Bob token 请求。Bob 没有途牛 Key，`search_candidates` 会返回 `SEARCH_FAILED ... 未配置可用的途牛 API Key`，并且**不会**借用 alice 的 Key。这是 [search.py:49-62](../../src/gogo_agent/planning/search.py) 的显式设计。

### 三个常见“看起来像失败、其实是对的”

| 现象 | 真实原因 |
| --- | --- |
| 回复“请补充去程/返程日期” | `create_plan` 的 `NEEDS_INPUT`，正常工作流 |
| 回复正常但 `tool_calls` 为空 | 模型这轮自己回答了，没委派；改句子或换离线脚本 |
| 503 `子 Agent 调用失败（RuntimeError）` + 日志 `exceeds the max iteration numbers 10` | 规划 Agent 的 ReAct 被 `max_iters=10` 用尽（[planning/agent.py:32](../../src/gogo_agent/planning/agent.py)），常发生在信息不全时反复试探。补齐城市与日期即可 |

## 5. 调参数、看边界

| 想验证 | 怎么做 | 预期 |
| --- | --- | --- |
| 规划超时 | 改 `GOGO_PLAN_TOOL_TIMEOUT_SECONDS`（默认 180s，[config.py:57](../../src/gogo_agent/chat/config.py)）到很小值 | 内部抛 `TimeoutError("行程规划 Agent 工具调用超时")`，但**客户端拿到的是** 503 `子 Agent 调用失败（TimeoutError）`（[executor.py:593](../../src/gogo_agent/chat/executor.py) 统一改写） |
| 模型不调工具 | 把 `GOGO_MASTER_MAX_ITERS` 调成 1 | 主 Agent 没机会发起委派，`tool_calls` 为空 |
| 规划记忆是否独立 | 同一会话里先规划一次，再问“继续”——对比 `_active_agent_for_turn` 的返回 | 规划**不是**活跃续跑目标（只有 InfoAgent 注册了续跑），所以“继续”会重新走完整流水线 |
| 计划是否持久 | 规划成功后 Ctrl+C 重启练习后端，再问“之前的方案呢” | 找不到：计划在内存里 |

## 6. 页面之外：稳定复现工具调用

真实模型的工具选择有随机性，做**代码级验收**时用它：

```sh
PYTHONPATH=src .venv/bin/python scripts/demo_042_060_planning.py --case all
```

脚本不联网、不读数据库，输出的 `plan_id` 与版本号每次一致。把它当“确定性的第二意见”：浏览器里看到的现象与脚本不一致时，以脚本和测试为准。

规划工具的边界也在这里体现：`create_plan / search_candidates / compute_plan / get_plan / review_plan` 五个（[tools.py:169](../../src/gogo_agent/planning/tools.py)），**没有**下单、提交差旅单、取消。`tests/test_043_planning_chat.py` 用断言把这条边界钉死。

## 7. 如何判断自己真正学会了

关掉代码，逐题先写预测，再用浏览器 / Scalar / 断点解释原因：

1. 前端代码里搜 `plan`，为什么找不到任何“调用规划接口”的地方？**因为前端只发自然语言，规划是后端根据意图挂的工具。**
2. 同一句“这次去成都三天，帮我把每天的日程都排明白”，在 5174 上一定进 Plan Agent 吗？**不一定。** 它不命中 L1，要靠改写+向量/模型，属不稳定路径；换第 2 节的句子才稳。
3. 主 Agent 这轮挂了哪些工具，由谁决定？**由 `prepared.decision.result.intents` 决定**，见 P3；模型只能在给定工具里选。
4. SSE 里没有 `plan_update`，说明规划没执行吗？**不能这样推断。** 服务端已增加 `thinking/progress` 展示实际 Agent 与工具执行，但尚未发送计划任务卡片事件。可查看页面委派和工具项、历史 `extra.timeline/tool_calls` 与控制台 `role=plan` 日志，见[时间轴调试指南](执行过程时间轴-调试指南.md)。
5. Bob 能不能用 alice 的计划或 alice 的途牛 Key？**都不能。** 计划按 `user_id` 隔离（跨用户返回 `PLAN_NOT_FOUND`），Key 按用户解析且不回退。
6. 规划成功后重启后端，计划还在吗？**不在。** 计划只存在进程内存。

能不看代码解释每项的 **入口 → 意图命中层 → 工具装配 → 子 Agent 执行 → 可见证据 → 数据落点**，并亲自复现一个成功例和一个失败例（缺 Key 或 `NEEDS_INPUT`），才算过关。

## 8. 本指南未验证的部分

- **真实途牛**：全程用仓库内假 CLI（`scripts/fake_tuniu_cli.py`）。本机 PATH 上确实存在 `tuniu`（`tuniu-cli 1.1.4`），但本指南**未使用**它，也未配置真实 Key；Python 侧目前**没有**录入途牛 Key 的 HTTP 入口。
- **真实 MySQL**：练习库是临时 SQLite；`itinerary_plan` 表尚未进入 MySQL 迁移脚本。
- **多实例**：计划在进程内存中，多进程之间不共享。
- **前端规划卡片**：`progress`/`thinking` 已由 `chat/trace.py` 发送并渲染为执行时间轴；但 `plan_update`（计划任务卡片）仍无服务端发送，因此页面**不会出现“计划任务清单”卡片**。时间轴形态见[执行过程时间轴调试指南](执行过程时间轴-调试指南.md)。
- **代理变量适配**：`InvalidURL` 的结论来自本机 `httpx2` 实测；换机器/换版本需重新确认。
