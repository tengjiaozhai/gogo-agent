"""014 意图种子索引与 Qdrant L2 近邻识别。"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from agentscope.message import TextBlock
from agentscope.rag import Chunk, KnowledgeBase, QdrantStore
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import (
    ConfidenceLevel,
    FastMatch,
    IntentCandidate,
    IntentCategory,
    IntentItem,
    IntentResult,
    MatchStatus,
    QueryInput,
    RecognitionLayer,
)
from .rules import IntentRuleMatcher


DEFAULT_SEED_PATH = Path(__file__).with_name("seed.json")
_EMBEDDING_BATCH_SIZE = 10  # 避免单次 68 条超过网关的批量输入限制。


class IntentVectorConfig(BaseModel):
    """L2 向量索引模型、维度、版本和 Java 判定阈值的独立配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    qdrant_url: str = Field(default="http://172.22.22.123:6333", min_length=1, description="意图专用 Qdrant 服务地址")
    embedding_model: str = Field(default="qwen3.7-text-embedding", min_length=1, description="意图索引与查询必须共用的 embedding 模型名")
    dimensions: int = Field(default=1024, ge=1, description="embedding 输出向量维度，默认沿用 Java 基线")
    index_version: str = Field(default="1", min_length=1, description="人工提升的意图索引版本，变更时切换物理集合")
    top_k: int = Field(default=2, ge=2, description="向量近邻数量，至少为二以检查跨类别分差")
    score_threshold: float = Field(default=0.75, ge=0, le=1, description="L2 单意图最低余弦相似度")
    ambiguity_margin: float = Field(default=0.05, ge=0, le=1, description="前两名跨类别且分差小于此值时弃权")
    high_confidence_threshold: float = Field(default=0.85, ge=0, le=1, description="达到此相似度时标记为高置信，否则命中为中置信")

    @model_validator(mode="after")
    def validate_thresholds(self) -> "IntentVectorConfig":
        """高置信界限不能低于命中界限。"""
        if self.high_confidence_threshold < self.score_threshold:
            raise ValueError("high_confidence_threshold 不能低于 score_threshold")
        return self


@dataclass(frozen=True)
class IntentSeedCorpus:
    """已校验类别覆盖、样例唯一性及 L1 分层边界的 Java 意图语料。"""

    categories: dict[IntentCategory, tuple[str, ...]]
    source: str

    @classmethod
    def load(cls, path: Path = DEFAULT_SEED_PATH) -> "IntentSeedCorpus":
        """读取仓库内固定种子，拒绝缺类、重复或已被 L1 捕获的样例。"""
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("意图种子 schema_version 必须为 1")
        source, raw = data.get("source"), data.get("categories")
        if not isinstance(source, str) or not source or not isinstance(raw, dict):
            raise ValueError("意图种子缺少 source 或 categories")
        expected = {category.value for category in IntentCategory}
        if set(raw) != expected:
            raise ValueError(f"意图种子类别必须恰好覆盖 {len(expected)} 类")

        categories: dict[IntentCategory, tuple[str, ...]] = {}
        seen: set[str] = set()
        matcher = IntentRuleMatcher()
        for category in IntentCategory:
            samples = raw[category.value]
            if not isinstance(samples, list) or len(samples) < 3:
                raise ValueError(f"{category.value} 至少需要三条样例")
            checked: list[str] = []
            for sample in samples:
                if not isinstance(sample, str) or not sample.strip() or sample != sample.strip():
                    raise ValueError(f"{category.value} 存在空白或非法样例")
                if sample in seen:
                    raise ValueError(f"重复意图样例：{sample}")
                if matcher.match(QueryInput(question=sample)).status is MatchStatus.HIT:
                    raise ValueError(f"{category.value} 样例已被 L1 捕获，应留给 L2 的语料不能重复覆盖")
                seen.add(sample)
                checked.append(sample)
            categories[category] = tuple(checked)
        return cls(categories=categories, source=source)

    def fingerprint(self, config: IntentVectorConfig) -> str:
        """语料、embedding 模型、维度和人工版本共同决定物理索引。"""
        content = {
            "categories": {category.value: self.categories[category] for category in IntentCategory},
            "embedding_model": config.embedding_model,
            "dimensions": config.dimensions,
            "index_version": config.index_version,
        }
        encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()[:16]

    @property
    def sample_count(self) -> int:
        """返回所有类别的样例数。"""
        return sum(map(len, self.categories.values()))


class IntentVectorIndex:
    """管理一个专用、版本化的 Qdrant 意图集合及其完整性。"""

    def __init__(
        self,
        config: IntentVectorConfig,
        vector_store: QdrantStore,
        embedding_model: object,
        corpus: IntentSeedCorpus | None = None,
    ) -> None:
        self.config = config
        self.vector_store = vector_store
        self.corpus = corpus or IntentSeedCorpus.load()
        if getattr(embedding_model, "dimensions", None) != config.dimensions:
            raise ValueError("embedding 模型维度与意图索引配置不一致")
        self.fingerprint = self.corpus.fingerprint(config)
        self.collection = f"gogo_intent_{self.fingerprint}"
        self.document_id = f"intent-seed:{self.fingerprint}"
        self.knowledge_base = KnowledgeBase(
            name="intent-examples",
            description="仅用于用户问题的意图分类，不存放差旅政策或业务文档。",
            embedding_model=embedding_model,
            vector_store=vector_store,
            collection=self.collection,
        )
        self._ready = False

    def _chunks(self) -> list[Chunk]:
        """按固定类别和样例顺序生成可重复的全局 chunk_index。"""
        chunks: list[Chunk] = []
        total = self.corpus.sample_count
        for category in IntentCategory:
            for sample_index, sample in enumerate(self.corpus.categories[category]):
                chunks.append(Chunk(
                    content=TextBlock(text=sample),
                    source=self.corpus.source,
                    chunk_index=len(chunks),
                    total_chunks=total,
                    metadata={
                        "intent": category.value,
                        "seed_version": self.fingerprint,
                        "sample_index": sample_index,
                    },
                ))
        return chunks

    async def _existing_chunks(self) -> list[Chunk] | None:
        """检查集合维度与来源；None 表示集合尚不存在。"""
        if not await self.vector_store.has_collection(self.collection):
            return None
        info = await self.vector_store.get_client().get_collection(self.collection)
        vector_params = info.config.params.vectors
        size = getattr(vector_params, "size", None)
        distance = getattr(vector_params, "distance", None)
        if size != self.config.dimensions or getattr(distance, "value", distance) != "Cosine":
            raise ValueError("同名意图集合的维度或距离度量不符合当前配置")
        documents = await self.knowledge_base.list_documents()
        if any(document.document_id != self.document_id for document in documents):
            raise ValueError("意图集合存在非当前版本文档，拒绝混合索引")
        if any(document.chunk_count > self.corpus.sample_count for document in documents):
            raise ValueError("意图集合包含重复或多余的种子记录")
        return await self.knowledge_base.list_chunks(self.document_id, limit=self.corpus.sample_count + 1)

    async def _inspect(self) -> str:
        """返回 missing、partial 或 ready；内容冲突时明确失败。"""
        found = await self._existing_chunks()
        if found is None:
            return "missing"
        expected = self._chunks()
        if len(found) > len(expected):
            raise ValueError("意图集合包含多余的种子记录")
        for chunk in found:
            index = chunk.chunk_index
            if index < 0 or index >= len(expected):
                raise ValueError("意图集合包含越界的样例索引")
            reference = expected[index]
            if (
                not isinstance(chunk.content, TextBlock)
                or chunk.content.text != reference.content.text
                or chunk.source != reference.source
                or chunk.total_chunks != reference.total_chunks
                or chunk.metadata != reference.metadata
            ):
                raise ValueError("意图集合已有与当前语料不一致的样例")
        if [chunk.chunk_index for chunk in found] != list(range(len(expected))):
            return "partial"
        return "ready"

    async def build(self) -> str:
        """首次批量 embedding 建索引；重复构建跳过调用，残缺时确定性补齐。"""
        state = await self._inspect()
        if state == "ready":
            self._ready = True
            return "reused"
        chunks = self._chunks()
        for offset in range(0, len(chunks), _EMBEDDING_BATCH_SIZE):
            await self.knowledge_base.insert_document(
                chunks[offset:offset + _EMBEDDING_BATCH_SIZE], document_id=self.document_id,
            )
        if await self._inspect() != "ready":
            raise RuntimeError("意图索引写入后仍不完整")
        self._ready = True
        return "built" if state == "missing" else "repaired"

    async def assert_ready(self) -> None:
        """查询前只读校验，禁止 KnowledgeBase 自动创建空集合。"""
        if not self._ready:
            state = await self._inspect()
            if state != "ready":
                raise RuntimeError(f"意图索引未就绪：{state}；先运行构建脚本")
            self._ready = True


class IntentVectorMatcher:
    """按 Java Top-2、阈值与跨类分差，把 Qdrant 命中转换为 L2 结果。"""

    def __init__(self, index: IntentVectorIndex):
        self.index = index

    async def match(self, query: QueryInput) -> FastMatch:
        """检索原问题；低分或跨类近分差弃权，供 L3 再判断。"""
        if not isinstance(query, QueryInput):
            raise TypeError("向量匹配需要经过校验的 QueryInput")
        await self.index.assert_ready()
        config = self.index.config
        hits = await self.index.knowledge_base.search(
            [query.question], top_k=config.top_k, score_threshold=0.0,
        )
        candidates: list[IntentCandidate] = []
        for hit in hits:
            if hit.document_id != self.index.document_id or hit.chunk.metadata.get("seed_version") != self.index.fingerprint:
                raise ValueError("向量命中来源不属于当前意图索引")
            score = hit.score
            if score < 0:
                continue
            if score > 1.000001:
                raise ValueError("Qdrant 余弦相似度超过 1")
            score = min(score, 1.0)
            category = IntentCategory(hit.chunk.metadata["intent"])
            if not isinstance(hit.chunk.content, TextBlock):
                raise ValueError("意图样例必须是文本")
            candidates.append(IntentCandidate(
                intent=category,
                layer=RecognitionLayer.VECTOR,
                score=score,
                reason=f"相似样例「{hit.chunk.content.text}」",
            ))

        threshold = config.score_threshold
        if not candidates or candidates[0].score < threshold:
            return FastMatch(
                status=MatchStatus.MISS, result=None, candidates=candidates,
                threshold=threshold, reason=f"L2 最高相似度低于 {threshold:.2f} 或无候选",
            )

        top = candidates[0]
        if (
            len(candidates) >= 2
            and top.intent is not candidates[1].intent
            and top.score - candidates[1].score < config.ambiguity_margin
        ):
            return FastMatch(
                status=MatchStatus.AMBIGUOUS, result=None, candidates=candidates,
                threshold=threshold, reason="L2 前两名属于不同意图且分差过小",
            )

        confidence = (
            ConfidenceLevel.HIGH if top.score >= config.high_confidence_threshold
            else ConfidenceLevel.MEDIUM
        )
        reason = f"L2 {top.intent.value} 相似度 {top.score:.3f}，命中 {threshold:.2f} 阈值"
        return FastMatch(
            status=MatchStatus.HIT,
            result=IntentResult(
                intents=[IntentItem(
                    intent=top.intent, confidence=confidence, reason=reason,
                    evidence=[query.question, top.reason],
                )],
                primary_intent=top.intent,
                multi_intent=False,
                overall_reason=reason,
            ),
            candidates=candidates,
            threshold=threshold,
            reason=reason,
        )
