"""第三方鉴权总览（055）：凭证类型、集成清单与“这次供应商请求用谁的授权”的决定。

清单内容来自对 Java 源码的静态核对（证据写在每条 ``evidence``），不含任何密钥值。
``CredentialResolver.resolve`` 只回答授权归属，不返回密钥；取明文是 059 ``ApiKeyService.resolve_for_use``
等具体凭证存储的职责。解析规则只有一条：用户级 provider 只认当前用户自己的凭证，缺失就失败，
不回退到其他用户、管理员或服务级密钥；服务级密钥只用于清单里明确标为服务级的 provider。
"""

from collections.abc import Callable, Collection, Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from gogo_agent.security.errors import SecurityError

PROVIDER_UNKNOWN = "CREDENTIAL_PROVIDER_UNKNOWN"
USER_REQUIRED = "CREDENTIAL_USER_REQUIRED"
USER_NOT_CONFIGURED = "CREDENTIAL_USER_NOT_CONFIGURED"
SERVICE_NOT_CONFIGURED = "CREDENTIAL_SERVICE_NOT_CONFIGURED"


class CredentialError(SecurityError):
    """无法确定授权归属；错误文本不含密钥。"""


class CredentialKind(StrEnum):
    """凭证形态。"""

    SERVICE_API_KEY = "service_api_key"
    USER_API_KEY = "user_api_key"
    USER_OAUTH_TOKEN = "user_oauth_token"
    CLI_ENV_VAR = "cli_env_var"
    MCP_CREDENTIAL = "mcp_credential"


class CredentialHolder(StrEnum):
    """凭证持有者类别：服务端共用，还是某个用户自己。"""

    SERVICE = "service"
    USER = "user"


class Integration(BaseModel):
    """一个实际存在的第三方集成及其凭证归属。"""

    model_config = ConfigDict(frozen=True)

    provider: str = Field(..., description="集成标识，用户级凭证以它作为 (user_id, provider) 的 provider")
    display_name: str = Field(..., description="集成的中文名称")
    kind: CredentialKind = Field(..., description="凭证形态")
    holder: CredentialHolder = Field(..., description="凭证持有者类别：service 为服务端共用，user 为用户自己的")
    resources: str = Field(..., description="凭证可访问的供应商资源范围")
    lifecycle: str = Field(..., description="凭证的更新与过期方式")
    revocation: str = Field(..., description="凭证的撤销方式")
    enabled_in_java: bool = Field(..., description="Java 当前规划/预订/信息 Agent 是否实际注册了该集成的调用入口")
    evidence: str = Field(..., description="Java 源码中的静态证据（相对 com/gogo/travel 与 resources）")


INTEGRATIONS: tuple[Integration, ...] = (
    Integration(
        provider="tuniu-cli", display_name="途牛 CLI", kind=CredentialKind.CLI_ENV_VAR,
        holder=CredentialHolder.USER,
        resources="该用户途牛账号下的机票/酒店/火车票查询与下单，订单出现在用户自己的账号里",
        lifecycle="用户在途牛开放平台领取的长期 API Key；无自动刷新，过期由用户重新提供",
        revocation="用户在途牛平台作废 Key；本系统 ApiKeyService.revoke 清空密文",
        enabled_in_java=True,
        evidence="agent/ItineraryPlanAgent.java:127、agent/BookingAgent.java:110 注册 tuniu-cli Skill；"
                 "agent/hook/TuniuApiKeyHook.java 用 env TUNIU_API_KEY=<key> 前缀注入；agent/tools/ApiKeyTools.java 的 check/save_tuniu_api_key",
    ),
    Integration(
        provider="flight-manager", display_name="航班管家", kind=CredentialKind.USER_API_KEY,
        holder=CredentialHolder.USER,
        resources="该用户航班管家账号下的机票查询/预订/退改签/发票（Skill 说明）",
        lifecycle="用户在 h5.133.cn 领取的长期 API Key；无自动刷新",
        revocation="用户在航班管家平台作废 Key；本系统 ApiKeyService.revoke 清空密文",
        enabled_in_java=False,
        evidence="Skill 资源 skills/flight-manager 存在，但 agent/ItineraryPlanAgent.java:127-128、agent/BookingAgent.java:110 未注册；"
                 "ApiKeyTools 的 flight 工具已注释；FlightApiKeyHook 仍作为 Hook 挂在 Plan/Booking Agent 上",
    ),
    Integration(
        provider="rolling-go-hotel", display_name="Rolling Go 酒店（rgh CLI）", kind=CredentialKind.USER_OAUTH_TOKEN,
        holder=CredentialHolder.USER,
        resources="该用户 Rolling Go 账号下的酒店订单读取、预订与取消（OAuth scope）",
        lifecycle="rgh 经 OAuth PKCE 取得 Token，Redis 保存 30 天（Java RghTokenStore）；CLI 可能刷新，过期需重新授权",
        revocation="rgh logout：Java 同时清 Redis 与本地 Token 文件；供应商侧授权由用户在平台撤销",
        enabled_in_java=False,
        evidence="skills/rolling-go-hotel 存在，但 Plan/Booking 的 SkillBox 未注册；agent/hook/RghUserIsolationHook.java 仍作为 Hook 挂载，"
                 "ItineraryPlanAgent.java:131 的 shell 白名单含 rgh；运行时是否真的被调用未用调用证据确认",
    ),
    Integration(
        provider="weather-mcp", display_name="天气 MCP（云市场）", kind=CredentialKind.MCP_CREDENTIAL,
        holder=CredentialHolder.SERVICE,
        resources="天气预报查询；所有用户共用服务端的云市场 AppCode，只读",
        lifecycle="随云市场订阅；AppCode 内嵌在 MCP endpoint URL，由运维配置 WEATHER_MCP_ENDPOINT",
        revocation="运维在云市场重置 AppCode 并更新配置",
        enabled_in_java=True,
        evidence="config/WeatherMcpConfig.java:34-54 streamableHttpTransport(endpoint)；agent/InfoAgent.java:58、ItineraryPlanAgent.java:73 注册",
    ),
    Integration(
        provider="orizn-visa-mcp", display_name="Orizn 签证 MCP", kind=CredentialKind.MCP_CREDENTIAL,
        holder=CredentialHolder.SERVICE,
        resources="签证要求查询；服务端共用，只读；未配置 Key 时只暴露免费工具",
        lifecycle="长期 API Key，经 ORIZN_API_KEY 环境变量注入 npx 子进程；无自动刷新",
        revocation="运维在 visa.orizn.app 重置 Key 并更新配置",
        enabled_in_java=True,
        evidence="config/OriznVisaMcpConfig.java:50-91 stdioTransport(command, args, env)；InfoAgent.java:59、ItineraryPlanAgent.java:74 注册",
    ),
    Integration(
        provider="newsdata-io", display_name="NewsData.io 资讯", kind=CredentialKind.SERVICE_API_KEY,
        holder=CredentialHolder.SERVICE,
        resources="目的地资讯查询；服务端共用，只读，免费额度按服务整体计",
        lifecycle="长期 API Key，查询参数 apikey 传递；未配置时工具降级返回提示",
        revocation="运维在 newsdata.io 重置 Key 并更新配置 app.news-api-key",
        enabled_in_java=True,
        evidence="agent/tools/DestinationLiveTools.java:68-72、271-291；InfoAgent.java:57、ItineraryPlanAgent.java:70 注册",
    ),
    Integration(
        provider="llm-gateway", display_name="模型网关（tinno）", kind=CredentialKind.SERVICE_API_KEY,
        holder=CredentialHolder.SERVICE,
        resources="对话/意图/规划模型调用；所有用户共用服务端 Key，费用记在服务账号",
        lifecycle="长期 API Key，来自 agentscope.tinno.api-key / TINNO_API_KEY",
        revocation="运维在网关作废 Key 并更新配置",
        enabled_in_java=True,
        evidence="agent/config/ModelConfig.java:23-64；config/IntentRouterKnowledgeConfig.java:56-78",
    ),
    Integration(
        provider="dashscope-bailian", display_name="百炼/DashScope（向量与长期记忆）", kind=CredentialKind.SERVICE_API_KEY,
        holder=CredentialHolder.SERVICE,
        resources="embedding、RAG 知识库、旅行偏好长期记忆；服务端共用，长期记忆按 userId 区分数据而非凭证",
        lifecycle="长期 API Key 与 AccessKey，来自 agentscope.dashscope/bailian 配置",
        revocation="运维在阿里云作废 Key 并更新配置",
        enabled_in_java=True,
        evidence="config/RagKnowledgeConfig.java:36-87；agent/memory/TravelPreferenceLongTermMemoryFactory.java:52-62",
    ),
)

class AuthorizationDecision(BaseModel):
    """一次供应商请求的授权归属；只描述“用谁的授权”，不含密钥。"""

    model_config = ConfigDict(frozen=True)

    provider: str = Field(..., description="供应商集成标识")
    kind: CredentialKind = Field(..., description="使用的凭证形态")
    holder: CredentialHolder = Field(..., description="持有者类别：service 或 user")
    authorized_as: str = Field(..., description="授权主体：用户级为该用户标识，服务级为 service:<provider>")
    credential_ref: str = Field(..., description="凭证引用，只用于定位存储位置，不是密钥")
    integration_enabled: bool = Field(..., description="该集成在 Java 当前 Agent 中是否实际注册；False 表示仅为练习/契约")
    reason: str = Field(..., description="授权归属的中文说明")


UserCredentialLookup = Callable[[str, str], bool]


class CredentialResolver:
    """按清单决定授权归属。``user_lookups`` 按 provider 给出“该用户是否有自己的凭证”的查询。"""

    def __init__(
        self, user_lookups: Mapping[str, UserCredentialLookup] | None = None,
        service_configured: Collection[str] = (),
        integrations: Collection[Integration] = INTEGRATIONS,
    ):
        self._user_lookups = dict(user_lookups or {})
        self._service_configured = set(service_configured)
        self._integrations = {item.provider: item for item in integrations}

    def resolve(self, user_id: str, provider: str) -> AuthorizationDecision:
        """返回本次请求的授权决定；无法授权时抛出稳定错误码，绝不回退。"""
        integration = self._integrations.get(provider)
        if integration is None:
            raise CredentialError(PROVIDER_UNKNOWN, "集成清单中没有该供应商")
        if not isinstance(user_id, str) or not user_id.strip():
            raise CredentialError(USER_REQUIRED, "缺少可信用户标识，不能发起供应商请求")
        if integration.holder is CredentialHolder.SERVICE:
            if provider not in self._service_configured:
                raise CredentialError(SERVICE_NOT_CONFIGURED, "服务级凭证未配置")
            return self._decision(integration, f"service:{provider}", f"service:{provider}", "使用服务端共用凭证，只读能力")
        lookup = self._user_lookups.get(provider)
        if lookup is None or not lookup(user_id, provider):
            raise CredentialError(USER_NOT_CONFIGURED, "当前用户未配置该供应商的凭证，需先引导其授权或提供 Key")
        return self._decision(integration, user_id, f"user:{provider}:{user_id}", "使用当前用户自己的凭证")

    @staticmethod
    def _decision(integration: Integration, authorized_as: str, ref: str, reason: str) -> AuthorizationDecision:
        return AuthorizationDecision(
            provider=integration.provider, kind=integration.kind, holder=integration.holder,
            authorized_as=authorized_as, credential_ref=ref,
            integration_enabled=integration.enabled_in_java, reason=reason,
        )
