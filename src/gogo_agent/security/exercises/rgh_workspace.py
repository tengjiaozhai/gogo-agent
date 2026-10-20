"""051 Rolling Go 工作空间与登录态练习。

**当前未启用，不属于已迁移业务功能。** Java 的 ``rolling-go-hotel`` Skill 资源存在，但规划/预订 Agent
的 SkillBox 没有注册它（``RghUserIsolationHook`` 仍挂在 Hook 链上，是否真被调用没有运行证据）。
本文件只用假 CLI（``MockPkceCli``）练习 Java ``RghUserIsolationHook`` / ``RghWorkspace`` / ``RghTokenStore``
的边界，不接入酒店搜索路径：

- 每个用户一个独立 HOME（目录 700、Token 文件 600），用户标识先过白名单再拼路径；
- 跨节点以共享存储为权威源：每次命令前把存储里的 Token 投影到本地，没有就删本地残留；
  命令后若本地 Token 变了就回写存储；logout 同时清存储与本地；
- 存储里的 Token 用 ``SecretCipher`` 加密，绑定 (rgh, user_id)；
- 与 Java 的差异：没有后台 watcher 线程，登录完成后由 ``capture_login`` 显式捕获；命令用枚举，不拼 shell 文本。
"""

import logging
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

from gogo_agent.security.cipher import SecretCipher, SecretCipherError
from gogo_agent.security.errors import SecurityError
from gogo_agent.security.pkce import TOKEN_DIR, TOKEN_FILE, CallbackParams, MockPkceCli

logger = logging.getLogger(__name__)

PROVIDER = "rolling-go-hotel"
LOGIN_OUTPUT_FILE = "rgh_login_output.txt"
INVALID_ID = "RGH_INVALID_ID"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TOKEN_TTL = 30 * 24 * 3600.0


class RghWorkspaceError(SecurityError):
    """工作空间标识非法等错误；文本不含 Token。"""


class TokenStore(Protocol):
    """跨节点 Token 共享存储接口；生产可换成 Redis 实现，练习用内存实现。"""

    def get(self, user_id: str) -> Optional[str]: ...
    def save(self, user_id: str, token_json: str) -> None: ...
    def delete(self, user_id: str) -> None: ...


class InMemoryTokenStore:
    """内存版共享存储（代替 Java 的 Redis ``rgh:token:{userId}``）：只存密文，带过期，线程安全。"""

    def __init__(self, cipher: SecretCipher, clock: Callable[[], float] = time.time, ttl: float = _TOKEN_TTL):
        self._cipher, self._clock, self._ttl = cipher, clock, ttl
        self._data: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def get(self, user_id: str) -> Optional[str]:
        with self._lock:
            entry = self._data.get(user_id)
        if entry is None or entry[1] <= self._clock():
            return None
        try:
            return self._cipher.decrypt(entry[0], f"{PROVIDER}\x1f{user_id}")
        except SecretCipherError as error:  # 密钥丢失/被篡改：按未登录处理，要求重新授权
            logger.warning("[RghTokenStore] 存储的 Token 无法解密，按未登录处理 user_id=%s code=%s", user_id, error.code)
            return None

    def save(self, user_id: str, token_json: str) -> None:
        sealed = self._cipher.encrypt(token_json, f"{PROVIDER}\x1f{user_id}")
        with self._lock:
            self._data[user_id] = (sealed, self._clock() + self._ttl)

    def delete(self, user_id: str) -> None:
        with self._lock:
            self._data.pop(user_id, None)


class WorkspaceLease(BaseModel):
    """某用户在某会话下获得的工作空间与凭证引用。"""

    model_config = ConfigDict(frozen=True)

    user_id: str = Field(..., description="工作空间所有者的可信用户标识")
    session_id: str = Field(..., description="本次使用的会话标识")
    home: Path = Field(..., description="注入给 CLI 的独立 HOME 目录，只属于该用户")
    session_dir: Path = Field(..., description="该会话的临时目录，存放含授权链接的登录输出，用完即删")
    credential_ref: str = Field(..., description="凭证引用 user:<provider>:<user_id>，不是 Token 本身")


class RghResult(BaseModel):
    """返回给上层（模型可见）的结果：只含状态与授权链接，不含 Token。"""

    model_config = ConfigDict(frozen=True)

    status: Literal["logged_in", "login_required", "authorization_link", "logged_out"] = Field(
        ..., description="登录态：已登录 / 需要授权 / 已生成授权链接 / 已退出"
    )
    account: Optional[str] = Field(default=None, description="已登录的供应商账号标识")
    authorize_url: Optional[str] = Field(default=None, description="需要用户打开的授权链接，仅 authorization_link 时有值")
    message: str = Field(..., description="面向用户的中文说明")


class RghNode:
    """一个应用节点：本机独立工作区 + 本机 CLI 进程，与其他节点只通过共享 ``TokenStore`` 同步。"""

    def __init__(self, base_dir: Path, cli: MockPkceCli, store: TokenStore):
        self._base, self._cli, self._store = Path(base_dir), cli, store

    def lease(self, user_id: str, session_id: str) -> WorkspaceLease:
        """分配并收紧权限；非法标识（含 ``..``）直接拒绝，不拼路径。"""
        if not _SAFE_ID.match(user_id or "") or not _SAFE_ID.match(session_id or ""):
            raise RghWorkspaceError(INVALID_ID, "用户或会话标识含非法字符，拒绝用于文件路径")
        home = self._base / user_id
        session_dir = home / "sessions" / session_id
        for directory in (home, home / TOKEN_DIR, home / "sessions", session_dir):
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        return WorkspaceLease(
            user_id=user_id, session_id=session_id, home=home, session_dir=session_dir,
            credential_ref=f"user:{PROVIDER}:{user_id}",
        )

    def run(self, user_id: str, session_id: str, command: Literal["whoami", "login", "logout"]) -> RghResult:
        """命令前同步登录态、执行假 CLI、命令后回写或清理。"""
        lease = self.lease(user_id, session_id)
        synced = self._project_token(lease)
        if command == "logout":
            self._cli.logout(lease.home)
            self._store.delete(user_id)
            self._delete_local(lease)
            (lease.session_dir / LOGIN_OUTPUT_FILE).unlink(missing_ok=True)
            return RghResult(status="logged_out", message="已退出登录，共享存储与本机 Token 均已清理")
        if command == "login":
            url = self._cli.login_start(lease.home)
            output = lease.session_dir / LOGIN_OUTPUT_FILE
            output.write_text(url, encoding="utf-8")
            os.chmod(output, 0o600)
            return RghResult(status="authorization_link", authorize_url=url, message="请打开链接完成授权，完成后再告诉我")
        account = self._cli.whoami(lease.home)
        self._write_back(lease, synced)
        if account is None:
            return RghResult(status="login_required", message="未登录或登录已过期，需要重新授权")
        return RghResult(status="logged_in", account=account, message="已登录")

    def deliver_callback(self, params: CallbackParams) -> None:
        """模拟浏览器回到本节点 CLI 的本地回调端口；回调只能送到发起登录的那个节点。"""
        self._cli.local_callback(params)

    def capture_login(self, user_id: str, session_id: str) -> RghResult:
        """Token 捕获（对应 Java 登录 watcher）：本机出现新 Token 就回写共享存储并清理授权链接文件。"""
        lease = self.lease(user_id, session_id)
        self._write_back(lease, self._store.get(user_id))
        account = self._cli.whoami(lease.home)
        if account is None:
            return RghResult(status="login_required", message="尚未完成授权")
        (lease.session_dir / LOGIN_OUTPUT_FILE).unlink(missing_ok=True)
        return RghResult(status="logged_in", account=account, message="登录成功，登录态已同步到共享存储")

    def _local_path(self, lease: WorkspaceLease) -> Path:
        return lease.home / TOKEN_DIR / TOKEN_FILE

    def _project_token(self, lease: WorkspaceLease) -> Optional[str]:
        """共享存储是权威源：有值写入本机，无值删除本机残留（让别处的登出/过期在这里同样生效）。"""
        token = self._store.get(lease.user_id)
        path = self._local_path(lease)
        if token is None:
            path.unlink(missing_ok=True)
            return None
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(token)
        return token

    def _write_back(self, lease: WorkspaceLease, synced: Optional[str]) -> None:
        path = self._local_path(lease)
        local = path.read_text(encoding="utf-8") if path.exists() else None
        if local and local != synced:
            self._store.save(lease.user_id, local)

    def _delete_local(self, lease: WorkspaceLease) -> None:
        self._local_path(lease).unlink(missing_ok=True)
