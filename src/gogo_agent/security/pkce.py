"""OAuth PKCE 小练习（057）：S256 校验、绑定用户的一次性 state、模拟授权方与模拟 rgh CLI。

核对结论（详见 docs/契约样例/阶段E-050-059-调试指南.md）：Java 仓库没有任何 PKCE 代码
（没有 code_verifier/code_challenge/state 的实现）；PKCE 由第三方 ``rgh`` CLI 执行，Java 只做用户隔离
（独立 HOME）、转发授权链接和捕获 Token（``RghUserIsolationHook``）。因此：

- ``PkceLoginService`` 是**教学用的应用内实现**，Java 没有对应物，只有“确需接管时”才会用；
- ``MockPkceCli`` 模拟 rgh 自己完成 PKCE，Python 主流程的真实边界是 ``exercises/rgh_workspace.py``。

本文件所有授权方与 CLI 都是内存/临时目录替身，不访问真实服务；未实现刷新令牌。
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from gogo_agent.security.errors import SecurityError

STATE_UNKNOWN = "PKCE_STATE_UNKNOWN"
STATE_EXPIRED = "PKCE_STATE_EXPIRED"
STATE_USER_MISMATCH = "PKCE_STATE_USER_MISMATCH"
CODE_UNKNOWN = "PKCE_CODE_UNKNOWN"
CODE_REUSED = "PKCE_CODE_REUSED"
CODE_EXPIRED = "PKCE_CODE_EXPIRED"
VERIFIER_MISMATCH = "PKCE_VERIFIER_MISMATCH"
REQUEST_INVALID = "PKCE_REQUEST_INVALID"

TOKEN_DIR = ".hotel-cli"
TOKEN_FILE = "token.json"


class PkceError(SecurityError):
    """PKCE 流程失败；错误文本不含 verifier、授权码或 Token。"""


def generate_code_verifier() -> str:
    """RFC 7636：43–128 个 URL 安全字符；这里取 86 个。"""
    return secrets.token_urlsafe(64)


def code_challenge_s256(verifier: str) -> str:
    """``BASE64URL(SHA256(verifier))``，不带 ``=`` 填充。"""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


class TokenSet(BaseModel):
    """授权方签发的令牌；``access_token`` 用 SecretStr 持有，repr 不会打印明文。"""

    model_config = ConfigDict(frozen=True)

    account: str = Field(..., description="授权方账号标识（供应商侧用户，不是本系统 user_id）")
    access_token: SecretStr = Field(..., description="访问令牌，仅服务端使用，不得进入日志或模型上下文")
    expires_at: float = Field(..., description="过期时间，Unix 秒")


class CallbackParams(BaseModel):
    """授权方重定向回调携带的参数。"""

    model_config = ConfigDict(frozen=True)

    code: str = Field(..., description="一次性授权码，短期有效")
    state: str = Field(..., description="发起方生成的一次性 state，原样回传")


class MockAuthorizationServer:
    """模拟授权方：授权码一次性、带过期，换令牌时校验 S256 verifier。"""

    def __init__(self, clock: Callable[[], float] = time.time, code_ttl: float = 60.0, token_ttl: float = 3600.0):
        self._clock, self._code_ttl, self._token_ttl = clock, code_ttl, token_ttl
        self._codes: dict[str, dict] = {}
        self._lock = threading.Lock()

    def approve(self, authorize_url: str, account: str) -> CallbackParams:
        """模拟用户在浏览器里以 ``account`` 登录并点击同意：解析授权链接并签发授权码。"""
        query = {key: values[0] for key, values in parse_qs(urlparse(authorize_url).query).items()}
        if query.get("response_type") != "code" or query.get("code_challenge_method") != "S256" \
                or not query.get("code_challenge") or not query.get("state"):
            raise PkceError(REQUEST_INVALID, "授权请求缺少 response_type=code、S256 challenge 或 state")
        code = secrets.token_urlsafe(24)
        with self._lock:
            self._codes[code] = {
                "challenge": query["code_challenge"], "account": account,
                "expires_at": self._clock() + self._code_ttl, "used": False,
            }
        return CallbackParams(code=code, state=query["state"])

    def exchange(self, code: str, code_verifier: str) -> TokenSet:
        """授权码换令牌：任何一次尝试都会消耗该码，再校验过期与 verifier。"""
        with self._lock:
            record = self._codes.get(code)
            if record is None:
                raise PkceError(CODE_UNKNOWN, "授权码不存在")
            if record["used"]:
                raise PkceError(CODE_REUSED, "授权码已使用过，不能重复兑换")
            record["used"] = True
            if self._clock() > record["expires_at"]:
                raise PkceError(CODE_EXPIRED, "授权码已过期")
            if not hmac.compare_digest(code_challenge_s256(code_verifier), record["challenge"]):
                raise PkceError(VERIFIER_MISMATCH, "code_verifier 与授权请求中的 challenge 不匹配")
            return TokenSet(
                account=record["account"], access_token=SecretStr("tok_" + secrets.token_urlsafe(32)),
                expires_at=self._clock() + self._token_ttl,
            )


def build_authorize_url(client_id: str, redirect_uri: str, state: str, challenge: str, scope: str) -> str:
    """拼接授权链接；链接里只有 challenge，不含 verifier。"""
    return "https://auth.mock.test/authorize?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "scope": scope,
    })


class AuthorizationStart(BaseModel):
    """发起登录的结果：给用户打开的授权链接与对应的 state。"""

    model_config = ConfigDict(frozen=True)

    authorize_url: str = Field(..., description="返回给用户的授权链接，含 challenge 与 state，不含 verifier")
    state: str = Field(..., description="绑定到发起用户与会话的一次性 state")


class LoginResult(BaseModel):
    """登录成功后恢复的原用户与会话。"""

    model_config = ConfigDict(frozen=True)

    user_id: str = Field(..., description="发起登录的本系统用户标识")
    session_id: str = Field(..., description="发起登录时的会话标识，登录后用它继续原对话")
    account: str = Field(..., description="授权方账号标识")


@dataclass
class _PendingState:
    user_id: str
    session_id: str
    verifier: str
    expires_at: float


class PkceLoginService:
    """应用内 PKCE 登录（教学用）：state 绑定用户与会话，一次性且带过期；Token 只按用户保存。"""

    def __init__(
        self, server: MockAuthorizationServer, clock: Callable[[], float] = time.time, state_ttl: float = 600.0,
        client_id: str = "gogo-agent-demo", redirect_uri: str = "https://gogo.example.test/oauth/callback",
    ):
        self._server, self._clock, self._state_ttl = server, clock, state_ttl
        self._client_id, self._redirect_uri = client_id, redirect_uri
        self._states: dict[str, _PendingState] = {}
        self._tokens: dict[str, TokenSet] = {}
        self._lock = threading.Lock()

    def start(self, user_id: str, session_id: str) -> AuthorizationStart:
        """为可信用户与会话生成 verifier（只留在服务端）、challenge 与一次性 state。"""
        verifier, state = generate_code_verifier(), secrets.token_urlsafe(32)
        with self._lock:
            self._states[state] = _PendingState(user_id, session_id, verifier, self._clock() + self._state_ttl)
        url = build_authorize_url(self._client_id, self._redirect_uri, state, code_challenge_s256(verifier), "orders")
        return AuthorizationStart(authorize_url=url, state=state)

    def handle_callback(self, authenticated_user_id: str, state: str, code: str) -> LoginResult:
        """回调处理：``authenticated_user_id`` 必须来自当前登录会话而不是回调参数。"""
        with self._lock:
            pending = self._states.get(state)
            if pending is None:
                raise PkceError(STATE_UNKNOWN, "state 不存在或已使用，可能是重复回调或伪造回调")
            if self._clock() > pending.expires_at:
                del self._states[state]
                raise PkceError(STATE_EXPIRED, "state 已过期，请重新发起授权")
            if pending.user_id != authenticated_user_id:
                raise PkceError(STATE_USER_MISMATCH, "state 属于其他用户，拒绝处理")
            del self._states[state]  # 一次性：之后无论换令牌成败，该 state 都不能再用
        token = self._server.exchange(code, pending.verifier)
        with self._lock:
            self._tokens[pending.user_id] = token
        return LoginResult(user_id=pending.user_id, session_id=pending.session_id, account=token.account)

    def token_for(self, user_id: str) -> TokenSet | None:
        """只返回该用户自己的令牌，且已过期时视为没有。"""
        with self._lock:
            token = self._tokens.get(user_id)
        return token if token is not None and token.expires_at > self._clock() else None


@dataclass
class _CliLogin:
    home: Path
    verifier: str
    expires_at: float


class MockPkceCli:
    """模拟 rgh：CLI 自己生成 verifier/state/challenge，经本地回调换令牌并写入 ``$HOME/.hotel-cli/token.json``。

    应用不接触 verifier 与授权码，只能：给 CLI 一个独立 HOME、拿回授权链接、事后读取 Token 文件。
    """

    def __init__(
        self, server: MockAuthorizationServer, clock: Callable[[], float] = time.time, login_timeout: float = 300.0,
        client_id: str = "rgh-mock", redirect_uri: str = "http://127.0.0.1:9999/callback",
    ):
        self._server, self._clock, self._login_timeout = server, clock, login_timeout
        self._client_id, self._redirect_uri = client_id, redirect_uri
        self._pending: dict[str, _CliLogin] = {}
        self._lock = threading.Lock()

    def login_start(self, home: Path) -> str:
        """``rgh login``：生成 PKCE 参数并返回授权链接；Token 稍后在本地回调时写入 ``home``。"""
        verifier, state = generate_code_verifier(), secrets.token_urlsafe(24)
        with self._lock:
            self._pending[state] = _CliLogin(home, verifier, self._clock() + self._login_timeout)
        return build_authorize_url(self._client_id, self._redirect_uri, state, code_challenge_s256(verifier), "hotel:order")

    def local_callback(self, params: CallbackParams) -> None:
        """模拟浏览器重定向到 CLI 的本地监听端口：校验 state、换令牌、写文件。"""
        with self._lock:
            login = self._pending.pop(params.state, None)
        if login is None:
            raise PkceError(STATE_UNKNOWN, "CLI 没有对应的待完成登录，state 无效或已使用")
        if self._clock() > login.expires_at:
            raise PkceError(STATE_EXPIRED, "登录已超时，请重新执行登录")
        token = self._server.exchange(params.code, login.verifier)
        path = login.home / TOKEN_DIR / TOKEN_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"account": token.account, "access_token": token.access_token.get_secret_value(), "expires_at": token.expires_at}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file)

    def whoami(self, home: Path) -> str | None:
        """``rgh whoami``：返回已登录的授权方账号；没有 Token 或 Token 已过期返回 None。"""
        path = home / TOKEN_DIR / TOKEN_FILE
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        return payload["account"] if payload.get("expires_at", 0) > self._clock() else None

    def logout(self, home: Path) -> None:
        """``rgh logout``：删除该 HOME 下的 Token 文件。"""
        (home / TOKEN_DIR / TOKEN_FILE).unlink(missing_ok=True)
