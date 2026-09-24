"""Security utilities: password hashing, transparent migration, and token management."""

import hashlib
import hmac
import os
import secrets
import time
from typing import Optional


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


class TokenManager:
    """服务端 Token 生成、有效性校验与主动撤销管理。"""

    def __init__(self, secret_key: Optional[str] = None):
        self._secret_key = secret_key or os.environ.get("GOGO_AUTH_SECRET_KEY") or secrets.token_hex(32)
        # 内存存储活跃 Token 会话: token -> {"user_id": str, "expires_at": float}
        self._active_tokens: dict[str, dict] = {}
        # 已撤销（黑名单）Token 缓存
        self._revoked_tokens: set[str] = set()

    def create_token(self, user_id: str, ttl_seconds: int = 86400) -> str:
        """为指定用户生成唯一加密令牌并记录会话有效期。"""
        raw_id = secrets.token_urlsafe(32)
        now = time.time()
        expires_at = now + ttl_seconds
        payload = f"{user_id}:{now}:{raw_id}"
        signature = hmac.new(
            self._secret_key.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        token = f"tk_{raw_id}_{signature[:16]}"

        self._active_tokens[token] = {
            "user_id": user_id,
            "expires_at": expires_at,
        }
        return token

    def verify_token(self, token: str) -> Optional[str]:
        """验证 Token 有效性。

        若 Token 存在且未过期、未被主动销毁，返回关联的 user_id；否则返回 None。
        """
        if not token or token in self._revoked_tokens:
            return None

        session = self._active_tokens.get(token)
        if not session:
            return None

        # 检查是否过期
        if time.time() > session["expires_at"]:
            self._active_tokens.pop(token, None)
            return None

        return session["user_id"]

    def revoke_token(self, token: str) -> bool:
        """主动注销（退出登录）Token。"""
        if token in self._active_tokens:
            self._active_tokens.pop(token)
            self._revoked_tokens.add(token)
            return True
        return False
