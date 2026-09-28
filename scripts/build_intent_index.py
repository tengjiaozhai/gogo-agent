"""014 复用项目模型网关，在 Qdrant 构建和检查意图索引。"""

import argparse
import asyncio

from agentscope.credential import OpenAICredential
from agentscope.embedding import OpenAIEmbeddingModel
from agentscope.rag import QdrantStore

from gogo_agent.intent import (
    IntentSeedCorpus,
    IntentVectorConfig,
    IntentVectorIndex,
    IntentVectorMatcher,
    QueryInput,
)
from gogo_agent.intent.runtime import load_intent_runtime_settings


def settings() -> tuple[IntentVectorConfig, str, str]:
    """复用项目网关凭证与根地址，单独配置 embedding 模型和索引参数。"""
    current = load_intent_runtime_settings()
    return current.vector, current.api_key, current.base_url


async def run(*, probe: bool) -> None:
    """构建或复用版本化索引，可选输出固定句的 Top-k 判定。"""
    config, api_key, base_url = settings()
    embedding = OpenAIEmbeddingModel(
        credential=OpenAICredential(api_key=api_key, base_url=base_url),
        model=config.embedding_model,
        dimensions=config.dimensions,
        max_retries=0,
    )
    embedding.client.max_retries = 0
    try:
        vector_store = QdrantStore(url=config.qdrant_url)
        async with vector_store:
            index = IntentVectorIndex(config, vector_store, embedding, IntentSeedCorpus.load())
            outcome = await index.build()
            print(f"索引：{outcome}；集合：{index.collection}；样例：{index.corpus.sample_count}；模型：{config.embedding_model}")
            if probe:
                matcher = IntentVectorMatcher(index)
                questions = (
                    "我交上去那个流程现在走到哪一步了",
                    "公司派我下周去青岛开会，帮我把手续走一下",
                    "我的费用单据要怎么交给财务",
                    "去杭州有没有合适的飞机班次",
                    "给我写个快速排序算法",
                    "我这个月都有哪几趟公务，月底行业展会那趟算上了吗",
                )
                for question in questions:
                    matched = await matcher.match(QueryInput(question=question))
                    top = [(item.intent.value, round(item.score, 4)) for item in matched.candidates]
                    print(f"{question} -> {matched.status.value} / {top}")
    finally:
        await embedding.client.close()


def main() -> None:
    """终端入口；未配置 embedding 时在发起任何外部写入前失败。"""
    parser = argparse.ArgumentParser(description="构建版本化意图种子索引")
    parser.add_argument("--probe", action="store_true", help="构建后查询六条固定句并打印 Top-k")
    args = parser.parse_args()
    asyncio.run(run(probe=args.probe))


if __name__ == "__main__":
    main()
