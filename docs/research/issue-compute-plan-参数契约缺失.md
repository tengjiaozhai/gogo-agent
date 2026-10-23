# compute_plan 的 scores_json / policy_json 缺乏机器可见契约，导致模型必然传错

- **状态**：**已修复**（修复记录见第九节）
- **影响范围**：`ItineraryPlanAgent` 计算方案环节，修复前稳定复现
- **严重度**：高 —— 修复前规划主链路无法成功，且错误不可自纠
- **定位**：`src/gogo_agent/planning/tools.py`、`src/gogo_agent/planning/models.py`、`src/gogo_agent/planning/agent.py`

---

## 一、现象

模型调用 `compute_plan` 时传入：

```json
{
  "plan_id": "bfe31b742c0b42a1b8f5236111e0cfc6",
  "policy_json": "{\"标准差旅政策\": \"交通经济舱/二等座优先，酒店为标准或舒适型房间\"}",
  "scores_json": "{\"preference_weights\": {\"时间优先\": 0.5, \"价格优先\": 0.3, \"舒适度优先\": 0.2}}"
}
```

返回：

```json
{
  "success": false,
  "errorCode": "INVALID_PARAM",
  "message": "偏好分或政策 JSON 字段无效"
}
```

## 二、复现与根因

两个参数**同时**校验失败，原因是两个模型都设了 `extra="forbid"`：

```text
scores_json → loc=('preference_weights',) type=extra_forbidden
policy_json → loc=('标准差旅政策',)        type=extra_forbidden
```

模型真实要求的结构（`models.py:122-128`、`models.py:88-99`）：

```json
// scores_json
{ "transport_scores": { "F1": { "score": 85, "basis": ["时间最早"] } },
  "hotel_scores":     { "H1": { "score": 70, "basis": ["离会场近"] } } }

// policy_json（只收数值/枚举字段，不收自然语言）
{ "flight_class": "经济舱", "train_seat_class": "二等座", "hotel_star_limit": 3 }
```

上述正确格式可正常解析。**因此问题不在计算层，而在“模型如何得知契约”这一层。**

## 三、责任归属

判断依据：模型只能依据三条信息行动 —— system prompt、tool schema、错误反馈。本 case 中**三条路径全部缺失格式信息**。

| 层 | 有责 | 严重度 | 证据 |
|---|---|---|---|
| **工具 schema** | **是（主责）** | 高 | 参数声明为 `str`，schema 里只有 `{"type": "string"}`，模型看不到任何结构 |
| **错误信息** | **是** | 高 | `except ValidationError` 丢弃了 `loc`/`type`，只回一句合并文案，模型无法自纠 |
| **Agent 提示词** | 是（次责） | 中 | `agent.py:15` 只说“由你通过 scores_json 给出”，未给格式；`tests`/`scripts`/`docs` 全仓库无一处正确示例 |
| **Agent 行为** | **否** | — | 在信息如此有限下，它按业务语义猜测，方向合理 |
| **用户输入** | 否 | — | 参数语义正确，是接口未承接 |

### 3.1 工具 schema 的问题（主责）

修复前的签名：

```python
def compute_plan(plan_id: str, scores_json: str = "", policy_json: str = "", weather_summary: str = "") -> str:
```

暴露给模型的 schema 只有：

```json
"scores_json": { "type": "string" },
"policy_json": { "type": "string" }
```

这是典型的**“把结构化契约塞进字符串”反模式**：校验发生在 `model_validate_json`，但该约束对模型**完全不可见**。

### 3.2 错误信息的问题

```python
except ValidationError:
    return _fail("INVALID_PARAM", "偏好分或政策 JSON 字段无效")
```

`ValidationError.errors()` 里有精确到字段的 `loc` 与 `type`，**全部被丢弃**。对比**同一文件**的日期错误 —— 那里给了正确格式说明。同一文件内标准不一致，说明作者知道该怎么做，此处遗漏。

模型收到“字段无效”能做什么？只能盲目重试或放弃。这把**可自纠错误变成了不可自纠错误**。

### 3.3 提示词的问题

修复前 `agent.py` 全文未出现任何 JSON 结构说明，只有一行：

> 偏好判断由你通过 scores_json 给出，工具不解读偏好文本。

全仓库检索正确示例：`tests`、`scripts`、`docs` **均无**。

需说明：**提示词本不该承担 schema 的职责**。即便提示词补上示例，也只是绕过 3.1 的补丁，不是修复。

## 四、两个更深的设计矛盾

### 4.1 “时间优先 0.5” 这类自然诉求无处安放

用户传的 `{时间优先: 0.5, 价格优先: 0.3, 舒适度优先: 0.2}`，语义上精确对应 `DimensionWeights(time, price, preference, experience)`。

但修复前工具层把它**写死**了：`weights=DimensionWeights()`。而 `service.compute` 其实**是接受 weights 的**（`service.py:110-113`）。

**下层能力具备，工具层没暴露。**

### 4.2 `preferences` 自由文本是“死参数”

`create_plan` 有 `preferences: str` 参数，能存进 `PlanConditions.preferences`。但追到评分层（`scoring.py:452-455`）它**只是被原样回显到 `PlanResult.preferences`**，模型说明写着：

> 用户偏好原文，**仅供追溯**

即：用户写了“我想坐最早一班”，这个字符串**不参与任何打分**。真正影响偏好分的是 `scores`。这构成陷阱 —— 看起来表达了偏好，实际无效。

### 4.3 `"auto"` 隐式约定无人知晓

修复前支持 `scores_json="auto"` 跳过校验，这本是最容易的降级路径，但 schema 与提示词**均未提及**。

## 五、结论

> **不是 Agent 约束不到位，是工具没有把契约“广播”给 Agent。**

模型可依据的三条路径（schema / 提示词 / 错误回执）全部缺乏格式信息。换任何模型都会失败 —— 这不是能力问题，是接口设计问题。

---

## 六、修复内容

### 6.1 改为结构化参数（主要修复）

```python
def compute_plan(
    plan_id: str,
    scores: PreferenceScores | None = None,
    policy: PlanPolicy | None = None,
    weights: DimensionWeights | None = None,
    weather_summary: str = "",
) -> str:
```

Pydantic 模型作为注解后，AgentScope 的 `_extract_input_schema` 会自动生成完整 `$defs`，模型可直接看到字段级约束。

**注意一个关键事实**：`FunctionTool.call` 直接 `**kwargs` 展开，**不做 Pydantic 校验**，因此函数内仍需手动 `model_validate`。实测确认过这一点（传入 JSON 对象到达函数体时是 `dict`，不是模型实例）。

### 6.2 暴露 weights（对应 4.1）

`weights` 现在可从工具层传入，不再写死。“时间优先 0.5”这类诉求有了安放处。

### 6.3 错误信息带字段名（对应 3.2）

新增 `field` 字段与 `_invalid_field()` 辅助函数，把 Pydantic 的 `loc`/`msg` 翻译成模型可读句子：

```json
{
  "success": false,
  "errorCode": "INVALID_PARAM",
  "field": "scores",
  "message": "参数 scores 不合法：字段 preference_weights Extra inputs are not permitted；期望结构见该参数的类型定义"
}
```

### 6.4 提示词补格式示例（对应 3.3）

`agent.py` 的 `PLAN_SYSTEM_PROMPT` 现在给出 `scores`/`policy`/`weights` 三者的对象示例，并明确“不要把自然语言政策原文放进 policy”“用户说时间优先应折算成 weights”。

## 七、兼容性处理

改为结构化参数属于**破坏性变更**。已同步更新：

- `tests/test_042_060_planning.py`、`tests/test_043_planning_chat.py` 中 3 处 `policy_json=...model_dump_json()` → `policy=...model_dump()`

旧的字符串用法现在会得到明确拒绝（而非静默解析）：

```json
{ "errorCode": "INVALID_PARAM", "field": "scores",
  "message": "参数 scores 必须是对象或省略；期望结构见该参数的类型定义" }
```

## 八、验收结果

新增 5 个回归测试（`test_044_*`），全部通过：

| 测试 | 锁定内容 |
|---|---|
| `test_044_compute_plan_exposes_structured_schema_to_model` | schema 里 `scores`/`policy`/`weights` 都以 `$ref` 指向非空 `$defs` |
| `test_044_compute_plan_accepts_structured_objects` | 结构化对象能算出方案 |
| `test_044_compute_plan_rejects_unknown_field_and_names_the_parameter` | 非法字段回指 `field` 且带出具体字段名 |
| `test_044_compute_plan_rejects_string_payload_instead_of_silently_parsing` | 旧字符串用法被明确拒绝 |

端到端实测（真实工具链）：

```text
场景1 结构化对象       → success=True, proposals=2
场景2 用户原错误写法   → field=scores, message 含 preference_weights
场景3 policy 传自然语言 → field=policy, message 含 标准差旅政策
场景4 scores 传字符串   → field=scores, "必须是对象或省略"
```

全量测试：**522 passed, 1 failed**。失败项是既有的 `test_022_async_context.py`（SSE 首事件断言），与本次改动无关，`grep planning` 该文件为 0 处。

## 九、遗留项（未在本次修复）

| # | 问题 | 说明 |
|---|---|---|
| 1 | `preferences` 仍是死参数（4.2） | 建议在工具描述里标注“仅追溯，不影响评分”，或真正接入评分 |
| 2 | `test_022_async_context.py` 失败 | `chat/executor.py` 新增 `event: progress` 后，该测试仍断言首事件是 `message`；属另一处未提交改动的分歧 |
| 3 | `scores_json` 字符串入参已移除 | 若有外部调用方依赖旧签名，需按第七节适配 |

## 十、经验

**结构化契约必须让模型看得见。** 把 Pydantic 校验藏在字符串参数背后，等于把约束留在服务端、把猜测留给模型 —— 而模型猜的永远是“业务上合理”的形状，恰好不是代码要求的形状。

---

## 十一、后续：同类问题在 `get_plan` 上再次出现（已修复）

修复 compute_plan 后，用户报告 `get_plan` 也返回 `PLAN_NOT_FOUND`。诊断结论：**`get_plan` 本身没有缺陷**，是传入的 `plan_id` 在持久化层中不存在。

### 证据

差分测试（同一工具、同一用户、唯一变量是 plan_id）：

```text
A. 库中真实存在的计划      → success=True
B. 用户报错的 plan_id      → PLAN_NOT_FOUND
C. u002 的计划（u001 读）  → PLAN_NOT_FOUND（隔离正常）
```

排除项：服务 CWD 正确、`.env` 正常加载、未设置 `GOGO_PLAN_STORE`、全库无删除计划路径、身份为 `u001` 无歧义。新建计划已验证可跨进程读回。

### 真正缺陷

| # | 缺陷 | 位置 |
|---|---|---|
| 1 | 失败回执不一致：`search_candidates` 的搜索失败带「当前下一步」，但四个工具的计划错误分支全是裸 `_fail(exc.code, str(exc))` | `tools.py` |
| 2 | 提示词无恢复规则；而规划 Agent 的 AgentState 跨重启持久化，模型会长期复用失效 plan_id | `agent.py` |

### 修复

1. 新增 `_plan_failure(exc)`：`PLAN_NOT_FOUND` 时附带 `field="plan_id"` 与「重新调用 create_plan」的指引；其余错误码保持原样。四处 `except PlanError` 统一走该助手。
2. 提示词补充：任何工具返回 `PLAN_NOT_FOUND` 即表示 plan_id 已失效，不得复用，应据已知条件重新 `create_plan`。

### 隐私约束

计划不存在与越权访问共用同一错误码（防探测），因此重建提示**不区分两者**、文案完全一致。已加测试锁定：跨用户与不存在返回同一份 message。

### 验证

- 新增 3 个测试：四个工具都回指 `field="plan_id"` 且含 `create_plan`；跨用户文案不泄露；提示词含恢复规则。
- 反例能力已验证：把回执替回旧行为后，新测试断言失败（红）；恢复后通过（绿）。
- 全量：`525 passed, 1 failed`（失败为既有 `test_022`，与本改动无关）。

### 未修

`create_plan` 仍非幂等，每次调用生成新 plan_id；重试会持续堆积计划（实测 10 分钟内 16 条）。是否允许同行程多计划属业务决策，未改动。
