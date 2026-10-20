"""敏感信息脱敏（059 输出层兜底）。

对照 Java ``config/sensitive/SensitiveMasker.java`` 与 ``SensitiveMaskingConverter.java``：
先用有边界的正则在自由文本里定位敏感片段，再做最小必要遮盖，保留末尾少量字符便于人工核对。
脱敏只是最后一道出口兜底，不能替代“凭证根本不进入这些位置”的设计。

与 Java 的差异：
- API Key / Token 保留末尾 4 位（Java 的 ``sk-`` 规则整串遮盖）；长度不足 12 位则整串遮盖。
- 新增 ``Bearer`` 令牌与“精确已知密钥”替换；日志过滤器同时处理异常堆栈文本。
- 未移植 Java 的全大写拼音姓名规则：它会误伤 ``BEIJING SHANGHAI`` 一类正常大写词。
"""

import logging
import re
from collections.abc import Iterable

_MASK = "****"
_MIN_TAIL_SOURCE_LEN = 12
_TAIL_LEN = 4

_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_BANK_CARD = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_API_KEY = re.compile(r"(?<![A-Za-z0-9])sk[-_][A-Za-z0-9_-]{6,}")
_BEARER = re.compile(r"(?i)(\bBearer\s+)([A-Za-z0-9._~+/=-]{8,})")
_CHINESE_NAME = re.compile(
    r"(?i)(姓名|中文名|乘客|联系人|乘机人|chinese_?name)(\"?\s*[:=：]?\s*\"?)([\u4e00-\u9fa5]{2,4})"
)
_KV_SECRET = re.compile(
    r"(?i)(password|passwd|pwd|secret|access[-_]?key[-_]?secret|access[-_]?key[-_]?id|"
    r"api[-_]?key|apikey|access[-_]?token|token|authorization)(\"?\s*[:=]\s*\"?)([^\"'\s,;)}\]]+)"
)


class SensitiveMasker:
    """统一脱敏入口；无状态，可在日志、工具响应和异常文本处复用。"""

    @staticmethod
    def secret_suffix(value: str) -> str:
        """返回可用于展示的末尾 4 位；过短的值不展示任何字符。"""
        return value[-_TAIL_LEN:] if len(value) >= _MIN_TAIL_SOURCE_LEN else ""

    @staticmethod
    def mask_secret(value: str) -> str:
        """把整个密钥遮成 ``****`` 加可展示的末尾字符。"""
        return _MASK + SensitiveMasker.secret_suffix(value)

    @staticmethod
    def mask(text: str | None, secrets: Iterable[str] = ()) -> str | None:
        """脱敏自由文本；``secrets`` 是调用方已知的明文，会先做精确替换（长度不足 4 的忽略）。"""
        if not text:
            return text
        masked = text
        for secret in secrets:
            if secret and len(secret) >= 4:
                masked = masked.replace(secret, SensitiveMasker.mask_secret(secret))
        # 顺序与 Java 一致：先身份证，再银行卡，避免 18 位身份证被银行卡规则重复处理
        masked = _ID_CARD.sub(lambda m: m.group()[:3] + "*" * 11 + m.group()[-4:], masked)
        masked = _BANK_CARD.sub(lambda m: m.group()[:4] + "*" * (len(m.group()) - 8) + m.group()[-4:], masked)
        masked = _PHONE.sub(lambda m: m.group()[:3] + _MASK + m.group()[-4:], masked)
        masked = _EMAIL.sub(lambda m: m.group()[0] + "***@" + m.group().split("@", 1)[1], masked)
        masked = _API_KEY.sub(lambda m: SensitiveMasker.mask_secret(m.group()), masked)
        masked = _BEARER.sub(lambda m: m.group(1) + SensitiveMasker.mask_secret(m.group(2)), masked)
        masked = _CHINESE_NAME.sub(lambda m: m.group(1) + m.group(2) + _MASK, masked)
        masked = _KV_SECRET.sub(SensitiveMasker._mask_kv, masked)
        return masked

    @staticmethod
    def _mask_kv(match: re.Match[str]) -> str:
        value = match.group(3)
        if value.lower() == "bearer" or value.startswith(_MASK):  # 令牌本体已由前面的规则处理，别重复遮盖
            return match.group(0)
        return match.group(1) + match.group(2) + SensitiveMasker.mask_secret(value)


class SensitiveLogFilter(logging.Filter):
    """挂在 ``logging.Handler`` 上的脱敏过滤器（对应 Java 的 logback ``%sensitive`` 转换器）。

    必须加在 handler 上：logger 上的过滤器不会处理子 logger 传播来的记录。
    过滤器会就地改写记录，因此同一记录经过的所有 handler 都看到脱敏后的文本。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = SensitiveMasker.mask(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = SensitiveMasker.mask(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = SensitiveMasker.mask(record.exc_text)
        return True
