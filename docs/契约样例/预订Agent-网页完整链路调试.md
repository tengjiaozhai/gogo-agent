# BookingAgent 网页完整链路：确认、门禁、模拟执行与结果回放

[阶段 F](../学习路线/阶段F-061-067.md) · [完整链脚本](../../scripts/demo_booking_chat.py) · [原组件练习](阶段F-061-067-调试指南.md)

当前已接通：网页聊天 → GoGo 的 `booking_agent` → BookingAgent → 服务端待确认摘要 → 网页显式确认 → 门禁复验 → 唯一 `BookingService.book` → 模拟供应商 → 结果卡与历史恢复。真实供应商、支付和供应商取消未接入本链。每个去程、返程和酒店分别确认，不会因为确认一项而购买其他项目。

## 1. 零准备运行

解释器为仓库 `.venv/bin/python`（Python 3.12.13），AgentScope 2.0.8；Node 使用本机运行时管理器的 24.x。先在仓库根准备依赖，再运行离线命令：

```bash
cd /Volumes/PortableSSD/workspace/gogo-agent
uv sync --frozen
GOGO_REDIS_HOST= GOGO_DATABASE_URL= PYTHONPATH=src .venv/bin/python scripts/demo_booking_chat.py
GOGO_REDIS_HOST= GOGO_DATABASE_URL= PYTHONPATH=src .venv/bin/python scripts/demo_booking_chat.py --scenario reject
GOGO_REDIS_HOST= GOGO_DATABASE_URL= PYTHONPATH=src .venv/bin/python scripts/demo_booking_chat.py --scenario lost_after_commit
GOGO_REDIS_HOST= GOGO_DATABASE_URL= PYTHONPATH=src .venv/bin/python scripts/demo_booking_chat.py --scenario lost_before_commit
```

这些脚本不调用真实模型、途牛、Redis 或 `.env` 业务数据库。每次在系统临时目录建立独立 SQLite 文件，以支持并发连接；普通命令结束后清理。脚本保留真实认证、HTTP、AgentScope 工具执行、签名、业务门禁、SQL 和回复事务。模型响应与供应商是替身。

每次开始会先做以下**隐藏准备**，不调用模型：

1. 建隔离 SQL 表，准备 Alice 的 P6 档案。
2. 提交一笔北京→上海、2026-11-10 至 2026-11-12 的差旅申请，由本地管理员审批通过。
3. 创建关联该单及 `booking-demo` 会话的计划，挂固定候选、计算方案、执行真实客观审核与固定主观评估。
4. 保存两条预置业务历史，让网页可以打开“模拟预订练习”；这不是以前真实聊天的回放。

默认命令再实际运行三轮 HTTP：请求摘要、结构化确认、原确认重放。GoGo 与 BookingAgent 都由真实 SDK 运行，单轮各两个固定模型响应（委派/汇总、工具/回复），没有真实模型请求。

| scenario | 首次实际执行结果 | 原确认重放 | 稳定断言 |
| --- | --- | --- | --- |
| `ok` | CONFIRMED | 已有确认结果 | 供应商创建调用 1，本地只有一条 |
| `reject` | FAILED | 已拒绝，不重新创建 | 调用 1，页面“预订未完成” |
| `lost_after_commit` | 查询确认供应商已有单，CONFIRMED | 已有确认结果 | 调用 1，没有补下一单 |
| `lost_before_commit` | UNKNOWN | 只读对账仍 UNKNOWN | 调用 1，保持待核实，不说成功 |

计划、审批、消息和预订 ID 每次不同；不要按具体 UUID 验收。金额为 `640.00 CNY`（本例酒店两晚），确认前创建调用 0、确认后 1、重放仍 1 是稳定观察点。

## 2. 浏览器与 IDE 路线

终端一：

```bash
GOGO_REDIS_HOST= GOGO_DATABASE_URL= PYTHONPATH=src .venv/bin/python scripts/demo_booking_chat.py --serve --port 8006
```

终端二：

```bash
cd /Volumes/PortableSSD/workspace/gogo-agent/frontend
GOGO_API_PROXY_TARGET=http://127.0.0.1:8006 npm run dev -- --host 127.0.0.1 --port 5178 --strictPort
```

打开 `http://127.0.0.1:5178/`，用公开练习账号 `alice / 123456` 登录，不要保存到浏览器密码管理器。打开“模拟预订练习”，发送“请预订这个方案的酒店”。

先观察 GoGo → 预订执行智能体 → `prepare_booking`，再核对卡片中的城市、日期、方案、金额和模拟标识。**卡片只在收到保存成功的 `message_id` 后可点击。** 点击“确认模拟预订”，观察第二轮的 `execute_confirmed_booking` 与结果卡；刷新页面，旧确认应显示“本项已处理”，结果仍在。浏览器前端只消费服务端结果，不把模型自然语言当订单事实。

PyCharm 新建 Python 配置：Script 为 `scripts/demo_booking_chat.py` 的绝对路径，Working directory 为仓库根，Interpreter 为 `.venv/bin/python`，Environment 填 `PYTHONPATH=src;GOGO_REDIS_HOST=;GOGO_DATABASE_URL=`。命令行练习参数为空或 `--scenario lost_before_commit`；浏览器断点参数为 `--serve --port 8006`，不要使用 `--reload`。修改后重启练习后端会清空临时库和登录态，须重新登录。

## 3. 输入、确认与保存的契约

初次消息仍走原接口：

```json
{"message":"请预订这个方案的酒店"}
```

网页确认仍向 `POST /api/chat/{sessionId}` 发送消息，但另带独立字段：

```json
{"message":"确认模拟预订：酒店…","booking_confirmation_token":"<已保存确认卡中的凭据>"}
```

`message`、模型的 `confirmed=true`、模型工具参数中的金额或用户 ID，都不能授权本次预订。认证用户来自原 Token；会话先校验归属；确认凭据必须能在该用户该会话的已保存助手回复中找到，并通过签名与有效期验证。密码学只证明摘要由服务端签发；执行授权来自认证客户端显式提交的结构化动作，不声称能证明操作者物理点击了鼠标。

| 数据 | 所在位置 | 作用与边界 |
| --- | --- | --- |
| 签名确认卡 | `chat_message.extra.booking_confirmations` 与 `extra.timeline` | 绑定用户、会话、签发轮次、计划内容版本、方案、行程段、金额、城市和时间；10 分钟有效 |
| 模型输入/AgentState | 用户文字和不含 token 的工具摘要 | 模型拿不到确认凭据；没有本轮可信确认时不注册执行工具 |
| 请求作用域 `BookingChatTurn` | 当前请求内存 | 收集待确认摘要、已验证对象、真实执行结果及其他事项回复；不跨用户复用 |
| 业务审计 | `business_action_audit` | 与本地状态同事务追加 supplier_booking 事件；待处理、未知、成功/失败有稳定键，重复查询不增加相同事件，不保存 token |
| 业务预订 | `booking_record` | 在调用供应商前先留待处理记录，写入所属差旅单与会话；幂等业务键决定同一项只创建一次 |
| 助手回复和 AgentState | `reply_transaction` | 一起提交。确认卡、结果事件在提交成功后发送，最后 `message_id` 使客户端完成该轮 |
| 观察日志与时间轴 | `tool_calls`、脱敏执行时间轴 | 用于观察委派、工具参数及结果；不能代替业务记录或确认授权 |

预订写入先于助手回复保存，二者不是跨系统原子事务。供应商调用后，成功、明确拒绝或未知状态的本地状态/审计写入失败时，该事务回滚，保留待处理记录并返回 UNKNOWN；恢复后同键只读对账并补全终态，不再次创建。若预订完成后回复保存失败，页面会提示“可能已执行”；原确认可以重放查询已有记录，不能换方案重下。若服务器保存回复成功但客户端丢失完成事件，刷新历史能恢复结果。

SSE 使用 `booking_confirmation` 和 `booking_result` 两个事件；JSON 回复在有预订结构化内容时增加 `timeline`。历史继续从同一个 `extra.timeline` 恢复，不增加另一套订单状态。FAILED、UNKNOWN、CONFIRMED 分别显示未完成、待核实、模拟已确认，所有模拟结果均无支付链接。未知卡可点击“核实原预订”，发送带已有预订标识的普通查询消息，不携带新的购买确认；供应商迟到结果可以在后续查询中确认。

## 4. 真实规划如何成为可预订计划

练习脚本预置计划是为了专注预订链。正式装配的规划 Agent 可以使用：

1. `list_approved_orders()`：从本人差旅单中查询已批准记录。
2. `create_plan(..., order_id=...)`：校验本人、审批状态和城市日期，再由服务端关联聊天会话。
3. 旧计划用 `bind_travel_order(plan_id, order_id)` 关联；关联变化会失效旧审核，必须重新审核。
4. 搜索、计算、`review_plan`；审核读取当前关联单，不采信模型自报审批。
5. 用户请求预订，BookingAgent 可用 `list_booking_plans()` 读取当前会话的最近 20 个计划引用（数据库先按用户和会话过滤，再限量），再准备具体项目。查询已有本地订单用 `query_bookings` / `query_booking`；未知结果卡的“核实原预订”会触发 `check_booking_result`，只按本人已有预订 ID 查询供应商，并可保存查得的既有结果及审计，绝不创建新订单；方案后来变化或确认过期也不影响只读查询，不会再次购买。

执行时仍重新检查：本人计划、同一代表方案、当前内容版本审核通过、本人关联差旅单已批准，且城市日期一致。确认后重算、改变金额或审批状态都会被拒绝。旧未知/处理中订单占用同一差旅单的同一行程段，换计划版本或方案标签也不能绕过。

## 5. Java → Python 的责任对照

Java 基线为 `/Users/shenmingjie/workSpace/LLMentor-teach/gogo-agent`，只用于对照，不修改。

| Java 入口 | Python 入口 | 对应关系 |
| --- | --- | --- |
| `agent/MasterAgent.java` 注册 `booking_agent` | `chat/executor.py:_build_agent/ask_booking_agent` | GoGo 委派独立 BookingAgent；普通预订仍经过唯一主协调入口 |
| `agent/BookingAgent.java:build` | `booking/agent.py:build_booking_agent` | 装配模型、状态和受控 Toolkit；Python 普通轮只有门禁与只读查询，本轮网页确认后才开放执行工具 |
| `tuniu-cli` 下单、`BookingPersistenceHook` 事后解析 | `booking/chat.py:execute`→`BookingService.book`→`SimulatedSupplier.create_order` | Python 先落本地待处理记录，再调用模拟网关；没有第二套 shell 下单或订单持久化路径 |
| `POST /api/chat/{sessionId}` / 交互式确认 | `ChatRequest.booking_confirmation_token`→`BookingChatTurn.verify_client_confirmation` | 本次是预订专项确认，不等于通用 HITL、恢复接口或支付已迁移 |
| `booking_result` SSE 与卡片 | 原 `ChatWindow` 消费链扩展确认卡、三态结果和历史恢复 | 仍用同一个 timeline；前端不能根据模型文字制造成功卡 |

`async def` 创建 coroutine，`await` 才推进；类似 Java 的异步编排，但并不自动新建线程。`async with asyncio.timeout` 限制 BookingAgent 总调用时间。模型可见函数通过闭包保留 RequestContext 和本轮已验证对象，类似 Java Provider 捕获请求作用域，模型不能替换闭包身份。确认工具没有参数，因此模型无法改选另一项目。

`with _BOOKING_LOCK` 是当前单进程模拟环境的串行临界区；SQL 唯一键继续防同一业务键重复记录。它不提供多进程分布式锁。模拟供应商订单在进程内存；重启后未决订单可能仍未知，不会自动重下。真实供应商、多个 worker 及更长期的对账须后续独立验收。

## 6. 照表单步

| 断点顺序 | 文件与具体语句 | 命中时看什么 | 下一步与原因 |
| --- | --- | --- | --- |
| P0，启动隐藏准备 | `scripts/demo_booking_chat.py:create_demo_app` 的 `plans.create(...)` | 本人已批单；固定日期；供应商创建 0 | 挂候选、计算、审核、保存两条历史，无模型调用 |
| P1，第一次 HTTP | `chat/executor.py:_build_agent` 的 `has_booking = any(...)` | 意图 booking；只有 `booking_agent`，没有管理 Agent 的新预订路径 | GoGo 调用子 Agent 工具 |
| P2，子 Agent | `_build_booking_agent` 的 `build_booking_toolkit(...)` | `turn.confirmed is None`；工具中没有 execute_confirmed_booking | 只能查询或 prepare_booking |
| P3，准备摘要 | `booking/chat.py:prepare` 的 `summary = self.service().check_ready(...)` | plan/order 属于 u001；审核当前；城市日期相同；金额 640.00 | 只读门禁，不调用供应商 |
| P4，签发 | 同方法的 `confirmation.issue(...)` | session/request/digest 均来自服务端；模型返回 data 不含 token | 待回复事务保存后，才交给网页 |
| P5，第一次保存 | `_stream_prepared_turn` 的 `with reply_transaction(...)` | 待确认 token 只在 extra，BookingAgent 状态不含 token | 提交后发送确认事件，再发 message_id |
| C1，按钮请求 | `booking/chat.py:verify_client_confirmation` 的历史遍历和 `confirmation.verify(...)` | 新 request_id；原卡已保存；同用户同会话；当前摘要摘要值完全相同 | 构造 confirmed_booking 路由，跳过自由文本猜授权 |
| C2，第二次子 Agent | `booking/tools.py` 的 `if turn ... confirmed is not None` | 只有此时注册 execute_confirmed_booking；它无参数 | 模型请求执行已验证对象 |
| C3，下单门禁 | `booking/service.py:book` 的 `expected_content_version` 比较、`_check_booking_slot(...)` | 版本未变，同单同行程段无其他已确认或未知订单 | 唯一键写 CREATED，再调用供应商 |
| C4，外部边界 | `booking/supplier.py:create_order` | request_id 等于 booking_id；调用计数从 0 到 1 | 按 scenario 返回成功、拒绝或未知 |
| C5，结果 | `BookingChatTurn.execute` 的 `self.results.append(data)` | resolution 是真实业务状态；无 payUrl；simulated=true | 正文按事实生成，不采信模型声称的成功 |
| C6，第二次保存 | `_stream_prepared_turn` 的 `for item in booking_turn.timeline` | 回复/状态已提交；结果将以 booking_result 发出 | 前端写时间轴；message_id 后完成 |
| R1，重放 | `BookingService.book` 的 `if existing is not None` | 同一个 booking_id，supplier.create_calls 已为 1 | 返回或只读对账，不再创建 |
| H1，刷新 | `frontend/src/components/ChatWindow.tsx` 的历史映射 | extra.timeline 同时含旧确认和新结果；confirmationId 对上 | 旧按钮变已处理，结果卡继续显示 |

## 7. 预测练习与验证

- 只在聊天文字里写 `confirmed=true`，会调用供应商吗？不会；必须独立的认证请求字段和有效已保存凭据。
- 换用户、会话，或修改/过期 token 呢？403/409；创建调用 0。
- 确认后方案又重算呢？旧审核或摘要失效，需重新审核并确认；不能拿旧价格继续购买。
- 下单已成功但回复保存失败呢？业务记录保留，同一确认重放返回原单；不会第二次创建。若确认过期或计划已变更，先查询本人记录取得原预订 ID，再用“核实原预订”只读对账。
- 未知结果后新建方案重试呢？同单同行程段被原待处理记录占用，必须先核实，不能换业务键绕过。
- 混合“查政策并预订”时模型说“已预订”呢？其他事项结果保留，预订部分由服务器摘要/业务事实生成；待确认不变成成功。

```bash
GOGO_REDIS_HOST= GOGO_DATABASE_URL= .venv/bin/python -m pytest tests/test_booking_chat.py tests/test_061_067_booking.py -q
npm --prefix frontend test
npm --prefix frontend run build
npm --prefix frontend run lint
```

全量离线回归沿用阶段 G 的隔离命令，显式排除现有真实模型/MariaDB 集成入口，避免测试读取 `.env` 后产生外部写入。实际在线模型如何选择新工具、真实供应商/支付、通用 HITL、多 worker 与跨重启模拟供应商状态均未在本次验证。

## 8. 本次验证记录（2026-10-26）

- 全量离线 Python：**621 passed、11 skipped、1 deselected**。显式排除真实模型及 MariaDB 集成入口，并清空数据库/Redis/专用 MySQL 测试连接，使用离线模型占位配置。
- 前端：构建与 Lint 通过，**11 项状态/组件测试通过**；三态结果、未保存/失效摘要、旧格式、凭据不入 DOM、未知核实入口均有覆盖。
- 新增业务模块和脚本 Pyright：**0 errors**。会话 JSON 过滤已在 SQLite 实跑，并完成 MySQL 方言编译；没有连接真实 MariaDB 验证。
- CLI 的 ok/reject/lost_after_commit/lost_before_commit 四个 scenario 实跑，供应商创建调用均为 1，重放没有再创建。
- 浏览器：内置 IAB 不可用，改用本机 Chrome/CUA 验证。桌面走过准备→确认→成功、刷新回放，以及未知→核实原预订；检查过 393×852 布局。控制台 error 级未见应用相关错误。未知核实实际显示 check_booking_result，只读查询未发购买确认。
- 审查发现的会话过滤顺序、旧文档、业务审计及供应商调用后持久化失败分类均已修正，追加回归验证。

验证使用固定模型、隔离 SQLite 和模拟供应商。在线模型选择可靠性、真实供应商/支付、多 worker 和跨重启供应商状态仍未验收。按用户要求，代码保留在工作区，没有暂存或提交。
