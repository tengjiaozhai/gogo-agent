"""用真实模型演示 010–011：三轮历史、问题改写、意图识别与语义检查。"""

import asyncio
import json
import os
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from agentscope.credential import OpenAICredential
from dotenv import load_dotenv

from gogo_agent.chat.repository import InMemoryChatHistoryRepository
from gogo_agent.chat.service import ChatHistoryService
from gogo_agent.intent import (
    IntentCategory,
    IntentRecognizer,
    IntentResult,
    QueryInput,
    QueryRewriter,
    RewriteResult,
    RewriteContextBuilder,
    create_text_model,
)


REFERENCE_DATE = date(2026, 9, 28)
QUESTIONS = (
    "请规划2026年10月8日从北京去杭州的两天出差行程，包括往返交通和酒店。",
    "明天去上海呢？",
    "改成三天，那边的酒店也重新选一下。",
)


def _gateway_settings() -> tuple[str, str, str]:
    """从本仓库 .env 读取网关配置，仅返回值，不打印密钥。"""
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    names = ("GOGO_MODEL_API_KEY", "GOGO_MODEL_NAME", "GOGO_MODEL_BASE_URL")
    missing = [name for name in names if not os.environ.get(name, "").strip()]
    if missing:
        raise ValueError("缺少模型配置：" + ", ".join(missing))
    root = os.environ["GOGO_MODEL_BASE_URL"].strip().rstrip("/")
    parsed = urlparse(root)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("GOGO_MODEL_BASE_URL 应是网关根地址，程序会追加 /v1")
    return os.environ["GOGO_MODEL_API_KEY"].strip(), os.environ["GOGO_MODEL_NAME"].strip(), root


def _semantic_issues(
    turn: int, question: str, intent: IntentResult, rewrite: RewriteResult
) -> list[str]:
    """检查这组教学问题的动作、最新地点、日期和时长是否被保留。"""
    issues: list[str] = []
    if rewrite.rewritten_question is None:
        return ["缺少必要上下文，未生成独立问题"]
    rewritten = rewrite.rewritten_question
    if intent.primary_intent is not IntentCategory.ITINERARY_PLANNING:
        issues.append("主要意图未保持为 itinerary_planning")
    if len(intent.intents) != 1 or intent.intents[0].intent is not IntentCategory.ITINERARY_PLANNING:
        issues.append("额外识别了规划之外的业务动作")
    if "北京" not in rewritten or "规划" not in rewritten:
        issues.append("丢失北京出发或规划动作")
    if turn == 1:
        if rewritten != question:
            issues.append("首轮完整问题被无必要地改写")
    else:
        if "上海" not in rewritten or "杭州" in rewritten:
            issues.append("没有使用最近确认的上海目的地")
        if not any(value in rewritten for value in ("2026年9月29日", "2026-09-29", "9月29日")):
            issues.append("没有按参考日期解析并继承明天")
        duration = ("两天", "2天") if turn == 2 else ("三天", "3天")
        if not any(value in rewritten for value in duration):
            issues.append("没有保持本轮要求的行程时长")
        if turn == 3 and "酒店" not in rewritten:
            issues.append("没有保留重新选酒店的要求")
    return issues


async def main() -> int:
    """只调用模型网关；业务历史保存在临时内存仓储，没有外部业务写入。"""
    try:
        api_key, model_name, root = _gateway_settings()
    except ValueError as exc:
        print(f"配置错误：{exc}")
        return 2
    model = create_text_model(
        OpenAICredential(api_key=api_key, base_url=f"{root}/v1"), model_name
    )
    # 只计数：保留原始 SDK 实现与真实网关请求，不替换模型响应。
    original_call = model._call_api
    call_count = 0

    async def counted_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return await original_call(*args, **kwargs)

    model._call_api = counted_call
    history_service = ChatHistoryService(repository=InMemoryChatHistoryRepository())
    builder = RewriteContextBuilder(history_service)
    rewriter = QueryRewriter(model)
    recognizer = IntentRecognizer(model)
    session_id = "learning-010011"
    user_id = "demo-user"
    print("真实模型演示：参考日期固定为 2026-09-28 / Asia/Shanghai")
    print("改写与识别输出来自当前配置的模型网关；助手历史是明确标注的教学输入。")
    print("每步最多一次生成，未注册业务工具；不会创建差旅单或预订订单。")
    try:
        for turn, question in enumerate(QUESTIONS, start=1):
            message_id = history_service.save_user_message(session_id, user_id, question)
            context = builder.build_rewrite_context(
                session_id, user_id,
                current_message_id=message_id,
                reference_date=REFERENCE_DATE,
            )
            print(f"\n第 {turn} 轮｜历史 {len(context.history)} 条｜本轮：{question}")
            for i, message in enumerate(context.history, start=1):
                print(f"  历史 {i} ({message.role})：{message.content}")

            before = call_count
            rewrite = await rewriter.rewrite(context)
            print("真实改写：", json.dumps(rewrite.model_dump(mode="json"), ensure_ascii=False))
            if call_count - before != 1:
                print("验收失败：改写调用次数不是 1")
                return 1
            if rewrite.rewritten_question is None:
                print("本轮需先补齐：", "；".join(rewrite.missing_context))
                return 1

            before = call_count
            decision = await recognizer.recognize(
                QueryInput(question=rewrite.rewritten_question)
            )
            print("真实识别：", json.dumps(decision.model_dump(mode="json"), ensure_ascii=False))
            if call_count - before != 1:
                print("验收失败：识别调用次数不是 1")
                return 1
            if decision.result is None:
                print("验收失败：完整识别没有返回意图")
                return 1
            issues = _semantic_issues(turn, question, decision.result, rewrite)
            if issues:
                print("语义验收失败：", "；".join(issues))
                return 1
            print("语义验收通过：规划动作、最新地点/日期与意图一致")
            if turn < len(QUESTIONS):
                summary = (
                    f"当前讨论的规划需求是：{rewrite.rewritten_question.rstrip('。')}。"
                    "尚未提交差旅申请或预订订单。"
                )
                history_service.save_assistant_message(
                    session_id, user_id, summary, agent_name="TeachingContext"
                )
                print("已加入教学用助手历史；这句摘要不是额外的模型回答。")
        print(f"\n三轮通过，共 {call_count} 次真实模型生成（改写与识别各三次）。")
        return 0
    except Exception as exc:
        # 不打印原始响应、HTTP Header、凭证或真实业务历史。
        code = getattr(exc, "status_code", None)
        print(f"演示失败：{type(exc).__name__}" + (f"，HTTP {code}" if code else ""))
        return 1
    finally:
        await model.client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
