# 048 旅行 Skill/MCP 能力清单

这份清单回答一个问题：Java 源项目**当前实际启用**了哪些外部旅行能力，每个行为在 Python 里只留哪一个入口。数据在 [`catalog.py`](../../src/gogo_agent/integrations/catalog.py) 的 `CATALOG`，由 `validate_catalog()` 校验；本文是同一份数据加核查证据。

核查对象：`/Users/shenmingjie/workSpace/LLMentor-teach/gogo-agent`（下文路径均相对该 Java 工程根目录）。核查日期 2026-10-11，只读源码，没有运行 Java 服务，因此**“启用”指代码里注册给了 Agent，不等于线上调用过**。

## 结论

1. **已启用并在本阶段迁移（只读）**：途牛机票/火车/酒店**搜索**、wttr.in 短期天气、NewsData 资讯、天气 MCP 的 15/40 日预报。入口分别落在 [`tuniu.py`](../../src/gogo_agent/integrations/tuniu.py)、[`destination_live.py`](../../src/gogo_agent/integrations/destination_live.py)、[`weather_mcp.py`](../../src/gogo_agent/integrations/weather_mcp.py)。
2. **已启用但本阶段不迁移**：途牛详情与下单/取消（属阶段 F 预订）、天气 MCP 的实况/预警/空气/24 小时、Orizn 签证 MCP（阶段 E 没有承接编号）。
3. **源项目未启用，不迁移，不计入“已迁移”**：航班管家 `flight-manager`、Rolling Go `rolling-go-hotel`、飞猪 `flyai`。三者的 Skill 资源目录都存在，但没有任何 Agent 调用 `registerSkill`。
4. 每个**已启用**行为恰有一个权威入口；未启用入口不能是权威入口，也不能指向 Python 迁移模块（`validate_catalog()` 检查）。
5. **写入类入口一律不对 Python 行程规划智能体开放**（`python_planning_allowed=False`）。这一点比 Java 严格，见下文“发现的差异与风险”。

## 清单

“注册到的 Agent”指 Java 里实际把该能力挂到 Toolkit/SkillBox 的 Agent。证据见下一节。

| 条目 | 行为 | 用途 | 入口 | 读写 | 凭证 | 注册到的 Agent | 权威入口 | 决定 | Python 承接 |
|---|---|---|---|---|---|---|---|---|---|
| `tuniu.flight.search` | `flight.search` | 机票低价搜索 | `tuniu call flight searchLowestPriceFlight -a '<JSON>'` | 只读 | 用户级 API Key | ItineraryPlanAgent、BookingAgent | 是 | 本阶段迁移（阶段 E） | `tuniu.py` |
| `tuniu.train.search` | `train.search` | 火车票车次搜索 | `tuniu call train searchLowestPriceTrain -a '<JSON>'` | 只读 | 用户级 API Key | ItineraryPlanAgent、BookingAgent | 是 | 本阶段迁移（阶段 E） | `tuniu.py` |
| `tuniu.hotel.search` | `hotel.search` | 酒店搜索 | `tuniu call hotel tuniuHotelSearch -a '<JSON>'` | 只读 | 用户级 API Key | ItineraryPlanAgent、BookingAgent | 是 | 本阶段迁移（阶段 E） | `tuniu.py` |
| `tuniu.flight.detail` | `flight.detail` | 舱位详情（下单前置，取 cabinPriceId） | `tuniu call flight multiCabinDetails` | 只读 | 用户级 API Key | ItineraryPlanAgent、BookingAgent | 是 | 保留，后续迁移 | — |
| `tuniu.train.detail` | `train.detail` | 车次详情（下单前置，取 resId） | `tuniu call train queryTrainDetail` | 只读 | 用户级 API Key | ItineraryPlanAgent、BookingAgent | 是 | 保留，后续迁移 | — |
| `tuniu.hotel.detail` | `hotel.detail` | 酒店详情（下单前置，取 preBookParam） | `tuniu call hotel tuniuHotelDetail` | 只读 | 用户级 API Key | ItineraryPlanAgent、BookingAgent | 是 | 保留，后续迁移 | — |
| `tuniu.flight.order` | `flight.order` | 机票下单 | `tuniu call flight saveOrder` | 写入 | 用户级 API Key | ItineraryPlanAgent、BookingAgent | 是 | 保留，后续迁移 | — |
| `tuniu.flight.cancel` | `flight.cancel` | 机票取消 | `tuniu call flight cancelOrder` | 写入 | 用户级 API Key | ItineraryPlanAgent、BookingAgent | 是 | 保留，后续迁移 | — |
| `tuniu.train.order` | `train.order` | 火车票下单 | `tuniu call train bookTrain` | 写入 | 用户级 API Key | ItineraryPlanAgent、BookingAgent | 是 | 保留，后续迁移 | — |
| `tuniu.train.cancel` | `train.cancel` | 火车票取消 | `tuniu call train cancelOrder` | 写入 | 用户级 API Key | ItineraryPlanAgent、BookingAgent | 是 | 保留，后续迁移 | — |
| `tuniu.hotel.order` | `hotel.order` | 酒店下单 | `tuniu call hotel tuniuHotelCreateOrder` | 写入 | 用户级 API Key | ItineraryPlanAgent、BookingAgent | 是 | 保留，后续迁移 | — |
| `wttr.weather.short` | `weather.short_range` | 今天起 3 天内的实况与逐日预报（免费首选） | `query_weather(city, date?)` | 只读 | 无 | ItineraryPlanAgent、InfoAgent | 是 | 本阶段迁移（阶段 E） | `destination_live.py` |
| `newsdata.news` | `news.destination` | 目的地交通管制、活动、安全等资讯 | `query_destination_news(city, topic?)` | 只读 | 服务级 API Key | ItineraryPlanAgent、InfoAgent | 是 | 本阶段迁移（阶段 E） | `destination_live.py` |
| `weather.mcp.long` | `weather.long_range` | 15 日与 40 日天气预报 | MCP 工具 `城市15日预报` / `城市40日预报`（Streamable HTTP） | 只读 | 服务级 AppCode（内嵌端点 URL） | ItineraryPlanAgent、InfoAgent | 是 | 本阶段迁移（阶段 E） | `weather_mcp.py` |
| `weather.mcp.extras` | `weather.alert_air_hourly` | 天气实况、预警、空气实况、24 小时预报 | MCP 工具 `城市天气实况` / `城市天气预警` / `城市空气实况` / `城市24小时预报` | 只读 | 服务级 AppCode（内嵌端点 URL） | ItineraryPlanAgent、InfoAgent | 是 | 保留，后续迁移 | — |
| `orizn.visa` | `visa.check` | 护照与目的地签证要求查询 | `npx -y orizn-visa-mcp`；工具 `quick_visa_check` / `check_visa_requirement` | 只读 | 服务级 API Key | ItineraryPlanAgent、InfoAgent | 是 | 保留，后续迁移 | — |
| `flightmanager.search` | `flight.search` | 机票搜索与查询 | `curl POST https://fly.huoli.com/mcp/flight_server`（JSON-RPC） | 只读 | 用户级 API Key | **无（未启用）** | 否 | 未启用，不迁移 | — |
| `flightmanager.order` | `flight.order` | 机票下单、退改签、发票 | 同上（`uni_order_create` 等） | 写入 | 用户级 API Key | **无（未启用）** | 否 | 未启用，不迁移 | — |
| `rollinggo.search` | `hotel.search` | 酒店搜索与房型价格 | `rgh <子命令>` | 只读 | 用户级 OAuth Token | **无（未启用）** | 否 | 未启用，不迁移 | — |
| `rollinggo.order` | `hotel.order` | 酒店预订与订单 | `rgh <预订子命令>` | 写入 | 用户级 OAuth Token | **无（未启用）** | 否 | 未启用，不迁移 | — |
| `flyai.flight.search` | `flight.search` | 机票搜索（只读，无预订） | `flyai` CLI | 只读 | 服务级 API Key（可选） | **无（未启用）** | 否 | 未启用，不迁移 | — |
| `flyai.train.search` | `train.search` | 火车票搜索（只读，无预订） | `flyai` CLI | 只读 | 服务级 API Key（可选） | **无（未启用）** | 否 | 未启用，不迁移 | — |

同一行为有多个候选入口的只有三组：`flight.search`（途牛、航班管家、飞猪）、`train.search`（途牛、飞猪）、`hotel.search` 与 `hotel.order`（途牛、Rolling Go）。每组里只有途牛已启用，因此权威入口是途牛。

## 核查证据（Java 文件:行号）

### 途牛已注册

- `src/main/java/com/gogo/travel/agent/ItineraryPlanAgent.java:127` `skillBox.registerSkill(skillRepository.getSkill("tuniu-cli"))`；同文件 `:128` 另注册 `html-plan`（渲染用，不是外部供应商）。
- `src/main/java/com/gogo/travel/agent/BookingAgent.java:110` 同样注册 `tuniu-cli`，且全工程 `getSkill(`/`registerSkill` 只有这三处（用 `rg "getSkill\(|registerSkill"` 核对），没有其他 Skill 被注册。
- 凭证：`agent/hook/TuniuApiKeyHook.java:43` 用 `env TUNIU_API_KEY=<key> <命令>` 注入；`agent/tools/ApiKeyTools.java:49-50` 的 provider 为 `tuniu-cli`；`:98`、`:123` 是启用中的 `check_tuniu_api_key`、`save_tuniu_api_key`。
- 命令与链路：`resources/skills/tuniu-cli/SKILL.md:41-43`（意图速查表）、`:63-65`（搜索 → 详情 → 下单 → 取消链路）；`agent/hook/SearchResultCaptureHook.java:51-53` 只把三个搜索命令的结果写入候选存储。
- 退出码：`resources/skills/tuniu-cli/references/common.md:80-89`。

### 天气、资讯、签证

- `agent/tools/DestinationLiveTools.java:52` `WEATHER_FORECAST_DAYS = 2`；`:83` 工具 `query_weather`；`:104-113` 日期晚于今天 + 2 天时只返回 `beyond_range=true`，**不发请求**；`:252` 工具 `query_destination_news`。
- 注册：`ItineraryPlanAgent.java:70`、`InfoAgent.java:57` 注册 `destinationLiveTools`。
- 天气 MCP：`config/WeatherMcpConfig.java:54` `streamableHttpTransport`；`resources/application.yml:76` 端点、`:79` 六个白名单工具；`ItineraryPlanAgent.java:73`、`InfoAgent.java:58` 注册并 `enableTools(...)`。
- 路由提示：`resources/prompts/itinerary-plan-agent-system.md:51` 要求超出 3 天时改用“城市15日预报”或“城市40日预报”。**路由发生在模型里**，代码里没有强制。
- 签证 MCP：`config/OriznVisaMcpConfig.java:91` stdio 传输 `npx -y orizn-visa-mcp`；`application.yml:96` 白名单 `quick_visa_check,check_visa_requirement`；`ItineraryPlanAgent.java:74`、`InfoAgent.java:59` 注册。

### 资源存在但未启用

- **航班管家**：`resources/skills/flight-manager/SKILL.md:2` 资源存在；上面三处注册里没有它。`agent/tools/ApiKeyTools.java:60-91` 的 `check_flight_api_key`、`save_flight_api_key` 整段被注释，只剩 `:47-48` 的 provider 登记。`agent/hook/FlightApiKeyHook.java:18,21` 仍在两个 Agent 的 Hook 列表中（`ItineraryPlanAgent.java:106`、`BookingAgent.java:95`），但它只在命令里同时含 `${FLIGHT_API_KEY}` 与 `fly.huoli.com` 时才动作，而没有任何已注册 Skill 会让模型写出这样的命令。
- **Rolling Go**：`resources/skills/rolling-go-hotel/SKILL.md:2` 资源存在，未注册。残留：`ItineraryPlanAgent.java:131` 的 shell 白名单含 `rgh`，`agent/hook/RghUserIsolationHook.java:142` 仍会对含 `rgh ` 的命令改写。`resources/prompts/` 下没有任何 `rgh` 引用。预订 Agent 的白名单（`BookingAgent.java:113`）不含 `rgh`。
- **飞猪 FlyAI**：`resources/skills/flyai/SKILL.md:2` 资源存在，未注册，也没有对应 Hook。

## 发现的差异与风险

1. **规划 Agent 在 Java 里并非真的只读**。它注册了整份 `tuniu-cli` Skill（文档里含 `saveOrder`、`bookTrain`、`cancelOrder`），`tuniu` 又在它的 shell 白名单里（`ItineraryPlanAgent.java:131`）；“规划阶段不得调用 booking 相关工具”只在提示词 `itinerary-plan-agent-system.md:61`。Python 在适配器策略里**显式拒绝**这四个写命令，并让清单把它们标成对规划不可用。
2. **Rolling Go 的 `rgh` 是残留而非启用**：白名单与隔离 Hook 还在，但 Skill 与提示词都不引导使用。是否曾被触发，只能靠 Java 线上调用日志证明，本次没有这份证据。
3. **明文密钥出现在 Java 仓库**：`resources/application.yml` 的 `:63`、`:73`、`:76`（含 AppCode 的端点）、`:91`、`:132`、`:135`，以及 `resources/skills/flyai/SKILL.md:57`。本次迁移**没有**抄录任何一处；这些值应视为已泄露并轮换。
4. **Java 失败时回显异常消息**（`DestinationLiveTools.java:216` 天气、`:338` 资讯的 `detail`），而资讯请求 URL 内含 `apikey`。Python 的 046 只返回错误类型名。
5. 源文章 047 里 `query_weather` 的提示写“改用 weather-mcp / maps_weather”，当前代码是“城市15日预报/城市40日预报”；以代码为准。

## 怎么再次校验

```bash
PYTHONPATH=src .venv/bin/python scripts/demo_046_058_integrations.py --case 048
.venv/bin/python -m pytest tests/test_046_049_058_integrations.py -q -k 048
```

`validate_catalog()` 的规则：条目标识唯一；已注册 ⇔ 决定不是“未启用”；未启用不能是权威入口、不能有 Python 模块；本阶段迁移的必须只读且有 Python 模块；写入类不得对规划开放；同一行为读写属性一致；已启用的行为恰有一个权威入口，没有已启用入口的行为不能有权威入口。逐步调试见[阶段 E 调试指南](阶段E-046-049-058-调试指南.md)。

## 未验证

没有运行 Java 服务，不能证明哪些能力在线上被模型真正调用过；天气 MCP 的真实工具参数与返回字段（源项目里没有样例）也没有核对。
