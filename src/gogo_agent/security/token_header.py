"""直接 Token 配置鉴权（056）：服务端读取配置型 Token，仅向目标供应商域名注入请求头。

对照文章 056 与 Java 的服务级凭证用法（``WeatherMcpConfig`` 把 AppCode 放在 endpoint、``OriznVisaMcpConfig``
用环境变量、``DestinationLiveTools`` 用查询参数）。Python 版把 Token 放在请求头，且 Token 只在
``httpx.Auth`` 的 ``auth_flow`` 里取用：

- 缺 Token、Token 含换行、目标不是 HTTPS 或域名不等于允许域名，都在请求发出前失败，传输层不会被调用；
- 跨域重定向由 httpx 自身去掉 ``Authorization``（测试覆盖），本模块不会向第三方域名注入；
- 错误与日志只含供应商标识、环境变量名和域名，不含 Token；Token 以 ``SecretStr`` 持有，不缓存。

注入点就是 ``TokenHeaderAuth``，可直接传给 ``httpx.Client(auth=..., transport=httpx.MockTransport(...))`` 验证。
"""

import logging
import os
from collections.abc import Generator, Mapping

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from gogo_agent.security.errors import SecurityError

logger = logging.getLogger(__name__)

TOKEN_MISSING = "TOKEN_MISSING"
TOKEN_INVALID = "TOKEN_INVALID"
TOKEN_HOST_NOT_ALLOWED = "TOKEN_HOST_NOT_ALLOWED"
TOKEN_INSECURE_SCHEME = "TOKEN_INSECURE_SCHEME"


class TokenHeaderError(SecurityError):
    """Token 缺失或目标不合法，请求未发出；错误文本不含 Token。"""


class TokenHeaderPolicy(BaseModel):
    """某个供应商的 Token 来源与注入规则。"""

    model_config = ConfigDict(frozen=True)

    provider: str = Field(..., description="供应商标识，只用于日志与错误")
    allowed_host: str = Field(..., description="唯一允许注入 Token 的目标域名，按完整主机名精确比较")
    env_var: str = Field(..., description="保存 Token 的环境变量名；Token 不写入源码或配置文件")
    header_name: str = Field(default="Authorization", description="承载 Token 的请求头名称")
    scheme: str = Field(default="Bearer", description="令牌前缀；为空串时直接放 Token 本身")


class TokenHeaderAuth(httpx.Auth):
    """``httpx`` 鉴权注入点：校验目标后读取 Token 并写入请求头。"""

    def __init__(self, policy: TokenHeaderPolicy, environ: Mapping[str, str] | None = None):
        self._policy = policy
        self._environ = os.environ if environ is None else environ

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        policy = self._policy
        if request.url.scheme != "https":
            raise TokenHeaderError(TOKEN_INSECURE_SCHEME, "只允许向 HTTPS 地址发送 Token")
        if request.url.host.lower() != policy.allowed_host.lower():
            raise TokenHeaderError(TOKEN_HOST_NOT_ALLOWED, f"目标域名不是 {policy.provider} 的允许域名，请求未发出")
        token = self._read_token()
        value = f"{policy.scheme} {token.get_secret_value()}".strip()
        request.headers[policy.header_name] = value
        logger.info("[TokenHeader] 已注入请求头 provider=%s host=%s", policy.provider, request.url.host)
        yield request

    def _read_token(self) -> SecretStr:
        raw = self._environ.get(self._policy.env_var, "").strip()
        if not raw:
            raise TokenHeaderError(TOKEN_MISSING, f"未配置 {self._policy.provider} 的 Token（环境变量 {self._policy.env_var}），请求未发出")
        if any(ch in raw for ch in "\r\n\x00") or any(ch.isspace() for ch in raw):
            raise TokenHeaderError(TOKEN_INVALID, f"{self._policy.env_var} 中的 Token 含空白或控制字符，请求未发出")
        return SecretStr(raw)
