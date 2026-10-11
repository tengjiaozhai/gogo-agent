"""040 主答复完成后的单次轻量问题推荐，不参与 ReAct 与业务决策。"""

from __future__ import annotations

import asyncio
import json

from agentscope.message import SystemMsg, TextBlock, ToolCallBlock, UserMsg
from agentscope.model import ChatModelBase, ChatResponse
from pydantic import BaseModel, ConfigDict, Field


class RecommendationOutput(BaseModel):
    """推荐模型的一次结构化输出。"""

    model_config = ConfigDict(extra="forbid")

    questions: list[str] = Field(..., description="根据主答复建议用户下一步可问的问题")


_PROMPT = (
    "你只负责生成下一步可问的问题，不回答问题，不调用工具。"
    "根据本轮用户问题和已经完成的助手答复，输出 JSON 对象 questions 数组，最多四项。"
    "问题应具体、简短、不重复，不提出已经完成的操作，也不要建议用户提供密钥或完整身份证号。"
    "不能暗示差旅申请、审批或供应商订单已经完成；不能给政策金额结论。"
    "如果没有合适建议，输出空数组。"
)
_CONTINUATION_WORDS = frozenset({"确定", "确认", "提交", "继续", "取消", "修改", "好的", "是的", "ok", "yes"})


def normalize_questions(raw: list[str], user_question: str, answer: str) -> list[str]:
    """去重并移除过长、空白、纯续跑词和已回答的问题。"""
    selected: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        question = item.strip()
        key = question.rstrip("？?。 ").casefold()
        if (
            not key or len(question) > 60 or key in seen
            or key == user_question.strip().rstrip("？?。 ").casefold()
            or key in _CONTINUATION_WORDS
            or question in answer
        ):
            continue
        seen.add(key)
        selected.append(question)
        if len(selected) == 4:
            break
    return selected


async def generate_recommendations(
    model: ChatModelBase, user_question: str, answer: str, *, timeout_seconds: float = 8,
) -> list[str]:
    """主答复已保存后调用一次快模型；无有效结构化结果则返回空列表。"""
    if not answer.strip():
        return []
    async with asyncio.timeout(timeout_seconds):
        response = await model(
            [
                SystemMsg(name="system", content=_PROMPT),
                UserMsg(name="user", content=json.dumps({
                    "question": user_question[:500], "answer": answer[:1200],
                }, ensure_ascii=False)),
            ],
            tools=None, tool_choice=None,
            response_format={"type": "json_object"}, max_tokens=300,
        )
    if not isinstance(response, ChatResponse) or not response.is_last:
        return []
    if getattr(response.finished_reason, "value", response.finished_reason) != "completed":
        return []
    if any(isinstance(block, ToolCallBlock) for block in response.content):
        return []
    text = "".join(block.text for block in response.content if isinstance(block, TextBlock))
    try:
        output = RecommendationOutput.model_validate_json(text)
    except ValueError:
        return []
    return normalize_questions(output.questions, user_question, answer)
