"""阶段 H 只读语义召回：从本机 ReMe 检索本人历史偏好片段，不写入。

显式偏好仍以 SQLite 为权威，本模块只提供增强召回。
ReMe 本身不按用户隔离：每次检索必须带本人 `search_filter.prefix`，返回后再按前缀过滤一次。
任何绕过本模块直接调用 `/search` 的代码都可能串户。
"""

from __future__ import annotations

import hashlib
import logging

import httpx

from gogo_agent.security.masking import SensitiveMasker

logger = logging.getLogger("uvicorn.error")
RECALL_KEY = "semantic_recall"


def user_memory_prefix(user_id: str) -> str:
    """派生本人在 ReMe workspace 中的目录前缀；空用户标识直接拒绝。"""
    if not user_id:
        raise ValueError("语义召回必须提供用户标识")
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]
    return f"digest/users/{digest}/"


def semantic_preferences(
    user_id: str,
    query: str,
    *,
    base_url: str,
    top_k: int = 5,
    timeout: float = 2.0,
    transport: httpx.BaseTransport | None = None,
) -> list[dict]:
    """返回与本轮问题相关的本人偏好片段，形状与 ModelInputContext 的 preferences 回调兼容。

    参数错误在发出请求前抛出 ValueError；ReMe 不可用、超时或返回异常时记录脱敏日志并返回空列表。
    `transport` 仅供测试注入 httpx.MockTransport。
    """
    prefix = user_memory_prefix(user_id)
    if top_k < 1:
        raise ValueError("top_k 必须为正整数")
    if not query.strip():
        return []
    # ReMe 0.4.2.0 搜索步骤只读取 limit（reme/steps/index/search.py:202），top_k 会被静默忽略。
    body = {"query": query, "limit": top_k, "search_filter": {"prefix": prefix}}
    try:
        # trust_env=False：不经环境变量中的代理转发用户问题与用户目录。
        with httpx.Client(base_url=base_url, timeout=timeout, trust_env=False, transport=transport) as client:
            response = client.post("/search", json=body)
            response.raise_for_status()
            payload = response.json()
        if payload.get("success") is not True:
            raise ValueError("ReMe 返回 success=false")
        items: list[dict] = []
        for result in payload["metadata"]["results"]:
            path, text = result.get("path"), result.get("text")
            if not isinstance(path, str) or not path.startswith(prefix):
                continue
            if not isinstance(text, str) or not text.strip():
                continue
            items.append({"key": RECALL_KEY, "value": text.strip(), "source": path[len(prefix):]})
        return items[:top_k]
    except Exception as exc:  # 语义召回只是增强层，任何失败都降级为空，不中断对话
        logger.warning(
            "reme semantic recall degraded: %s",
            SensitiveMasker.mask(f"{type(exc).__name__}: {exc}") or "",
        )
        return []
