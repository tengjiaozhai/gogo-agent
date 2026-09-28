"""014 意图种子、Qdrant 索引幂等与 Java Top-2 判定基线。"""

import json
from pathlib import Path
from types import SimpleNamespace

from agentscope.embedding import EmbeddingResponse
from agentscope.message import TextBlock
from agentscope.rag import Chunk, QdrantStore
from agentscope.rag._vdb import VectorSearchResult
import pytest
from pydantic import ValidationError

from gogo_agent.intent import (
    IntentCategory,
    IntentRecognizer,
    IntentRuleMatcher,
    MatchStatus,
    QueryInput,
    RecognitionLayer,
)
from gogo_agent.intent.vector import (
    DEFAULT_SEED_PATH,
    IntentSeedCorpus,
    IntentVectorConfig,
    IntentVectorIndex,
    IntentVectorMatcher,
)
from scripts.build_intent_index import settings


class FakeEmbedding:
    """为 SDK 数据流提供确定性向量，不冒充真实语义模型。"""

    dimensions = 16
    supports_multimodal = False

    def __init__(self, corpus: IntentSeedCorpus):
        self.categories = {
            sample: category for category, samples in corpus.categories.items() for sample in samples
        }
        self.calls = 0
        self.inputs = 0

    async def __call__(self, inputs):
        self.calls += 1
        self.inputs += len(inputs)
        vectors = []
        for value in inputs:
            text = value.text if isinstance(value, TextBlock) else value
            category = self.categories.get(text, IntentCategory.UNKNOWN)
            vector = [0.0] * self.dimensions
            vector[list(IntentCategory).index(category)] = 1.0
            vectors.append(vector)
        return EmbeddingResponse(embeddings=vectors)


def test_java_seed_has_68_examples_and_16_categories_without_l1_hits():
    corpus = IntentSeedCorpus.load()
    assert corpus.sample_count == 68
    assert set(corpus.categories) == set(IntentCategory)
    assert len(corpus.categories[IntentCategory.UNKNOWN]) == 7
    assert len(corpus.categories[IntentCategory.TRAIN_SEARCH]) == 3


def test_seed_rejects_duplicate_missing_and_l1_hit(tmp_path: Path):
    data = json.loads(DEFAULT_SEED_PATH.read_text(encoding="utf-8"))
    data["categories"]["travel_cancel"][0] = data["categories"]["travel_application"][0]
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        IntentSeedCorpus.load(path)
    data["categories"]["travel_cancel"][0] = "取消出差"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="L1"):
        IntentSeedCorpus.load(path)
    del data["categories"]["unknown"]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="覆盖"):
        IntentSeedCorpus.load(path)


def test_fingerprint_changes_only_for_index_affecting_configuration():
    corpus = IntentSeedCorpus.load()
    first = corpus.fingerprint(IntentVectorConfig())
    assert first == corpus.fingerprint(IntentVectorConfig(score_threshold=0.8, top_k=3))
    assert first != corpus.fingerprint(IntentVectorConfig(index_version="2"))
    assert first != corpus.fingerprint(IntentVectorConfig(embedding_model="another-embedding"))
    assert first != corpus.fingerprint(IntentVectorConfig(dimensions=512))


def test_embedding_settings_reuse_chat_gateway(monkeypatch):
    monkeypatch.setenv("GOGO_MODEL_API_KEY", "shared-key")
    monkeypatch.setenv("GOGO_MODEL_BASE_URL", "https://gateway.example.test")
    monkeypatch.setenv("GOGO_INTENT_EMBEDDING_API_KEY", "ignored-key")
    monkeypatch.setenv("GOGO_INTENT_EMBEDDING_BASE_URL", "https://ignored.example.test/v1")
    config, key, base_url = settings()
    assert key == "shared-key"
    assert base_url == "https://gateway.example.test/v1"
    assert config.embedding_model == "qwen3.7-text-embedding"


@pytest.mark.parametrize("kwargs", [
    {"top_k": 1},
    {"score_threshold": -0.01},
    {"ambiguity_margin": 1.1},
    {"high_confidence_threshold": 0.6},
    {"dimensions": 0},
])
def test_invalid_vector_config_rejected(kwargs):
    with pytest.raises(ValidationError):
        IntentVectorConfig(**kwargs)


@pytest.mark.asyncio
async def test_qdrant_sdk_build_reuse_and_exact_seed_lookup():
    corpus = IntentSeedCorpus.load()
    embedding = FakeEmbedding(corpus)
    config = IntentVectorConfig(dimensions=16)
    async with QdrantStore(location=":memory:") as store:
        index = IntentVectorIndex(config, store, embedding, corpus)
        with pytest.raises(RuntimeError, match="missing"):
            await index.assert_ready()
        assert not await store.has_collection(index.collection)
        assert await index.build() == "built"
        assert embedding.calls == 7
        assert embedding.inputs == 68
        assert await index.build() == "reused"
        assert embedding.calls == 7
        documents = await index.knowledge_base.list_documents()
        assert len(documents) == 1
        assert documents[0].chunk_count == 68
        assert documents[0].document_id == index.document_id
        query = corpus.categories[IntentCategory.APPROVAL_QUERY][0]
        result = await IntentVectorMatcher(index).match(QueryInput(question=query))
        assert result.status is MatchStatus.HIT
        assert result.result.primary_intent is IntentCategory.APPROVAL_QUERY
        assert len(result.candidates) == 2
        assert all(item.intent is IntentCategory.APPROVAL_QUERY for item in result.candidates)
        recognizer = IntentRecognizer(
            object(), rule_matcher=IntentRuleMatcher(), vector_matcher=IntentVectorMatcher(index)
        )
        decision = await recognizer.recognize(QueryInput(question=query), fast_only=True)
        assert decision.hit_layer is RecognitionLayer.VECTOR
        assert decision.result.primary_intent is IntentCategory.APPROVAL_QUERY
        before = embedding.calls
        direct = await recognizer.recognize(QueryInput(question="查机票"), fast_only=True)
        assert direct.hit_layer is RecognitionLayer.RULE
        assert embedding.calls == before


@pytest.mark.asyncio
async def test_partial_qdrant_index_is_repaired_without_duplicate_points():
    corpus = IntentSeedCorpus.load()
    embedding = FakeEmbedding(corpus)
    async with QdrantStore(location=":memory:") as store:
        index = IntentVectorIndex(IntentVectorConfig(dimensions=16), store, embedding, corpus)
        await index.knowledge_base.insert_document(index._chunks()[:10], document_id=index.document_id)
        assert await index.build() == "repaired"
        documents = await index.knowledge_base.list_documents()
        assert len(documents) == 1
        assert documents[0].chunk_count == 68


def _hit(intent: IntentCategory, score: float, *, doc_id="intent-seed:test") -> VectorSearchResult:
    return VectorSearchResult(
        score=score,
        document_id=doc_id,
        chunk=Chunk(
            content=TextBlock(text=f"{intent.value} 的相似样例"),
            source="intent-seed.yml", chunk_index=0, total_chunks=2,
            metadata={"intent": intent.value, "seed_version": "test"},
        ),
    )


class StubIndex:
    """只替换检索得分，保留真实 L2 判定实现供 Java 对照。"""

    config = IntentVectorConfig()
    document_id = "intent-seed:test"
    fingerprint = "test"

    def __init__(self, hits):
        self.hits = hits
        self.search_args = None
        self.knowledge_base = self

    async def assert_ready(self):
        return None

    async def search(self, queries, *, top_k, score_threshold):
        self.search_args = (queries, top_k, score_threshold)
        return self.hits


@pytest.mark.asyncio
@pytest.mark.parametrize("hits,status,primary,confidence", [
    ([(_hit(IntentCategory.FLIGHT_SEARCH, .90)), (_hit(IntentCategory.TRAIN_SEARCH, .76))], MatchStatus.HIT, IntentCategory.FLIGHT_SEARCH, "high"),
    ([(_hit(IntentCategory.POLICY_QUERY, .80)), (_hit(IntentCategory.POLICY_QUERY, .79))], MatchStatus.HIT, IntentCategory.POLICY_QUERY, "medium"),
    ([(_hit(IntentCategory.FLIGHT_SEARCH, .82)), (_hit(IntentCategory.TRAIN_SEARCH, .79))], MatchStatus.AMBIGUOUS, None, None),
    ([(_hit(IntentCategory.FLIGHT_SEARCH, .60))], MatchStatus.MISS, None, None),
    ([(_hit(IntentCategory.REIMBURSEMENT, .88))], MatchStatus.HIT, IntentCategory.REIMBURSEMENT, "high"),
])
async def test_java_top2_score_threshold_and_margin_baseline(hits, status, primary, confidence):
    index = StubIndex(hits)
    result = await IntentVectorMatcher(index).match(QueryInput(question="查询固定句"))
    assert result.status is status
    assert result.threshold == .75
    assert [candidate.score for candidate in result.candidates] == [hit.score for hit in hits]
    assert index.search_args == (["查询固定句"], 2, 0.0)
    if primary is None:
        assert result.result is None
    else:
        assert result.result.primary_intent is primary
        assert result.result.intents[0].confidence == confidence


@pytest.mark.asyncio
async def test_foreign_or_unversioned_hit_fails_closed():
    index = StubIndex([_hit(IntentCategory.FLIGHT_SEARCH, .91, doc_id="other")])
    with pytest.raises(ValueError, match="来源"):
        await IntentVectorMatcher(index).match(QueryInput(question="查航班"))
