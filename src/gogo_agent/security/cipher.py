"""凭证加密（059）：AES-256-GCM，带密钥版本，支持多密钥轮换。

对照 Java ``config/sensitive/SecretCipher.java``（AES-GCM，密钥由 SHA-256 派生）与
``apikey/ApiKeyService``（AES-128-ECB，不应沿用）。Python 版的选择：
- 使用 ``cryptography`` 的 ``AESGCM``，随机 12 字节 nonce，认证标签防篡改；
- 密钥是环境变量里的 32 字节随机密钥，不从口令做无盐 SHA-256 派生，也没有默认密钥；
- 密文自带密钥版本；``associated_data`` 把密文绑定到 (用户, provider)，防止拷贝密文到别人的行；
- 解密失败显式抛出稳定错误码，而不是像 Java 那样返回 ``null`` 后静默降级成“无凭据”。

轮换预案：``GOGO_SECRET_KEYS=v2:<密钥>,v1:<旧密钥>``，第一个是当前加密密钥，其余仍可解密；
对旧密文调用 ``reencrypt`` 迁移到当前密钥，迁移完成后再从环境变量里删除旧密钥。
"""

import base64
import binascii
import os
import re
from collections.abc import Mapping, Sequence

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from gogo_agent.security.errors import SecurityError

ENV_NAME = "GOGO_SECRET_KEYS"
_VERSION = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
_NONCE_LEN = 12
_KEY_LEN = 32

KEYS_MISSING = "SECRET_KEYS_MISSING"
KEYS_INVALID = "SECRET_KEYS_INVALID"
KEY_VERSION_UNKNOWN = "SECRET_KEY_VERSION_UNKNOWN"
CIPHERTEXT_INVALID = "SECRET_CIPHERTEXT_INVALID"
DECRYPT_FAILED = "SECRET_DECRYPT_FAILED"


class SecretCipherError(SecurityError):
    """加密配置缺失/非法，或密文无法解密；文本固定，不含密钥或密文。"""


def generate_keys_env(version: str = "v1") -> str:
    """生成一个 ``版本:密钥`` 片段，供运维写入 ``GOGO_SECRET_KEYS``。"""
    return f"{version}:{base64.urlsafe_b64encode(os.urandom(_KEY_LEN)).decode()}"


class SecretCipher:
    """持有一个当前密钥和若干旧密钥；密文格式 ``<版本>.<base64url(nonce+密文+标签)>``。"""

    def __init__(self, keys: Sequence[tuple[str, bytes]]):
        if not keys:
            raise SecretCipherError(KEYS_MISSING, "未配置加密密钥")
        self._keys: dict[str, AESGCM] = {}
        for version, key in keys:
            if not _VERSION.match(version) or len(key) != _KEY_LEN or version in self._keys:
                raise SecretCipherError(KEYS_INVALID, "加密密钥配置不合法")
            self._keys[version] = AESGCM(key)
        self.current_version = keys[0][0]

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "SecretCipher":
        """只从环境变量读取密钥；缺失或格式错误时显式失败。"""
        raw = (os.environ if environ is None else environ).get(ENV_NAME, "").strip()
        if not raw:
            raise SecretCipherError(KEYS_MISSING, f"未设置环境变量 {ENV_NAME}")
        keys: list[tuple[str, bytes]] = []
        for item in raw.split(","):
            version, separator, encoded = item.strip().partition(":")
            try:
                key = base64.urlsafe_b64decode(encoded) if separator else b""
            except (binascii.Error, ValueError):
                key = b""
            keys.append((version, key))
        return cls(keys)

    def encrypt(self, plaintext: str, associated_data: str = "") -> str:
        """用当前密钥加密；``associated_data`` 解密时必须一致。"""
        nonce = os.urandom(_NONCE_LEN)
        sealed = self._keys[self.current_version].encrypt(nonce, plaintext.encode(), associated_data.encode())
        return f"{self.current_version}.{base64.urlsafe_b64encode(nonce + sealed).decode()}"

    def decrypt(self, token: str, associated_data: str = "") -> str:
        """按密文里的版本选择密钥解密；任何失败都只返回稳定错误码。"""
        version, payload = self._split(token)
        try:
            raw = base64.urlsafe_b64decode(payload)
        except (binascii.Error, ValueError):
            raise SecretCipherError(CIPHERTEXT_INVALID, "密文格式不合法") from None
        if len(raw) <= _NONCE_LEN:
            raise SecretCipherError(CIPHERTEXT_INVALID, "密文格式不合法")
        try:
            plain = self._keys[version].decrypt(raw[:_NONCE_LEN], raw[_NONCE_LEN:], associated_data.encode())
            return plain.decode()
        except (InvalidTag, UnicodeDecodeError):
            raise SecretCipherError(DECRYPT_FAILED, "密文无法解密：密钥不匹配、被篡改或绑定对象不一致") from None

    def key_version_of(self, token: str) -> str:
        """读取密文声明的密钥版本，不解密。"""
        return self._split(token)[0]

    def needs_rotation(self, token: str) -> bool:
        """密文不是用当前密钥加密时返回 True。"""
        return self.key_version_of(token) != self.current_version

    def reencrypt(self, token: str, associated_data: str = "") -> str:
        """用旧密钥解密再用当前密钥加密；已是当前版本时原样返回。"""
        if not self.needs_rotation(token):
            return token
        return self.encrypt(self.decrypt(token, associated_data), associated_data)

    def _split(self, token: str) -> tuple[str, str]:
        version, separator, payload = token.partition(".")
        if not separator or not payload:
            raise SecretCipherError(CIPHERTEXT_INVALID, "密文格式不合法")
        if version not in self._keys:
            raise SecretCipherError(KEY_VERSION_UNKNOWN, "密文使用的密钥版本未配置")
        return version, payload
