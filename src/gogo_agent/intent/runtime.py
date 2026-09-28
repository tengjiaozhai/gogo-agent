"""017 从共用网关配置装配一次请求所需的真实意图流水线。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator
from urllib.parse import urlparse

from agentscope.credential import DeepSeekCredential, OpenAICredential
from agentscope.embedding import OpenAIEmbeddingModel
from agentscope.rag import QdrantStore
from dotenv import load_dotenv

if TYPE_CHECKING:
    from gogo_agent.chat.service import ChatHistoryService

from .context import RewriteContextBuilder
from .pipeline import IntentPipelineService
from .rules import IntentRuleMatcher
from .service import IntentRecognizer, QueryRewriter, create_text_model
from .vector import IntentVectorConfig, IntentVectorIndex, IntentVectorMatcher


@dataclass(frozen=True)
class IntentRuntimeSettings:
    """同一网关的聊天模型、embedding 模型与 Qdrant 索引配置。"""

    api_key: str = field(repr=False)
    base_url: str
    chat_model_name: str
    vector: IntentVectorConfig


def load_intent_runtime_settings() -> IntentRuntimeSettings:
    """复用 GOGO_MODEL 凭证；模型名和索引参数分别读取环境配置。"""
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    key = os.getenv("GOGO_MODEL_API_KEY", "").strip()
    root_url = os.getenv("GOGO_MODEL_BASE_URL", "").strip().rstrip("/")
    if not key or not root_url:
        raise ValueError("请在 .env 设置 GOGO_MODEL_API_KEY 和 GOGO_MODEL_BASE_URL")
    parsed = urlparse(root_url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.path not in ("", "/v1")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("GOGO_MODEL_BASE_URL 必须是完整的 HTTP(S) 网关根地址或 /v1 地址")
    base_url = root_url if root_url.endswith("/v1") else f"{root_url}/v1"
    vector = IntentVectorConfig(
        qdrant_url=os.getenv("GOGO_INTENT_QDRANT_URL") or "http://172.22.22.123:6333",
        embedding_model=os.getenv("GOGO_INTENT_EMBEDDING_MODEL") or "qwen3.7-text-embedding",
        dimensions=int(os.getenv("GOGO_INTENT_EMBEDDING_DIMENSIONS") or "1024"),
        index_version=os.getenv("GOGO_INTENT_INDEX_VERSION") or "1",
        top_k=int(os.getenv("GOGO_INTENT_TOP_K") or "2"),
        score_threshold=float(os.getenv("GOGO_INTENT_SCORE_THRESHOLD") or "0.75"),
        ambiguity_margin=float(os.getenv("GOGO_INTENT_AMBIGUITY_MARGIN") or "0.05"),
        high_confidence_threshold=float(os.getenv("GOGO_INTENT_HIGH_CONFIDENCE_THRESHOLD") or "0.85"),
    )
    return IntentRuntimeSettings(
        api_key=key,
        base_url=base_url,
        chat_model_name=os.getenv("GOGO_MODEL_NAME", "").strip(),
        vector=vector,
    )


@asynccontextmanager
async def open_intent_pipeline(history_service: ChatHistoryService) -> AsyncIterator[IntentPipelineService]:
    """每次请求创建并关闭模型与 Qdrant 客户端，不在聊天请求中重建索引。"""
    settings = load_intent_runtime_settings()
    if not settings.chat_model_name:
        raise ValueError("请在 .env 设置 GOGO_MODEL_NAME")
    text_model = create_text_model(
        DeepSeekCredential(api_key=settings.api_key, base_url=settings.base_url),
        settings.chat_model_name,
    )
    embedding = OpenAIEmbeddingModel(
        credential=OpenAICredential(api_key=settings.api_key, base_url=settings.base_url),
        model=settings.vector.embedding_model,
        dimensions=settings.vector.dimensions,
        max_retries=0,
    )
    embedding.client.max_retries = 0
    try:
        async with QdrantStore(url=settings.vector.qdrant_url) as store:
            index = IntentVectorIndex(settings.vector, store, embedding)
            yield IntentPipelineService(
                RewriteContextBuilder(history_service),
                QueryRewriter(text_model),
                IntentRecognizer(
                    text_model,
                    rule_matcher=IntentRuleMatcher(),
                    vector_matcher=IntentVectorMatcher(index),
                ),
            )
    finally:
        await embedding.client.close()
        await text_model.client.close()
