# 长期记忆复刻方案：Java 基线 → Python + ReMe

> 2026-10-27。参照 Java 版 `TravelPreferenceLongTermMemory` 的职责，用 ReMe 在 Python 版复刻。
> **核心发现：不需要自研 HTTP 客户端。** `agentscope==2.0.8` 已内置 `ReMeMiddleware`（`agentscope.middleware._longterm_memory._reme`），项目只需装 `reme-ai`。
> 状态：**方案，未实施**。除标注「实测」外均为源码阅读推导。

---

## 一、Java 版长期记忆到底在做什么（复刻的靶子）

读 `TravelPreferenceLongTermMemory.java`、`...Factory.java`、`...MemoryCache.java`、`PreferenceController.java` 得出五项职责：

| # | 职责 | Java 实现 | 关键约束 |
|---|---|---|---|
| 1 | **偏好持久化（唯一载体）** | 百炼托管记忆库（`memoryLibraryId`）；**无本地表**（`find -iname "*preference*"` 只命中 memory/ 与 Controller；`grep -i preference *.sql` 零命中） | 自然语言片段，非结构化字段 |
| 2 | **规划前提供 preferences 自由文本** | `itinerary-plan-agent-system.md:49`、`:84` 强制要求 `preferences` **必传**自 `retrieve_from_memory`，**不得编造**；无偏好则传空串 | 这是它最重要的一项工作 |
| 3 | **用户可见的偏好表单** | `PreferenceController`：GET 召回 + fastModel 反解析；POST 结构化 → 自然语言 → `record` | 写入是**追加**语义 |
| 4 | **按用户隔离 + 慢调用缓存** | `userId` 来自请求上下文，每次 Agent 构建现造；Redis `ltm:travel-pref:{userId}` TTL 1800s，`record` 成功后 `evict` | 百炼 retrieve 是跨网络慢调用 |
| 5 | **四个 Agent 挂载，两个真用** | Master / Plan / Booking / Review 都 `AGENT_CONTROL`；Review 提示词明确「无需调用 `retrieve_from_memory`」 | 默认 `topK=20`，Controller 用 `topK=100` |

### 已被证实的缺陷（复刻时不该照抄）

1. **来源不可区分**：用户表单选的 与 Agent 自主 `record_to_memory` 记的，写进同一个库、同一段自然语言，**无任何字段区分**。`PreferencePage.tsx:114-124` 只在解析失败时才暴露「智能助手已记录的偏好」，成功时混进表单**看起来像用户自己选的**。
2. **有损往返**：结构化 → 自然语言 → 记忆库 → 自然语言 → fastModel 解析 → 结构化。`getUserPreferences` 同时返回 `memorySummary` 原文与 `preferences`，等于承认不可逆。
3. **无法撤销 AI 的错误推断**：表单只能改能匹配选项的字段，AI 误记的留在库里。
4. **覆盖关系不明**：`record` 是追加，百炼内部合并策略不在代码里。

---

## 二、Python 侧现状（起点比想象中好）

### 已有的

~~~python
# src/gogo_agent/context/preferences.py:1-45
"""085：显式用户偏好，采用本地 SQL 存储，不让模型抽取或授权写入。"""

class PreferenceWrite(BaseModel):        # value + source_message（可追溯、可撤回）
class PreferenceView(PreferenceWrite):   # key: Literal["transport","hotel","schedule","other"]
class PreferenceRow(Base):               # user_travel_preference，主键 (user_id, key)
class PreferenceStore:                   # list / put / delete —— 物理删除，不召回旧版本
~~~

**这份设计恰好修掉了 Java 的缺陷 1、2、3**：结构化、可逆、来源可追溯、可物理删除。

### 已留好的接入点（关键）

~~~python
# src/gogo_agent/context/input.py:55-57
def __init__(self, request: RequestContext, *, timezone_name="Asia/Shanghai",
             clock=..., preferences: Callable[[], list[dict]] = lambda: [], ...):

# src/gogo_agent/chat/executor.py:224
context = ModelInputContext(request, preferences=lambda: [...])
~~~

`preferences` 是**可注入回调**，每轮模型调用前调用一次，结果注入 `saved_preferences` 消息（`input.py:87-91`）。**这就是 ReMe 的天然接入点——不需要改 middleware。**

### 缺的

| 缺口 | 说明 |
|---|---|
| 语义召回 | SQLite 只能按 4 个固定 key 全量取；Java 那套「用户这次问酒店就只召回酒店偏好」的能力没有 |
| 对话自动积累 | Python **无任何** AI 写入入口（这是优点也是缺点：安全，但用户每次都要手动设） |
| `reme` 依赖 | 项目 venv 当前 `import reme` → `ModuleNotFoundError`（**实测**） |

---

## 三、关键发现：agentscope 已内置 ReMeMiddleware

~~~text
.venv/lib/python3.12/site-packages/agentscope/middleware/_longterm_memory/
├── _agentic_memory/    AgenticMemoryMiddleware
├── _mem0/              Mem0Middleware
└── _reme/              ReMeMiddleware     ← 已存在
~~~

~~~python
# agentscope/middleware/_longterm_memory/__init__.py
from ._agentic_memory import AgenticMemoryMiddleware
from ._mem0 import Mem0Middleware
from ._reme import ReMeMiddleware
~~~

### 它的行为契约（读 `_reme/_middleware.py`）

| 项 | 行为 |
|---|---|
| 运行形态 | **进程内嵌**（`from reme import ReMe`，`_build_app()` 懒加载），**不需要单独跑 ReMe 服务** |
| 写入 | `on_reply` 钩子自动写回（`auto_memory` job），**所有 mode 都写**；**没有手动 add 工具** |
| 检索 | `mode` 三选一：`static_control`（自动召回注入）/ `agent_control`（暴露 `memory_search` 工具）/ `both` |
| 构造参数 | `workspace_dir` + `Parameters(chat_model, embedding_model, mode, top_k)` |
| 依赖 | `pip install "agentscope[memory-reme]"`（`Requires-Dist: reme-ai>=0.4.0.6`） |

### 一个必须先解决的安全问题

`_middleware.py` 模块 docstring 原文：

~~~text
ReMe scopes writes by session_id, which is read from
agent.state.session_id at hook time ... search runs over the whole workspace.
~~~

而 `_search()`（第 475 行）直接 `run_job("search", query=query, limit=top_k)`，**不传任何用户过滤**。

**本机实测复现**（`/tmp/reme-probe`，ReMe v0.4.2.0）：

~~~text
写入 digest/users/u001/prefs.md  → 只坐国航
写入 digest/users/u002/prefs.md  → 只坐东航

POST /search {"query":"只坐哪家航空","top_k":5}
→ digest/users/u001/prefs.md  1.122
→ digest/users/u002/prefs.md  1.122     ← 串了
~~~

**必须显式过滤**，且实测只有 `prefix` 有效：

~~~json
{"query":"航空","top_k":5,"search_filter":{"prefix":"digest/users/u001"}}
→ 只返回 digest/users/u001/prefs.md   （有效）

{"query":"航空","top_k":5,"search_filter":{"paths":["digest/users/u001"]}}
→ 0 结果                              （无效，不是前缀匹配）
~~~

**因此：不能直接裸用 ReMeMiddleware。** 要么包一层加用户过滤，要么用「每用户独立 workspace」。

---

## 四、复刻方案：三个选项

### 方案 A：独立 ReMe 服务 + 自建 HTTP 客户端（推荐给生产）

~~~text
gogo-agent ──HTTP──> ReMe (127.0.0.1:2333, Docker)
    │                    └── workspace/digest/users/{user_hash}/*.md
    └── preferences() 回调合并两个来源
~~~

**隔离做法**：每个用户一个目录 `digest/users/{sha256(user_id)[:16]}/`，检索时带 `search_filter.prefix`。

| 优点 | 缺点 |
|---|---|
| 用户隔离可控、可测试 | 多一个进程（实测 RSS 48–113 MB） |
| 进程崩溃不影响 agent | 需要维护 Docker/venv |
| 可独立升级 ReMe | 一次 HTTP 往返 |

**本机已完整验证这条链路**（见第八节）。

### 方案 B：进程内嵌 ReMeMiddleware（最省事，但有隔离缺陷）

~~~python
# chat/executor.py 装配处
from agentscope.middleware import ReMeMiddleware

mw = ReMeMiddleware(workspace_dir="~/.gogo-agent/reme",
                    parameters=ReMeMiddleware.Parameters(mode="static_control"))
agent = Agent(..., middlewares=[*self._input_middlewares(request, trace), mw])
~~~

| 优点 | 缺点 |
|---|---|
| 零部署、零额外进程 | **搜索跨用户**（源码明示 + 实测）→ **多租户不可用** |
| agentscope 原生接口 | 需自己 fork/覆盖 `_search` 才能加过滤 |
| 写入自动 | `on_reply` 写回可能拖慢回复 |

**结论：单用户/演示可用，生产不可用。** 除非改 `_search`。

### 方案 C：只补「语义召回」，不引入自动写入（最保守）

保留 SQLite 为唯一写入权威，ReMe 只做**只读检索层**：

~~~python
# 新建 src/gogo_agent/context/reme.py
async def semantic_preferences(user_id: str, query: str) -> list[dict]:
    """从 ReMe 召回与本轮问题相关的历史偏好；不写。"""
    prefix = "digest/users/" + user_dir_name(user_id)
    ...  # POST /search with search_filter.prefix
~~~

~~~python
# chat/executor.py:224 改造
context = ModelInputContext(request, preferences=lambda: [
    *store.list(request.user_id),              # 显式设定（权威）
    *semantic_extra,                           # 语义召回（增强）
])
~~~

| 优点 | 缺点 |
|---|---|
| 保留「用户显式设定」的绝对权威 | 仍无自动积累 |
| 无数据串户风险（只读 + prefix） | 需要自己写检索 |
| 可灰度：ReMe 挂了自动降级 | — |

---

## 五、推荐路径

~~~text
第 1 步  方案 C 落地（语义召回只读层）        ← 风险最低，1 个新文件 + 1 处改
第 2 步  验证召回质量（用真实偏好数据）        ← 决定是否值得继续
第 3 步  再评估是否要「自动积累」              ← 这一步才需要 ReMeMiddleware 或 auto_memory
~~~

**理由**：Java 那套「AI 自动记录」正是它的缺陷来源（来源不明、不可撤销、有损往返）。**先只复刻「语义召回」这一项净收益，把「自动写入」当作独立决策。**

---

## 六、如果要做自动积累，隔离方案

不用共享 `workspace_dir`，而是**每用户一个 ReMe 实例**：

~~~python
def middleware_for(user_id: str) -> ReMeMiddleware:
    return ReMeMiddleware(
        workspace_dir="~/.gogo-agent/reme/" + user_dir_name(user_id),
        parameters=ReMeMiddleware.Parameters(mode="static_control", top_k=20),
    )
~~~

代价：每用户一个内嵌 ReMe app（实测 48–113 MB RSS）。**10 个并发用户就吃掉 1 GB——这台 1.8 GiB 的服务器装不下。**

所以自动积累在**当前服务器规格下不可行**，除非：
- 改成方案 A（单进程服务 + prefix 过滤），内存只算一份
- 或等到能扩容

---

## 七、部署与运维

### 服务器实测容量（2026-10-27 清理后）

~~~text
磁盘：40G 总 / 15G 用 / 23G 可用（40%）      ← 清理前 16G 可用
内存：1.8 GiB 总 / 1031 MiB 可用
Swap：1.0 GiB 总 / 317 MiB 可用
~~~

**本次清理已执行（非删除，是 truncate + 停无用进程）**：

| 动作 | 效果 |
|---|---|
| truncate `www.shixingdafa.xyz.log`（6.97 GB） | 恢复 6.5 GB |
| truncate `www.shixingdafa.xyz.error.log`（405 MB） | 恢复 387 MB |
| 新增 `/etc/logrotate.d/nginx-gogo`（daily / rotate 7 / maxsize 500M / copytruncate） | 防止复发 |
| `pg_ctl stop -m fast` 停 PG18 | 释放端口 5432；数据完整保留（PG_VERSION + 67 MB data） |

**PG18 停止的依据**：`douya` 库虽有 8 张表（`chat_message` 17 行、`chat_session` 7 行、`checkpoints` 53 行等），但**最后写入是 2026-03-25**，且无任何应用进程连接（`pg_stat_activity` 除 psql 外零客户端，`ss` 无 ESTABLISHED 到 5432）。**数据没删，随时可 `pg_ctl start`。**

### ReMe 部署建议

| 项 | 值 |
|---|---|
| 形态 | Docker：`ghcr.io/agentscope-ai/reme:main`（服务器实测可达，0.49s 返回 401 匿名探测） |
| 端口 | `127.0.0.1:2333`（仅本机） |
| 数据卷 | `/www/dk_project/reme/workspace` → `/data` |
| 资源估算 | ~100 MB RSS，磁盘 ∝ 记忆条数（4 个 md 文件 = 48 KB） |
| 回滚 | `docker stop reme` 即可，不污染现有 venv |

**注意**：服务器 Python 是 3.6.8，ReMe 要求 3.11+。docker 路线绕开这个问题；若要源码部署，`dnf` 可装 `python3.11.x86_64`（3.11.13）。

---

## 八、验证边界

### 已实测（本机）

- ReMe 0.4.2.0 完整跑通：`write` → 自动索引 → `search` 命中中文内容（score 1.1096 / 1.3973）
- 自动索引延迟 ~8 秒（watch debounce 5s）
- RSS 48–113 MB；workspace 48 KB；venv 221 MB
- **跨用户串数据已复现**；`prefix` 过滤有效，`paths` 无效
- 安装三个坑：① `reme-ai` 不自动带 `agentscope` ② `agentscope` 版本 2.0.8 与 ReMe 声明的 2.0.10 **实测兼容**（write/search/reindex/health 全通） ③ `fastmcp` 报错真因是 `mcp` 版本太旧，需 `mcp>=2.0`

### 未验证

| 项 | 说明 |
|---|---|
| `ReMeMiddleware` 实际运行 | 只读了源码，**没有实例化跑过** |
| `auto_memory`（LLM 抽取） | 未配 LLM key，未测 |
| 服务器部署 | **未执行** |
| 多用户并发下的内存实测 | 未测 |
| 中文分词的召回质量 | 仅 2 条样本，不足以判断 |

### 依赖条件

1. **必须先有 `reme-ai` 依赖**：`pip install "agentscope[memory-reme]"`
2. **必须先配 LLM** 才能用 `auto_memory` 自动写入；纯 BM25 检索不需要
3. **必须先解决用户隔离**，否则不可上多租户

---

## 九、变式练习（推导）

**题目**：按方案 C 落地后，用户 A 与用户 B 在同一台服务器用同一份 ReMe workspace。用户 A 说「我不喜欢红眼航班」，B 从不说话。此时问 B「我的航班偏好是什么」，B 会看到 A 的偏好么？

> **参考答案**：不会——**但原因不是 ReMe 天然隔离，而是方案 C 里我们强制加了 `search_filter.prefix`**。
>
> 推导链：`semantic_preferences(user_id)` 按 `sha256(user_id)[:16]` 算出 prefix → 传给 `/search` 的 `search_filter.prefix` → ReMe 只在该目录下检索（本机实测有效）。B 的目录是空的 → 返回 0 条。
>
> **再改一个条件**：如果绕过 `semantic_preferences` 直接调 `/search` 不传 filter，**A 的偏好会返回给 B**（本机已复现 u001/u002 串数据）。这说明隔离不在 ReMe，在调用方的每一次调用——**任何一处漏传 filter 都是数据泄露**。

---

## 十、遗留

| # | 项 |
|---|---|
| 1 | `PreferenceStore` 只有 4 个固定 key（transport/hotel/schedule/other），Java 有 14+ 字段。扩字段是独立决策 |
| 2 | Python 无 `LongTermMemory` 抽象（`agentscope.memory` 模块不存在），**只能走 middleware 路线**，不能照搬 Java 的 `.longTermMemory(...)` |
| 3 | `docker images` 尚有 606 MB 可回收（neo4j 607MB / chroma 580MB / minio 175MB / pgvector 440MB 均未运行） |
| 4 | `/tmp/reme-probe`（225 MB）保留未删，可直接复现 |
