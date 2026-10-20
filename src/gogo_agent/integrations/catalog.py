"""048 旅行 Skill/MCP 能力清单：源项目（Java）当前实际启用的外部能力，以及它们在 Python 中的去向。

清单只记录 2026-10 核对 Java 基线源码得到的事实，每条都带 `文件:行号` 证据。
“资源文件存在”不等于“已启用”：只有在某个 Agent 的 Toolkit/SkillBox 里注册（`registered_agents` 非空）才算启用。
"""

from __future__ import annotations

from collections import defaultdict
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

_JAVA = "src/main/java/com/gogo/travel/"
_RES = "src/main/resources/"
_PLAN = f"{_JAVA}agent/ItineraryPlanAgent.java"
_BOOK = f"{_JAVA}agent/BookingAgent.java"
_INFO = f"{_JAVA}agent/InfoAgent.java"
_LIVE = f"{_JAVA}agent/tools/DestinationLiveTools.java"
_YML = f"{_RES}application.yml"
_TUNIU_SKILL = f"{_RES}skills/tuniu-cli/SKILL.md"


class EntryKind(StrEnum):
    """外部能力的接入形态。"""

    NATIVE_TOOL = "native_tool"
    SKILL_CLI = "skill_cli"
    SKILL_CURL = "skill_curl"
    MCP_HTTP = "mcp_http"
    MCP_STDIO = "mcp_stdio"


class AccessMode(StrEnum):
    """能力读写属性。"""

    READ_ONLY = "read_only"
    WRITE = "write"
    READ_WRITE = "read_write"


class CredentialKind(StrEnum):
    """凭证类型与归属。"""

    NONE = "none"
    USER_API_KEY = "user_api_key"
    USER_OAUTH_TOKEN = "user_oauth_token"
    SERVICE_API_KEY = "service_api_key"
    SERVICE_ENDPOINT_SECRET = "service_endpoint_secret"


class Decision(StrEnum):
    """Python 迁移决定。"""

    MIGRATE_STAGE_E = "migrate_stage_e"
    DEFER_LATER = "defer_later"
    NOT_ENABLED = "not_enabled"


class CatalogEntry(BaseModel):
    """清单中的一条外部能力入口；`behavior` 相同的条目争夺同一个权威入口。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_id: str = Field(..., min_length=1, description="条目唯一标识，如 tuniu.flight.search")
    behavior: str = Field(..., min_length=1, description="行为标识，如 flight.search；同一行为只能有一个权威入口")
    vendor: str = Field(..., description="供应商或服务名称")
    purpose: str = Field(..., description="用途的中文描述")
    entry_kind: EntryKind = Field(..., description="接入形态：原生工具、Skill(CLI/curl) 或 MCP(HTTP/stdio)")
    entry_point: str = Field(..., description="调用入口，如命令模板、工具名或配置键")
    access: AccessMode = Field(..., description="读写属性：read_only、write 或 read_write")
    credential: CredentialKind = Field(..., description="凭证类型与归属")
    credential_note: str = Field(..., description="凭证的注入方式与持有者说明；不记录任何密钥值")
    registered_agents: tuple[str, ...] = Field(
        ..., description="Java 中实际注册该能力的 Agent；为空表示源项目当前未启用",
    )
    decision: Decision = Field(..., description="迁移决定：本阶段迁移、后续阶段再迁移或因未启用而不迁移")
    authoritative: bool = Field(..., description="是否为该行为计划中的唯一权威调用入口")
    python_planning_allowed: bool = Field(..., description="Python 行程规划智能体是否可使用；写入类必须为 false")
    python_module: str | None = Field(default=None, description="Python 中承接的模块；未迁移则为空")
    evidence: tuple[str, ...] = Field(..., min_length=1, description="Java 基线证据，格式为 文件:行号 加简述")
    note: str = Field(default="", description="补充说明与风险")

    @property
    def enabled(self) -> bool:
        """源项目是否已把该能力注册给某个 Agent。"""
        return bool(self.registered_agents)


def _tuniu(
    suffix: str, behavior: str, purpose: str, command: str, *, access: AccessMode,
    decision: Decision, module: str | None = None, planning: bool, line: str, note: str = "",
) -> CatalogEntry:
    """途牛条目共享的注册事实：规划与预订 Agent 都注册了 tuniu-cli，凭证都是用户级 API Key。"""
    return CatalogEntry(
        entry_id=f"tuniu.{suffix}", behavior=behavior, vendor="途牛", purpose=purpose,
        entry_kind=EntryKind.SKILL_CLI, entry_point=command, access=access,
        credential=CredentialKind.USER_API_KEY,
        credential_note="用户级 API Key；Hook 以 env TUNIU_API_KEY 注入命令，provider=tuniu-cli",
        registered_agents=("ItineraryPlanAgent", "BookingAgent"), decision=decision, authoritative=True,
        python_planning_allowed=planning, python_module=module,
        evidence=(
            f"{_PLAN}:127 registerSkill(tuniu-cli)", f"{_BOOK}:110 registerSkill(tuniu-cli)",
            f"{_JAVA}agent/hook/TuniuApiKeyHook.java:43 env TUNIU_API_KEY 注入", line,
        ),
        note=note,
    )


_WRITE_NOTE = (
    "规划 Agent 在 Java 中同样注册了该 Skill 且 shell 白名单含 tuniu（ItineraryPlanAgent.java:131），"
    "‘规划阶段不得下单’只靠提示词（itinerary-plan-agent-system.md:61）约束；Python 规划路径不得暴露。"
)

CATALOG: tuple[CatalogEntry, ...] = (
    _tuniu("flight.search", "flight.search", "机票低价搜索", "tuniu call flight searchLowestPriceFlight -a '<JSON>'",
           access=AccessMode.READ_ONLY, decision=Decision.MIGRATE_STAGE_E, module="gogo_agent.integrations.tuniu",
           planning=True, line=f"{_TUNIU_SKILL}:41 意图速查表；{_JAVA}agent/hook/SearchResultCaptureHook.java:51"),
    _tuniu("train.search", "train.search", "火车票车次搜索", "tuniu call train searchLowestPriceTrain -a '<JSON>'",
           access=AccessMode.READ_ONLY, decision=Decision.MIGRATE_STAGE_E, module="gogo_agent.integrations.tuniu",
           planning=True, line=f"{_TUNIU_SKILL}:43 意图速查表；{_JAVA}agent/hook/SearchResultCaptureHook.java:52"),
    _tuniu("hotel.search", "hotel.search", "酒店搜索", "tuniu call hotel tuniuHotelSearch -a '<JSON>'",
           access=AccessMode.READ_ONLY, decision=Decision.MIGRATE_STAGE_E, module="gogo_agent.integrations.tuniu",
           planning=True, line=f"{_TUNIU_SKILL}:42 意图速查表；{_JAVA}agent/hook/SearchResultCaptureHook.java:53"),
    _tuniu("flight.detail", "flight.detail", "舱位详情（下单前置，取 cabinPriceId）", "tuniu call flight multiCabinDetails",
           access=AccessMode.READ_ONLY, decision=Decision.DEFER_LATER, planning=False,
           line=f"{_TUNIU_SKILL}:63 服务工具链路", note="只服务下单前置参数，规划不需要；阶段 F 随预订迁移。"),
    _tuniu("train.detail", "train.detail", "车次详情（下单前置，取 resId）", "tuniu call train queryTrainDetail",
           access=AccessMode.READ_ONLY, decision=Decision.DEFER_LATER, planning=False,
           line=f"{_TUNIU_SKILL}:65 服务工具链路", note="只服务下单前置参数；阶段 F 随预订迁移。"),
    _tuniu("hotel.detail", "hotel.detail", "酒店详情（下单前置，取 preBookParam）", "tuniu call hotel tuniuHotelDetail",
           access=AccessMode.READ_ONLY, decision=Decision.DEFER_LATER, planning=False,
           line=f"{_TUNIU_SKILL}:64 服务工具链路", note="只服务下单前置参数；阶段 F 随预订迁移。"),
    _tuniu("flight.order", "flight.order", "机票下单", "tuniu call flight saveOrder",
           access=AccessMode.WRITE, decision=Decision.DEFER_LATER, planning=False,
           line=f"{_RES}prompts/booking-agent-system.md:24 下单命令", note=_WRITE_NOTE),
    _tuniu("flight.cancel", "flight.cancel", "机票取消", "tuniu call flight cancelOrder",
           access=AccessMode.WRITE, decision=Decision.DEFER_LATER, planning=False,
           line=f"{_RES}prompts/booking-agent-system.md:41 取消只能走工具", note=_WRITE_NOTE),
    _tuniu("train.order", "train.order", "火车票下单", "tuniu call train bookTrain",
           access=AccessMode.WRITE, decision=Decision.DEFER_LATER, planning=False,
           line=f"{_RES}prompts/booking-agent-system.md:25 下单命令", note=_WRITE_NOTE),
    _tuniu("train.cancel", "train.cancel", "火车票取消", "tuniu call train cancelOrder",
           access=AccessMode.WRITE, decision=Decision.DEFER_LATER, planning=False,
           line=f"{_TUNIU_SKILL}:65 服务工具链路", note=_WRITE_NOTE),
    _tuniu("hotel.order", "hotel.order", "酒店下单", "tuniu call hotel tuniuHotelCreateOrder",
           access=AccessMode.WRITE, decision=Decision.DEFER_LATER, planning=False,
           line=f"{_RES}prompts/booking-agent-system.md:26 下单命令", note=_WRITE_NOTE),
    CatalogEntry(
        entry_id="wttr.weather.short", behavior="weather.short_range", vendor="wttr.in",
        purpose="今天起 3 天内的实况与逐日预报（免费首选）", entry_kind=EntryKind.NATIVE_TOOL,
        entry_point="query_weather(city, date?)", access=AccessMode.READ_ONLY, credential=CredentialKind.NONE,
        credential_note="无需凭证", registered_agents=("ItineraryPlanAgent", "InfoAgent"),
        decision=Decision.MIGRATE_STAGE_E, authoritative=True, python_planning_allowed=True,
        python_module="gogo_agent.integrations.destination_live",
        evidence=(f"{_LIVE}:52 WEATHER_FORECAST_DAYS=2", f"{_LIVE}:83 @Tool query_weather",
                  f"{_PLAN}:70 注册 destinationLiveTools", f"{_INFO}:57 注册 destinationLiveTools"),
        note="超出窗口时返回 beyond_range，由模型改调天气 MCP；Java 无代码层强制路由。",
    ),
    CatalogEntry(
        entry_id="newsdata.news", behavior="news.destination", vendor="NewsData.io",
        purpose="目的地交通管制、活动、安全等资讯", entry_kind=EntryKind.NATIVE_TOOL,
        entry_point="query_destination_news(city, topic?)", access=AccessMode.READ_ONLY,
        credential=CredentialKind.SERVICE_API_KEY,
        credential_note="服务级 Key，配置项 app.news-api-key；未配置时降级为 available=false",
        registered_agents=("ItineraryPlanAgent", "InfoAgent"), decision=Decision.MIGRATE_STAGE_E,
        authoritative=True, python_planning_allowed=True, python_module="gogo_agent.integrations.destination_live",
        evidence=(f"{_LIVE}:252 @Tool query_destination_news", f"{_YML}:73 news-api-key（该文件含明文，不得照搬）",
                  f"{_PLAN}:70 注册 destinationLiveTools"),
        note="Java 失败时 detail 回显异常文本，而请求 URL 含 apikey，存在泄露风险；Python 只给错误类型名。",
    ),
    CatalogEntry(
        entry_id="weather.mcp.long", behavior="weather.long_range", vendor="阿里云云市场天气 MCP",
        purpose="15 日与 40 日天气预报", entry_kind=EntryKind.MCP_HTTP,
        entry_point="MCP 工具 城市15日预报 / 城市40日预报（Streamable HTTP）", access=AccessMode.READ_ONLY,
        credential=CredentialKind.SERVICE_ENDPOINT_SECRET,
        credential_note="服务级 AppCode，内嵌在 app.weather-mcp.endpoint 的 URL 中，日志与异常不得回显 URL",
        registered_agents=("ItineraryPlanAgent", "InfoAgent"), decision=Decision.MIGRATE_STAGE_E,
        authoritative=True, python_planning_allowed=True, python_module="gogo_agent.integrations.weather_mcp",
        evidence=(f"{_JAVA}config/WeatherMcpConfig.java:54 streamableHttpTransport", f"{_YML}:79 enabled-tools",
                  f"{_PLAN}:73 注册 weatherMcpClient", f"{_INFO}:58 注册 weatherMcpClient"),
        note="收费服务；按量计费，因此只在免费窗口之外才调用。",
    ),
    CatalogEntry(
        entry_id="weather.mcp.extras", behavior="weather.alert_air_hourly", vendor="阿里云云市场天气 MCP",
        purpose="天气实况、预警、空气实况、24 小时预报", entry_kind=EntryKind.MCP_HTTP,
        entry_point="MCP 工具 城市天气实况 / 城市天气预警 / 城市空气实况 / 城市24小时预报", access=AccessMode.READ_ONLY,
        credential=CredentialKind.SERVICE_ENDPOINT_SECRET, credential_note="同 weather.mcp.long，同一个端点",
        registered_agents=("ItineraryPlanAgent", "InfoAgent"), decision=Decision.DEFER_LATER,
        authoritative=True, python_planning_allowed=True, python_module=None,
        evidence=(f"{_YML}:79 enabled-tools", f"{_PLAN}:73 注册 weatherMcpClient"),
        note="047 只实现预报调用；这四个工具已被 WeatherMcpService 的白名单参数覆盖，但没有专门方法，也未验证其真实参数。",
    ),
    CatalogEntry(
        entry_id="orizn.visa", behavior="visa.check", vendor="Orizn Visa MCP",
        purpose="护照与目的地签证要求查询", entry_kind=EntryKind.MCP_STDIO,
        entry_point="npx -y orizn-visa-mcp；工具 quick_visa_check / check_visa_requirement", access=AccessMode.READ_ONLY,
        credential=CredentialKind.SERVICE_API_KEY, credential_note="服务级 Key，经子进程环境变量 ORIZN_API_KEY 注入",
        registered_agents=("ItineraryPlanAgent", "InfoAgent"), decision=Decision.DEFER_LATER,
        authoritative=True, python_planning_allowed=True, python_module=None,
        evidence=(f"{_JAVA}config/OriznVisaMcpConfig.java:91 stdioTransport", f"{_YML}:96 enabled-tools",
                  f"{_PLAN}:74 注册 oriznVisaMcpClient", f"{_INFO}:59 注册 oriznVisaMcpClient"),
        note="阶段 E 没有承接编号；可复用 weather_mcp 的 MCP 封装模式。application.yml:91 含明文默认 Key，不得照搬。",
    ),
    CatalogEntry(
        entry_id="flightmanager.search", behavior="flight.search", vendor="航班管家（huoli）",
        purpose="机票搜索与查询", entry_kind=EntryKind.SKILL_CURL,
        entry_point="curl POST https://fly.huoli.com/mcp/flight_server（JSON-RPC）", access=AccessMode.READ_ONLY,
        credential=CredentialKind.USER_API_KEY,
        credential_note="用户级 Key；占位符 ${FLIGHT_API_KEY} 由 FlightApiKeyHook 替换", registered_agents=(),
        decision=Decision.NOT_ENABLED, authoritative=False, python_planning_allowed=False,
        evidence=(f"{_RES}skills/flight-manager/SKILL.md:2 资源存在", f"{_PLAN}:127-128 只注册 tuniu-cli 与 html-plan",
                  f"{_JAVA}agent/tools/ApiKeyTools.java:60-91 flight Key 工具已注释"),
        note="FlightApiKeyHook 仍在两个 Agent 的 Hook 列表中（Plan:106，Booking:95），但没有 Skill 引导模型发出对应命令。",
    ),
    CatalogEntry(
        entry_id="flightmanager.order", behavior="flight.order", vendor="航班管家（huoli）",
        purpose="机票下单、退改签、发票", entry_kind=EntryKind.SKILL_CURL,
        entry_point="curl POST https://fly.huoli.com/mcp/flight_server（uni_order_create 等）", access=AccessMode.WRITE,
        credential=CredentialKind.USER_API_KEY, credential_note="同 flightmanager.search", registered_agents=(),
        decision=Decision.NOT_ENABLED, authoritative=False, python_planning_allowed=False,
        evidence=(f"{_RES}skills/flight-manager/SKILL.md:2 资源存在", f"{_BOOK}:110 预订 Agent 只注册 tuniu-cli"),
    ),
    CatalogEntry(
        entry_id="rollinggo.search", behavior="hotel.search", vendor="Rolling Go",
        purpose="酒店搜索与房型价格", entry_kind=EntryKind.SKILL_CLI, entry_point="rgh <子命令>",
        access=AccessMode.READ_ONLY, credential=CredentialKind.USER_OAUTH_TOKEN,
        credential_note="用户级 OAuth Token，Redis 为权威源，本地 HOME 目录只是投影", registered_agents=(),
        decision=Decision.NOT_ENABLED, authoritative=False, python_planning_allowed=False,
        evidence=(f"{_RES}skills/rolling-go-hotel/SKILL.md:2 资源存在", f"{_PLAN}:127-128 未注册该 Skill",
                  f"{_PLAN}:131 shell 白名单含 rgh", f"{_JAVA}agent/hook/RghUserIsolationHook.java:142 命令含 rgh 才介入"),
        note="shell 白名单与用户隔离 Hook 是残留；提示词与已注册 Skill 都不引导使用 rgh（prompts 目录无 rgh 引用），需运行调用日志才能证明从未触发。",
    ),
    CatalogEntry(
        entry_id="rollinggo.order", behavior="hotel.order", vendor="Rolling Go",
        purpose="酒店预订与订单", entry_kind=EntryKind.SKILL_CLI, entry_point="rgh <预订子命令>",
        access=AccessMode.WRITE, credential=CredentialKind.USER_OAUTH_TOKEN, credential_note="同 rollinggo.search",
        registered_agents=(), decision=Decision.NOT_ENABLED, authoritative=False, python_planning_allowed=False,
        evidence=(f"{_RES}skills/rolling-go-hotel/SKILL.md:83 预订与订单", f"{_BOOK}:113 预订 shell 白名单不含 rgh"),
    ),
    CatalogEntry(
        entry_id="flyai.flight.search", behavior="flight.search", vendor="飞猪 FlyAI",
        purpose="机票搜索（只读，无预订）", entry_kind=EntryKind.SKILL_CLI, entry_point="flyai CLI",
        access=AccessMode.READ_ONLY, credential=CredentialKind.SERVICE_API_KEY,
        credential_note="可选的服务级 Key（Skill 文档称无 Key 也可试用）", registered_agents=(),
        decision=Decision.NOT_ENABLED, authoritative=False, python_planning_allowed=False,
        evidence=(f"{_RES}skills/flyai/SKILL.md:2 资源存在", f"{_PLAN}:127-128 未注册该 Skill"),
        note="与途牛的机票/火车搜索重叠；SKILL.md:57 含明文 Key 示例，不得照搬。",
    ),
    CatalogEntry(
        entry_id="flyai.train.search", behavior="train.search", vendor="飞猪 FlyAI",
        purpose="火车票搜索（只读，无预订）", entry_kind=EntryKind.SKILL_CLI, entry_point="flyai CLI",
        access=AccessMode.READ_ONLY, credential=CredentialKind.SERVICE_API_KEY,
        credential_note="同 flyai.flight.search", registered_agents=(), decision=Decision.NOT_ENABLED,
        authoritative=False, python_planning_allowed=False,
        evidence=(f"{_RES}skills/flyai/SKILL.md:2 资源存在", f"{_PLAN}:127-128 未注册该 Skill"),
    ),
)


def validate_catalog(entries: tuple[CatalogEntry, ...] | list[CatalogEntry] = CATALOG) -> list[str]:
    """校验清单；返回问题列表，空列表表示通过。"""
    problems: list[str] = []
    seen: set[str] = set()
    by_behavior: dict[str, list[CatalogEntry]] = defaultdict(list)
    for entry in entries:
        if entry.entry_id in seen:
            problems.append(f"{entry.entry_id}: 条目标识重复")
        seen.add(entry.entry_id)
        by_behavior[entry.behavior].append(entry)

        if entry.enabled and entry.decision is Decision.NOT_ENABLED:
            problems.append(f"{entry.entry_id}: 已注册给 Agent，却标成未启用")
        if not entry.enabled:
            if entry.decision is not Decision.NOT_ENABLED:
                problems.append(f"{entry.entry_id}: 未注册给任何 Agent，不能标成“{entry.decision.value}”")
            if entry.authoritative:
                problems.append(f"{entry.entry_id}: 未启用的入口不能作为权威入口")
            if entry.python_module is not None:
                problems.append(f"{entry.entry_id}: 未启用的入口不能指向 Python 迁移模块")
        if entry.decision is Decision.MIGRATE_STAGE_E:
            if entry.access is not AccessMode.READ_ONLY:
                problems.append(f"{entry.entry_id}: 阶段 E 只迁移只读能力")
            if entry.python_module is None:
                problems.append(f"{entry.entry_id}: 标为本阶段迁移但没有 Python 模块")
        if entry.access is not AccessMode.READ_ONLY and entry.python_planning_allowed:
            problems.append(f"{entry.entry_id}: 写入类入口不得开放给行程规划智能体")

    for behavior, group in by_behavior.items():
        accesses = {entry.access for entry in group}
        if len(accesses) > 1:
            problems.append(f"{behavior}: 同一行为的读写属性不一致")
        authoritative = [entry.entry_id for entry in group if entry.authoritative]
        any_enabled = any(entry.enabled for entry in group)
        if any_enabled and len(authoritative) != 1:
            problems.append(f"{behavior}: 已启用行为必须恰有 1 个权威入口，实际 {len(authoritative)} 个 {authoritative}")
        if not any_enabled and authoritative:
            problems.append(f"{behavior}: 没有任何已启用入口，却存在权威入口 {authoritative}")
    return problems
