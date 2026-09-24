"""Security utilities: password hashing, transparent migration, and token management."""

import hashlib
import hmac
import os
import secrets
import time
from typing import Optional, Protocol


class PasswordManager:
    """密码安全管理与历史明文向哈希平滑迁移工具。"""

    ITERATIONS = 100_000
    ALGORITHM = "sha256"
    PREFIX = "pbkdf2:sha256"

    @classmethod
    def hash_password(cls, plain_password: str) -> str:
        """为纯文本密码生成 PBKDF2-HMAC-SHA256 安全加盐哈希。"""
        salt = secrets.token_hex(16)
        key = hashlib.pbkdf2_hmac(
            cls.ALGORITHM,
            plain_password.encode("utf-8"),
            salt.encode("utf-8"),
            cls.ITERATIONS,
        )
        return f"{cls.PREFIX}:{cls.ITERATIONS}:{salt}:{key.hex()}"

    @classmethod
    def verify_password(cls, plain_password: str, stored_value: str) -> tuple[bool, bool]:
        """验证密码是否正确，并返回是否需要自动升级为安全哈希。

        Returns:
            tuple[bool, bool]: (is_valid, needs_upgrade)
            - is_valid: 密码是否匹配
            - needs_upgrade: 若为历史明文且匹配成功，返回 True，提示服务端立即写回新哈希
        """
        if stored_value.startswith(f"{cls.PREFIX}:"):
            # 标准安全哈希比对
            try:
                _, _, iterations_str, salt, expected_hash = stored_value.split(":")
                iterations = int(iterations_str)
                key = hashlib.pbkdf2_hmac(
                    cls.ALGORITHM,
                    plain_password.encode("utf-8"),
                    salt.encode("utf-8"),
                    iterations,
                )
                is_valid = hmac.compare_digest(key.hex(), expected_hash)
                return is_valid, False
            except Exception:
                return False, False
        else:
            # 兼容历史明文密码（恒定时间字符串比对防止时序攻击）
            is_valid = hmac.compare_digest(stored_value, plain_password)
            needs_upgrade = is_valid  # 密码正确且为旧明文时触发自动升级
            return is_valid, needs_upgrade


class TokenStoreProtocol(Protocol):
    """Token 会话存储契约接口。"""

    def save_token(self, token: str, user_id: str, ttl_seconds: int) -> None:
        """存储 Token 与用户 ID 映射并设置有效期 (TTL)。"""
        ...

    def get_user_id(self, token: str) -> Optional[str]:
        """查询 Token 对应的用户 ID，若不存在或已过期返回 None。"""
        ...

    def revoke_token(self, token: str) -> bool:
        """主动注销/撤销指定的 Token。"""
        ...


class InMemoryTokenStore:
    """内存 Token 存储实现（用于轻量单测与无 Redis 环境备用）。"""

    def __init__(self):
        self._tokens: dict[str, dict] = {}
        self._revoked: set[str] = set()

    def save_token(self, token: str, user_id: str, ttl_seconds: int) -> None:
        self._tokens[token] = {
            "user_id": user_id,
            "expires_at": time.time() + ttl_seconds,
        }
        self._revoked.discard(token)

    def get_user_id(self, token: str) -> Optional[str]:
        if not token or token in self._revoked:
            return None
        session = self._tokens.get(token)
        if not session:
            return None
        if time.time() > session["expires_at"]:
            self._tokens.pop(token, None)
            return None
        return session["user_id"]

    def revoke_token(self, token: str) -> bool:
        existed = token in self._tokens
        self._tokens.pop(token, None)
        self._revoked.add(token)
        return existed


class RedisTokenStore:
    """基于 Redis 的跨会话持久化 Token 存储。"""

    def __init__(
        self,
        host: str = "172.22.22.123",
        port: int = 6379,
        db: int = 0,
        password: Optional[str] = None,
        prefix: str = "gogo:auth:token:",
        socket_timeout: float = 3.0,
    ):
        import redis
        self.host = host
        self.port = port
        self.prefix = prefix
        self._client = redis.Redis(
            host=host,
            port=port,
            db=db,
            password=password or None,
            socket_timeout=socket_timeout,
            decode_responses=True,
        )

    def ping(self) -> bool:
        """探活 Redis 节点连接。"""
        try:
            return bool(self._client.ping())
        except Exception:
            return False

    def save_token(self, token: str, user_id: str, ttl_seconds: int) -> None:
        """存储 Token 并设置指定 TTL 过期时间（秒）。"""
        self._client.set(f"{self.prefix}{token}", user_id, ex=ttl_seconds)

    def get_user_id(self, token: str) -> Optional[str]:
        """获取 Token 对应的用户 ID，若不存在或已过期返回 None。"""
        if not token:
            return None
        try:
            val = self._client.get(f"{self.prefix}{token}")
            return str(val) if val is not None else None
        except Exception:
            return None

    def revoke_token(self, token: str) -> bool:
        """主动从 Redis 中删除指定的 Token。"""
        if not token:
            return False
        try:
            return bool(self._client.delete(f"{self.prefix}{token}") > 0)
        except Exception:
            return False


class TokenManager:
    """服务端 Token 生成、有效性校验与跨会话持久化管理。"""

    DEFAULT_TTL_SECONDS = 30 * 24 * 3600  # 默认有效期 30 天 (2,592,000 秒)

    def __init__(
        self,
        secret_key: Optional[str] = None,
        store: Optional[TokenStoreProtocol] = None,
        default_ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ):
        self._secret_key = secret_key or os.environ.get("GOGO_AUTH_SECRET_KEY") or secrets.token_hex(32)
        self._store: TokenStoreProtocol = store or InMemoryTokenStore()
        self._default_ttl = default_ttl_seconds

    @property
    def store(self) -> TokenStoreProtocol:
        """获取当前绑定的底层 Token 存储器。"""
        return self._store

    def create_token(self, user_id: str, ttl_seconds: Optional[int] = None) -> str:
        """为指定用户生成唯一加密令牌并记录会话有效期 (默认 30 天)。"""
        raw_id = secrets.token_urlsafe(32)
        now = time.time()
        payload = f"{user_id}:{now}:{raw_id}"
        signature = hmac.new(
            self._secret_key.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        token = f"tk_{raw_id}_{signature[:16]}"

        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        self._store.save_token(token, user_id, ttl)
        return token

    def verify_token(self, token: str) -> Optional[str]:
        """验证 Token 有效性。

        若 Token 存在且未过期、未被主动注销，返回关联的 user_id；否则返回 None。
        """
        if not token:
            return None
        return self._store.get_user_id(token)

    def revoke_token(self, token: str) -> bool:
        """主动注销（退出登录）Token。"""
        if not token:
            return False
        return self._store.revoke_token(token)


def create_token_manager(prefer_redis: bool = True) -> TokenManager:
    """根据环境变量配置工厂方法创建 TokenManager，优先尝试连接 Redis 实现跨会话持久化。

    - 默认连接 172.22.22.123:6379 (无密码, DB 0)
    - 默认过期时间为 30 天 (2,592,000 秒)
    - 若 Redis 不可用或无法连接，自动平滑回退到 InMemoryTokenStore
    """
    ttl = int(os.environ.get("GOGO_AUTH_TOKEN_TTL_SECONDS", str(TokenManager.DEFAULT_TTL_SECONDS)))
    secret = os.environ.get("GOGO_AUTH_SECRET_KEY")

    if prefer_redis:
        redis_host = os.environ.get("GOGO_REDIS_HOST", "172.22.22.123").strip()
        redis_port = int(os.environ.get("GOGO_REDIS_PORT", "6379").strip())
        redis_db = int(os.environ.get("GOGO_REDIS_DB", "0").strip())
        redis_password = os.environ.get("GOGO_REDIS_PASSWORD", "").strip() or None
        if redis_host:
            try:
                redis_store = RedisTokenStore(
                    host=redis_host,
                    port=redis_port,
                    db=redis_db,
                    password=redis_password,
                )
                if redis_store.ping():
                    return TokenManager(secret_key=secret, store=redis_store, default_ttl_seconds=ttl)
            except Exception:
                pass

    return TokenManager(secret_key=secret, store=InMemoryTokenStore(), default_ttl_seconds=ttl)
