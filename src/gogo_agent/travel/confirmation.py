"""差旅申请提交的确认令牌：把「用户已确认」从模型自证的布尔值变成服务端可验证、有有效期的凭据。

设计取舍（如实声明边界）：

- 令牌是 HMAC 签名的无状态串，不落库。重放安全性由 `submit_order` 的幂等键、
  生效单指纹唯一约束与审计记录共同兜底，因此不需要令牌表。
- 令牌能证明「这份摘要由服务端签发过、且没被改动」，但**不能**从密码学上证明
  用户本人点了确认。它约束的是工作流形状（必须先取得服务端签发的摘要再提交），
  而不是用户意图本身；用户意图仍由对话确认过程承担。
- 签名密钥优先取 `GOGO_CONFIRMATION_SECRET`，其次从 `GOGO_SECRET_KEYS` 派生；
  两者都未配置时使用进程内随机密钥，仅供测试（重启后旧令牌失效，符合预期）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from typing import Any

_TOKEN_VERSION = "v1"
DEFAULT_TTL_SECONDS = 600
_SECRET_ENV = "GOGO_CONFIRMATION_SECRET"
_FALLBACK_ENV = "GOGO_SECRET_KEYS"
_PROCESS_KEY = os.urandom(32)

CONFIRMATION_INVALID = "CONFIRMATION_INVALID"
CONFIRMATION_STALE = "CONFIRMATION_STALE"
CONFIRMATION_EXPIRED = "CONFIRMATION_EXPIRED"


class ConfirmationError(Exception):
    """确认令牌校验失败；message 是可展示给模型的中文说明，不含密钥。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _signing_key() -> bytes:
    """签名密钥只从环境读取；缺失时退回进程内随机值，不写死默认密钥。"""
    raw = os.getenv(_SECRET_ENV, "").strip() or os.getenv(_FALLBACK_ENV, "").strip()
    if not raw:
        return _PROCESS_KEY
    return hashlib.sha256(("gogo-confirmation:" + raw).encode("utf-8")).digest()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def draft_digest(user_id: str, fields: dict[str, Any]) -> str:
    """用户与行程要素的稳定摘要；任一字段变化都会让旧令牌失效。"""
    encoded = json.dumps(
        {"user_id": user_id, "draft": fields},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def issue(
    user_id: str,
    digest: str,
    *,
    acknowledge_conflicts: bool = False,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    """签发一次性确认令牌；`acknowledge_conflicts` 记录用户是否已知情并接受冲突。"""
    now = int(time.time())
    payload = {
        "v": _TOKEN_VERSION, "u": user_id, "d": digest,
        "iat": now, "exp": now + max(1, int(ttl_seconds)),
        "ack": bool(acknowledge_conflicts),
    }
    body = _b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = _b64encode(hmac.new(_signing_key(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def verify(token: str, user_id: str, digest: str) -> dict[str, Any]:
    """校验令牌的用户绑定、摘要一致性与有效期；失败抛 ConfirmationError。"""
    if not isinstance(token, str) or "." not in token:
        raise ConfirmationError(CONFIRMATION_INVALID, "确认令牌格式不正确，请重新获取摘要")
    body, _, signature = token.partition(".")
    expected = _b64encode(hmac.new(_signing_key(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise ConfirmationError(CONFIRMATION_INVALID, "确认令牌签名校验失败，请重新获取摘要")
    try:
        payload = json.loads(_b64decode(body))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise ConfirmationError(CONFIRMATION_INVALID, "确认令牌内容无法解析，请重新获取摘要") from None
    if not isinstance(payload, dict) or payload.get("u") != user_id:
        raise ConfirmationError(CONFIRMATION_INVALID, "确认令牌不属于当前用户")
    if payload.get("d") != digest:
        raise ConfirmationError(CONFIRMATION_STALE, "确认令牌对应的行程已变化，请向用户重新确认摘要")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise ConfirmationError(CONFIRMATION_EXPIRED, "确认令牌已过期，请向用户重新确认摘要")
    return payload
