"""018 用真实规则、Qdrant 和模型逐条对照意图误路由回归语料。"""

import argparse
import asyncio
from datetime import date
import json
from pathlib import Path

from gogo_agent.chat.repository import InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.intent.runtime import open_intent_pipeline
from gogo_agent.request_context import RequestContext


CORPUS_PATH = Path(__file__).resolve().parents[1] / "docs/契约样例/018-意图回归语料.json"


def load_cases() -> tuple[date, list[dict]]:
    """读取固定参考日期与静态语料，拒绝不支持的格式版本。"""
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    if corpus.get("schema_version") != 1:
        raise ValueError("018 回归语料 schema_version 必须为 1")
    return date.fromisoformat(corpus["reference_date"]), corpus["cases"]


async def run(selected: str) -> int:
    """每条语料使用独立会话，失败只输出样例名和层级，不回显网关秘密。"""
    reference_date, cases = load_cases()
    if selected != "all":
        cases = [case for case in cases if case["id"] == selected]
        if not cases:
            raise ValueError(f"不存在样例：{selected}")
    history = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    failures = 0
    async with open_intent_pipeline(history) as pipeline:
        for case in cases:
            session_id = f"learning-018-{case['id']}"
            user_id = "demo-user"
            for message in case["history"]:
                if message["role"] == "user":
                    history.save_user_message(session_id, user_id, message["content"])
                else:
                    history.save_assistant_message(
                        session_id, user_id, message["content"], agent_name="TeachingContext",
                    )
            message_id = history.save_user_message(session_id, user_id, case["question"])
            request = RequestContext(user_id=user_id, session_id=session_id, request_id=message_id)
            try:
                prepared = await pipeline.prepare(
                    request,
                    reference_date=reference_date,
                )
            except Exception as exc:
                failures += 1
                print(f"[FAIL] {case['id']}：{type(exc).__name__}")
                continue

            expected = case["expected"]
            actual_layer = prepared.decision.hit_layer.value if prepared.decision else None
            actual_intents = [item.intent.value for item in prepared.decision.result.intents] if prepared.decision else []
            rewritten = prepared.rewrite.rewritten_question if prepared.rewrite else None
            issues = []
            if prepared.branch != expected["branch"]:
                issues.append(f"分支期望 {expected['branch']}，实际 {prepared.branch}")
            if actual_layer != expected["layer"]:
                issues.append(f"层级期望 {expected['layer']}，实际 {actual_layer}")
            if actual_intents != expected["intents"]:
                issues.append(f"意图期望 {expected['intents']}，实际 {actual_intents}")
            for fragment in expected.get("rewrite_contains", []):
                if rewritten is None or fragment not in rewritten:
                    issues.append(f"改写缺少「{fragment}」")
            for fragment in expected.get("rewrite_excludes", []):
                if rewritten is not None and fragment in rewritten:
                    issues.append(f"改写仍含「{fragment}」")
            if issues:
                failures += 1
            print(f"[{'FAIL' if issues else 'PASS'}] {case['id']}：{case['question']}")
            print(f"  之前：{case['before']}")
            print(f"  改写：{rewritten}")
            print(f"  现在：{prepared.branch} / {actual_layer} / {actual_intents}")
            for issue in issues:
                print(f"  差异：{issue}")
    print(f"\n018 回归：{len(cases) - failures}/{len(cases)} 通过")
    return 0 if failures == 0 else 1


def main() -> None:
    """传 --case 可只调试一条真实样例，默认遍历全部样例。"""
    parser = argparse.ArgumentParser(description="用真实模型检查 018 意图回归语料")
    parser.add_argument("--case", default="all", help="语料 id；默认 all")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.case)))


if __name__ == "__main__":
    main()
