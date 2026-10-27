# 预订 Agent 三项能力缺口与接入方案

> 2026-10-26。对照 Java 基线 `/Users/shenmingjie/workSpace/LLMentor-teach/gogo-agent/src/main/java/com/gogo/travel/` 与当前 Python 实现，评估「真实供应商 / 支付链路 / 用户信息与密钥」三项的接入面。
> 本文是**评估与方案**，不是已完成的修复。除特别标明「实测」外，行号与结论均来自源码静态阅读；**未运行任何测试或演示脚本**。

- **状态**：方案，未实施
- **影响范围**：`booking/`、`security/`、`travel/`、`chat/`、`frontend/`
- **严重度**：中 —— 当前系统只能跑模拟演练，不能承接真实订单
- **定位**：`src/gogo_agent/booking/supplier.py`、`src/gogo_agent/security/apikey.py`、`src/gogo_agent/booking/tools.py`

---

## 一、正常机制（Situation）

### 1.1 读者想完成什么

用户确认一份已审核方案后，系统向真实供应商下单、拿到支付入口、用本人联系资料完成出票。这条链在 Java 基线里存在，在 Python 里只到「模拟供应商返回假订单号」为止。

### 1.2 当前的实际路径

```text
网页确认卡点击
  → POST /api/chat/{sessionId} + booking_confirmation_token
  → BookingChatTurn.verify_client_confirmation        (chat.py:73)
  → BookingService.book(confirmed=True)               (service.py:162)
      → _gate：审核当前版 + verdict=PASS + 差旅单 APPROVED   (service.py:109)
      → _insert_pending：先写 CREATED / payment_status=UNPAID  (service.py:215)
      → _execute → self.supplier.create_order(...)     (service.py:249, 252)
          → SimulatedSupplier：4 种脚本模式             (supplier.py:59)
      → _finish_confirmed / _finish / _mark_unknown
  → BookingChatTurn.execute 组装 type="booking_result" (chat.py:109)
  → SSE timeline → BookingResultCard.tsx
```

### 1.3 关键状态应由谁产生

| 状态 | 权威来源 | 位置 |
| --- | --- | --- |
| 审核是否通过 | `PlanService` 的 `last_review` | `service.py:128-132` |
| 差旅单是否已审批 | `TravelBusinessService` 的 `TravelOrder` | `service.py:139` |
| 本地预订记录 | `BookingRecordModel`（先写后调） | `service.py:215` |
| 供应商订单事实 | `SupplierGateway` 返回值 | `supplier.py:31` |
| 支付状态 | **无人产生**（见二.2） | — |
| 用户联系资料 | `TravelBusinessService.get_profile` | `travel/service.py:165` |

### 1.4 把修复目标写成可检查的行为要求

1. **真实供应商**：同一 `booking_id` 经真实 CLI 下单后，本地记录的 `external_order_no` 与供应商侧一致；供应商拒绝时本地记 `FAILED`；超时或响应丢失时记 `UNKNOWN` 且**不第二次调用创建**。
2. **支付链路**：`booking_result` 事件里的 `payUrl` 来自供应商响应的真实字段；取不到时为 `null`，前端不渲染支付按钮，**不得由代码拼接**。
3. **用户信息与密钥**：本人可通过已认证入口保存/撤销途牛 Key；下单所需的联系人字段来自 `UserProfileView`，缺失时在门禁阶段就返回缺项清单，**不进入供应商调用**。

这三条是判断方案有效性的尺子，不是「加了几个字段」。

---

## 二、异常与影响（Complication）

### 2.1 已观察到的代码事实

| # | 事实 | 证据 |
| --- | --- | --- |
| A | 真实供应商未接入 | `supplier.py:5` 模块 docstring 自述「真实供应商（途牛 CLI 的下单命令等）未在本仓库接入」 |
| B | `payUrl` 恒为 `None` | `chat.py:127` 字面量 `"payUrl": None` |
| C | `ApiKeyService.save` 无生产调用方 | `grep -rn 'ApiKeyService\|\.save(user' src/` 排除定义后，只命中读端 `planning/search.py:104`（实测） |
| D | 无 Key 管理 HTTP 路由 | `api.py:28-33` 注册 6 个 router：auth / chat / preference / travel / admin / callback（实测） |
| E | 预订 toolkit 无联系人工具 | `booking/tools.py:155` 只有 6 个函数：`resolve_plan, prepare_booking, query_booking, query_bookings, list_booking_plans, check_booking_result` |
| F | `SupplierGateway` 是同步 Protocol | `supplier.py:43-56` 三个方法全是 `def`，无 `async` |

### 2.2 必须更正的既有说法

本仓此前（含对话中的口头评估）把第 2 项描述为「无 payUrl、无 SSE 支付事件、无 PENDING_PAYMENT/PAID 状态流转」。核实后只有第一条成立：

| 原说法 | 实际 |
| --- | --- |
| 无 SSE 支付事件 | **不成立**。SSE 通道存在：`chat.py:109` 产出 `type="booking_result"`，经 `chat/executor.py:960` 的 `timeline` 推送，`BookingResultCard.tsx` 会渲染。缺的只是其中的 `payUrl` 字段 |
| 无 PENDING_PAYMENT/PAID 状态流转 | **不成立**，且**Java 也没有**。Java 侧 `setStatus(BookingStatus...)` 全库仅 4 处：`CREATED`×1（`BookingPersistenceHook.java:190`）、`CANCELLED`×2（同文件 :283、`BookingWriteTools.java:99`）；`paymentStatus` 只在 `BookingPersistenceHook.java:191` 写死 `"UNPAID"`；`BookingStatus.PAID` **全库零引用**（实测）。Python 的 `models.py:37-38` 那两个枚举值是**死值**，不是待迁移缺口 |
| 用户信息与密钥工具在 Python 里没有对应项 | **部分不成立**。`travel/tools.py:90` `query_user_contact_info`、`:102` `update_user_contact_info` 都存在，注册在**管理 Agent** toolkit（`:276`）。缺的是「预订 Agent 拿不到」而不是「Python 没有」 |

**这一节的用途**：写方案前先把夸大项砍掉，否则会去补一个 Java 从未存在过的状态机。

### 2.3 影响与推测的边界

- **已证实的影响**：用户无法通过任何已注册路由保存途牛 Key（事实 C+D），而读端 `planning/search.py:56` 每次搜索都会 `resolve_for_use` —— 这是「读端接通、写端悬空」。
- **推断（未验证）**：真实下单因同步/异步边界无法直接接（见四.1）。这是从 `supplier.py` 与 `skill_runner.py` 的签名推出，**没有实际尝试接入并观察报错**。

---

## 三、核心疑问（Question）

> 同一类「Java 有、Python 无」的缺口，为什么三项的接入成本差一个数量级？是代码量差异，还是某处结构与缺口不兼容？

把「下单」这一个动作按数据流拆开，四个环节各自问一遍：

```text
① 参数从哪来？   联系资料 / 证件 / 乘客  ← 本仓有 get_profile，但没接线
② 怎么调？       异步 CLI vs 同步 Protocol ← 签名不兼容
③ 结果怎么落？   已有 _finish_confirmed 等，无需改
④ 支付入口从哪来？ 供应商响应里的 URL     ← 只能在接真实供应商后才有
```

答案收敛为一个判断：**①②是结构问题，③已解决，④是①②的下游。** 所以三项的真实顺序不是并列，而是：

```text
密钥保存入口（独立，且是①④的前置）
    ↓
支付链路（5 个文件，但 payUrl 依赖真实供应商）
    ↓
用户信息接线（2 个文件，依赖 profile 数据真实）
    ↓
真实供应商（8–12 个文件，卡在同步/异步边界）
```

---

## 四、排查证据：从现象回到代码（Evidence）

### 4.1 阻塞点：`SupplierGateway` 同步 vs `SkillRunner` 异步

```python
# booking/supplier.py:43-56（仓库原文）
class SupplierGateway(Protocol):
    def create_order(self, request_id: str, payload: dict[str, Any]) -> SupplierOrder: ...
    def lookup(self, request_id: str) -> SupplierOrder | None: ...
    def cancel(self, order_no: str, reason: str | None) -> bool: ...

# booking/service.py:249-252 —— 调用方也是同步
def _execute(self, user_id: str, booking_id: str, payload: dict[str, Any], leg: str, amount: Decimal) -> BookingOutcome:
    """写入类调用不重试：拒绝视为失败，超时或异常视为未知并转入只读对账。"""
    try:
        order = self.supplier.create_order(booking_id, payload)

# integrations/skill_runner.py:177 —— 唯一的 CLI 执行入口只有异步版本
async def run_json(self, *, user_id: str, args: Sequence[str], ...) -> SkillResult:
```

**这条链解释了为什么「接一下供应商」不是改一个类**：`Service._execute` → `book` → `cancel` → `_reconcile` → `check_result` 全是同步方法，而真实 CLI 必须 `await`。

### 4.2 第二阻塞点：payload 字段对不上

```python
# booking/service.py:172-176 现状 payload（送进供应商的全部内容）
payload = {
    "leg": leg, "code": spec["code"], "amount": f"{spec['amount']:.2f}", "currency": CURRENCY,
    "title": spec["title"], "plan_id": plan.plan_id, "content_version": plan.content_version,
    "proposal_tag": proposal_tag,
}
```

真实 `tuniu call flight saveOrder` 需要联系人、证件、乘客、舱位等。**这些字段在本仓有原料但未接线**：

```python
# travel/service.py:184 —— 已按品类定义必填项
def profile_completeness(self, user_id: str, biz_type: BookingType) -> ProfileCompleteness:
    required = {
        BookingType.FLIGHT: ("chinese_name", "id_type", "id_number", "phone", "gender"),
        BookingType.HOTEL:  ("name_pinyin", "email", "phone"),
        BookingType.TRAIN:  ("chinese_name", "id_type", "id_number", "phone"),
    }[biz_type]
```

**所以这一步不是「写代码」，是「做业务映射」。** 报价时容易漏掉这块。

### 4.3 密钥断点

```text
ApiKeyService.save(user_id, provider, key)   apikey.py:110   ← 定义完整，含加密落库
        ↓ 调用者
   grep src/ 排除定义 = 0 处生产代码（仅 security/exercises/ 教学脚本）

HTTP 路由：api.py:28-33 六个 router 无 apikey
Agent 工具：Java 有 check_tuniu_api_key / save_tuniu_api_key（ApiKeyTools.java:98/123），Python 无

读端却在用：planning/search.py:56  resolve_for_use(user_id, "tuniu-cli")
```

**这是三项里唯一「现在就是断的、且与供应商无关」的问题。**

### 4.4 已排除的解释

| 猜想 | 排除依据 |
| --- | --- |
| 「PENDING_PAYMENT/PAID 是待迁移状态机」 | Java 全库零引用 `BookingStatus.PAID`、`setPaymentStatus` 只写一次 `UNPAID`（实测），没有可迁移的目标 |
| 「SSE 支付事件缺失」 | `booking_result` 事件链路完整，缺的是字段值 |
| 「Python 完全没有用户资料能力」 | `travel/tools.py:90/102` + `travel/service.py:165/184` 都在 |
| 「接供应商只需换一个 Gateway 实现」 | 签名方向不兼容（4.1），payload 无来源（4.2） |

---

## 五、接入方案（Answer）

### 5.1 三项的成本对比

| 缺口 | 改动面 | 文件数* | 关键前提 | 独立可做 |
| --- | --- | --- | --- | --- |
| 密钥保存入口 | 中 | 5–6 | 无 | ✅ |
| 支付链路 | 小 | 5–6 | 真实 payUrl 依赖供应商 | ⚠️ 可先做字段与前端，值留空 |
| 用户信息接线 | 小 | 2 | profile 数据真实 | ✅ |
| 真实供应商 | 大 | 8–12 | 同步/异步改造 | ❌ 需要专门一轮 |

* 文件数为按代码触点逐个点数的估算，非穷举。

### 5.2 ③ 密钥保存入口（建议先做）

| 动作 | 文件 | 说明 |
| --- | --- | --- |
| 新建 `security/router.py` | 新建 | `POST/GET/DELETE /api/keys/{provider}`，依赖 `get_current_user`，写走 `ApiKeyService.save/revoke` |
| 注册路由 | `api.py` | 追加一行 `include_router` |
| 前端 Key 管理 | `frontend/src/` | 至少提供保存与状态显示 |

**行为要求**：保存后立即 `has_active` 为真、`resolve_for_use` 可取出、日志与响应只回显掩码（复用 `security/masking.py`）。

### 5.3 ② 支付链路

| 动作 | 文件 | 说明 |
| --- | --- | --- |
| `SupplierOrder` 加 `pay_url: str \| None = None` | `supplier.py` | 冻结模型加字段，默认 `None` |
| `SimulatedSupplier` 生成模拟 URL | `supplier.py` | 便于前端联调 |
| `BookingOutcome` 加 `pay_url` 并透传 | `service.py` | `_outcome` 加参数 |
| `"payUrl": None` → `outcome.pay_url` | `chat.py:127` | 一行 |
| 条件渲染支付按钮 | `BookingResultCard.tsx` | `data.payUrl` 为真才显示 |
| 类型已就绪 | `frontend/src/types/index.ts:182` | 已有 `payUrl?: string \| null`，无需改 |

**明确不做**：`PENDING_PAYMENT`/`PAID` 流转。它需要支付回调入口，而本仓 `callback_router` 目前只有 `/api/callback/dingtalk/approval` 且**明确 503 禁用**（`travel/router.py:215`）—— 在一个禁用的回调通道上再挂支付回调，是把未验证的信任边界扩得更大。

**取不到 payUrl 时**：保持 `null`，前端不渲染按钮。Java 侧是靠 6 个候选键逐个试（`payUrl/paymentUrl/cashierUrl/orderDetailH5Url/orderDetailUrl/jumpUrl`，`BookingPersistenceHook.java:239`），Python 接真实供应商时照抄这份候选表即可。

### 5.4 ③' 用户信息接线

| 动作 | 文件 | 说明 |
| --- | --- | --- |
| 抽出联系人查询函数 | `travel/tools.py` | 现为 `build_manage_toolkit` 内闭包，需提到模块级或共享工厂 |
| 预订 toolkit 注册 | `booking/tools.py:155` | `functions` 列表加一项 |

**必须保持**：`update_user_contact_info` 在 `travel/tools.py:108` 返回 `CONFIRMATION_REQUIRED` 且**不写库**。这是刻意的确认边界，搬到预订 Agent 时不能顺手改成直接写。

**门禁联动**：真实下单前应调用 `profile_completeness(user_id, biz_type)`，缺项直接 `BookingError`，**不进入供应商调用**。这条现在是空的——`_gate`（`service.py:109-144`）完全没有资料检查。

### 5.5 ① 真实供应商（最后一轮）

两条路线，**建议 A 但先说清代价**：

| | 路径 A：整链改 async | 路径 B：同步里跑协程 |
| --- | --- | --- |
| 改动 | `supplier.py` Protocol、`service.py` 的 `book/_execute/_reconcile/check_result/cancel/_cancel_hotel_at_supplier`、`tools.py` 6 个函数、`chat.py` `execute()`、全部调用方与测试 | 只新增一个 adapter |
| 收益 | `_BOOKING_LOCK = RLock()`（`service.py:38`）可换 `asyncio.Lock`，不再阻塞事件循环 | 改动局部 |
| 代价 | 8–12 文件，测试面随之扩大（未穷举） | FastAPI async 请求内 `asyncio.run()` 直接报错；线程池方案让 `RLock` 变成跨线程争用 |

```text
路径 A 的分步（建议顺序）：
1. supplier.py：Protocol 三方法改 async，SimulatedSupplier 同步方法用 async def 包一层
2. service.py：book/_execute/_reconcile/check_result/cancel 改 async；RLock → asyncio.Lock
3. tools.py：6 个工具函数改 async（AgentScope FunctionTool 支持协程）
4. chat.py：execute() → async execute()
5. 新增 tuniu_supplier.py：用 SkillRunner.run_json 实现三方法，payload 从 profile 组装
6. 测试：现有 make_adapter / supplier 构造点需适配（未点数）
```

**风险提示**：`_execute` 的 `except Exception`（`service.py:259`）当前会把包括 `SupplierUnknownOutcome` 在内的一切当成 UNKNOWN。改 async 后，`asyncio.CancelledError` 是 `BaseException`，**不会被这段捕获**——行为不变，但要确认 `finally` 里 `_persisted_outcome` 的语义没被 `await` 打断。

---

## 六、验证结果与边界（Verification）

### 6.1 本轮已证实（静态阅读 + grep）

- 2.1 表格 A–F 六条事实。
- 2.2 三条更正：Java 的 `PAID` 零引用、`paymentStatus` 只写一次、`ApiKeyService.save` 零调用方、`api.py` 六 router 无 apikey。
- 4.1/4.2/4.3 的代码引用与行号。

### 6.2 本轮未验证（明确标记）

| 项 | 状态 |
| --- | --- |
| 任何测试或 demo 脚本 | **未运行** |
| 路径 A 改 async 会打破多少测试 | **未统计**；`tests/test_061_067_booking.py`、`tests/test_booking_chat.py` 的 `make_adapter`/`supplier` 构造点未读 |
| 真实 tuniu CLI 的响应结构 | **未接触**；本文的字段推测来自 Java `BookingPersistenceHook` 的解析逻辑 |
| `payUrl` 候选键在实际响应中的命中率 | **未知** |
| 文件数 5–6 / 2 / 8–12 | **估算**，按代码触点逐个点数，非穷举 |

### 6.3 方案仍依赖的条件

1. **密钥入口必须先通** —— 否则 ①④ 接了也无法验证（`resolve_for_use` 会抛 `TUNIU_KEY_MISSING`）。
2. **profile 数据必须真实** —— `get_profile`（`travel/service.py:165`）返回空 `UserProfileView` 时，资料门禁会拦住所有下单。
3. **端到端验证需要一个真实或录制的 tuniu 响应** —— 本仓目前只有 `SimulatedSupplier` 的 4 种模式，没有录制样本。

### 6.4 建议顺序（成本与前置反着排）

```text
第 1 步  密钥保存入口     独立、现在就是断的、后续两项的前置
第 2 步  支付链路         便宜，可先接字段与前端的条件渲染
第 3 步  用户信息接线     2 个文件，接完门禁才能拦缺项
第 4 步  真实供应商       需要专门一轮，含 async 改造与业务字段映射
```

---

## 七、变式练习（推导，未执行）

**题目**：假设已完成第 1–3 步但未做第 4 步（仍是 `SimulatedSupplier`）。用户点击确认卡后，`execute_confirmed_booking` 被调用。**`BookingResultCard` 会不会显示「立即支付」按钮？**

> **参考答案**：不会。`SimulatedSupplier.create_order`（`supplier.py:80`）构造的 `SupplierOrder` 只有 `request_id / order_no / status / amount / currency` 五个字段（`supplier.py:31-40`）；若第 2 步只加了 `pay_url` 字段但模拟器不填，值为 `None` → `BookingOutcome.pay_url` 为 `None` → `chat.py:127` 的 `payUrl` 为 `None` → 前端条件渲染为假。
>
> **推导链**：`SimulatedSupplier` 是否生成 URL（第 5.3 步里明确要求它生成）→ `_outcome` 是否透传 → `chat.py` 的字段赋值 → 前端条件判断。**四个环节任一处为 `None`，按钮就不出现** —— 这也是为什么第 2 步可以「先做但值为空」而不算假完成：它在结构上就是正确的降级。
>
> **再改一个条件**：若第 2 步不改 `SimulatedSupplier`（保持不产 URL），而直接接真实供应商（第 4 步），按钮仍可能不出现 —— 因为 Java 的 6 个候选键是从响应里**尝试**提取，提取不到同样是 `null`。**「接了真实供应商」不等于「一定有支付链接」。**

---

## 八、遗留项

| # | 项 | 说明 |
| --- | --- | --- |
| 1 | `BookingStatus.PENDING_PAYMENT` / `PAID` | 保留枚举但不流转。若将来要做，需先有支付回调的信任边界（当前 `callback_router` 的钉钉回调仍是 503 禁用） |
| 2 | 长期记忆 | Java 用 `LongTermMemory` + `retrieve_from_memory`；Python 无。本仓 `context/preferences.py` 是 4 个固定 key 的 CRUD，**不等价**（自由文本向量召回 vs 固定分类）。是否用 preferences 顶替属业务决策，未定 |
| 3 | `_plan_search_call` 每次新建 adapter | `chat/executor.py:269` 每次搜索都 `TuniuCliAdapter(...)`，无状态但意味着将来加并发闸门/计数会归零 |
| 4 | Java `check_travel_order_approval` 的 session 缓存 | Java 有 `sessionCtx.setCachedOrder`（`TravelOrderReadTools.java:413`）；Python `_gate` 每次都读库。属性能差异，非功能缺口 |

---

## 九、经验

**「Java 有、Python 无」这句话本身不足以作为工作量估算。** 同一句话下藏着三类完全不同的东西：

1. **真的缺代码**（密钥保存入口）——最便宜、最该先做、最容易被漏掉，因为它不报错，只是永远为空。
2. **结构不兼容**（同步 Protocol vs 异步 CLI）——代码量不大，但改动会向外扩散到所有调用方。
3. **本来就没有**（`PENDING_PAYMENT`/`PAID` 流转）——补它是新增功能，不是迁移。**把它当成迁移缺口，就会去补一个 Java 从未存在过的状态机。**

判断方法只有一条：**去读对方那一侧的实现，而不是读对它的描述。** 本文 2.2 节的三条更正，全部来自「读 Java 源码」而不是「信之前的口头结论」。
