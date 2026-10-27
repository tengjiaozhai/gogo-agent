"""053 主观评估器：三个有限的模型调用（体验、韧性、偏好），输出结构化结论。

每个评估器只调用一次模型，不调用工具、不读写业务数据。模型输出无法解析、调用超时或异常时，
`review_plan` 会把该评估器记为不可用并保留在审核结论中，不会当作通过。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

from agentscope.message import SystemMsg, TextBlock, ToolCallBlock, UserMsg
from agentscope.model import ChatModelBase, ChatResponse
from pydantic import BaseModel, ConfigDict, Field

from gogo_agent.model import structured_response_format
from gogo_agent.context.input import PROMPT_VERSION

from .models import ReviewIssue, ReviewResult, ReviewSource, ReviewVerdict
from .review import Itinerary

# 20 秒对推理模型偏紧：实测单次最长 14.5 秒，留出网关抖动余量。外层 review 仍有 30 秒总闸。
ASSESSOR_TIMEOUT_SECONDS = 25.0
# 400 会被思考链吃光，导致正文为空、被当成「主观评估不可用」。正文只需 200 token 左右，
# 余量全部留给 thinking。网关按 32k 封顶；再大也不会报错，只是被静默夹到实际容量。
MAX_OUTPUT_TOKENS = 32768


class AssessmentOutput(BaseModel):
    """模型对单个维度的结构化输出。"""

    model_config = ConfigDict(extra="forbid")

    verdict: ReviewVerdict = Field(..., description="该维度判定：pass、warning 或 fail")
    issues: list[str] = Field(default_factory=list, max_length=5, description="发现的问题，中文短句，最多五条")


_COMMON_RULES = (
    "你只评估给定的行程方案，不调用工具，不提出下单、取消或修改差旅单。"
    "只能依据输入中的字段判断；字段缺失时判 warning 并说明缺什么。"
    "输出一个 JSON 对象，包含 verdict（pass、warning 或 fail）与 issues（字符串数组）。"
)
PROMPTS: dict[ReviewSource, str] = {
    ReviewSource.EXPERIENCE: _COMMON_RULES + (
        "评估维度：出行体验。关注红眼航班、到达过晚、单程通勤过长、换乘或返程仓促。"
        "体验明显不可接受判 fail，有需要注意的地方判 warning。"
    ),
    ReviewSource.RESILIENCE: _COMMON_RULES + (
        "评估维度：韧性。关注航班或班次取消后的备选、退改限制、返程时间余量。"
        "没有任何退改或备选信息时判 warning；存在明显无法补救的风险判 fail。"
    ),
    ReviewSource.PREFERENCE: _COMMON_RULES + (
        "评估维度：偏好匹配。对照用户在 preferences 中的表述与方案。"
        "preferences 为空时判 pass 并说明无偏好约束。方案明显违背用户明确偏好时判 fail。"
    ),
}


def _itinerary_view(itinerary: Itinerary) -> dict[str, object]:
    """只向模型暴露方案字段，不包含任何凭证、用户标识或数据库标识。"""
    return {
        "origin": itinerary.origin,
        "destination": itinerary.destination,
        "departure_date": itinerary.departure_date.isoformat(),
        "return_date": itinerary.return_date.isoformat(),
        "outbound": {
            "type": itinerary.outbound.type.value, "code": itinerary.outbound.code,
            "departure_time": itinerary.outbound.departure_time,
            "arrival_time": itinerary.outbound.arrival_time, "price": itinerary.outbound.price,
            "cabin_class": itinerary.outbound.cabin_class, "refund_policy": itinerary.outbound.refund_policy,
        },
        "hotel": {
            "name": itinerary.hotel.name, "price_per_night": itinerary.hotel.price_per_night,
            "nights": itinerary.hotel.nights, "cancel_policy": itinerary.hotel.cancel_policy,
        },
        "inbound": {
            "type": itinerary.inbound.type.value, "code": itinerary.inbound.code,
            "departure_time": itinerary.inbound.departure_time,
            "arrival_time": itinerary.inbound.arrival_time, "price": itinerary.inbound.price,
            "cabin_class": itinerary.inbound.cabin_class, "refund_policy": itinerary.inbound.refund_policy,
        },
        "total_price": itinerary.total_price,
        "budget": itinerary.budget,
        "preferences": itinerary.preferences,
    }


class ModelSubjectiveAssessor:
    """调用一次模型并解析为 ReviewResult；任何结构或状态异常都抛出，由审核层转为不可用。"""

    def __init__(self, source: ReviewSource, model: ChatModelBase, *, timeout_seconds: float = ASSESSOR_TIMEOUT_SECONDS):
        if source not in PROMPTS:
            raise ValueError("该处理器没有主观评估提示词")
        self.source = source
        self._model = model
        self._timeout_seconds = timeout_seconds

    async def assess(self, itinerary: Itinerary) -> ReviewResult:
        messages = [
            SystemMsg(name="system", content=f"[提示词版本 {PROMPT_VERSION}]\n" + PROMPTS[self.source]),
            UserMsg(name="user", content=json.dumps(_itinerary_view(itinerary), ensure_ascii=False)),
        ]
        async with asyncio.timeout(self._timeout_seconds):
            response = await self._model(
                messages, tools=None, tool_choice=None,
                response_format=structured_response_format(AssessmentOutput), max_tokens=MAX_OUTPUT_TOKENS,
            )
        text = _completed_text(response)
        output = AssessmentOutput.model_validate_json(text)
        severity = ReviewVerdict.FAIL if output.verdict is ReviewVerdict.FAIL else ReviewVerdict.WARNING
        issues = [
            ReviewIssue(dimension=self.source.value, severity=severity, evidence=item,
                        suggestion="按问题调整方案，或在说明中注明例外原因")
            for item in output.issues
        ]
        return ReviewResult(source=self.source, verdict=output.verdict, issues=issues)


def _completed_text(response: object) -> str:
    """只接受一次完整、无工具调用的文本回复；其余情况视为无效输出。"""
    if not isinstance(response, ChatResponse) or not response.is_last:
        raise ValueError("模型没有返回完整回复")
    if getattr(response.finished_reason, "value", response.finished_reason) != "completed":
        raise ValueError("模型回复未正常结束")
    if any(isinstance(block, ToolCallBlock) for block in response.content):
        raise ValueError("主观评估不允许工具调用")
    text = "".join(block.text for block in response.content if isinstance(block, TextBlock))
    # 推理模型的输出预算被 thinking 吃光时，finished_reason 仍是 completed、正文却是空串；
    # 不在这里拦住，就会以 pydantic ValidationError 的形式冒出去，掩盖真正的原因。
    if not text.strip():
        thinking = sum(len(getattr(block, "thinking", "") or "") for block in response.content
                       if type(block).__name__ == "ThinkingBlock")
        raise ValueError(f"模型只输出了思考、没有正文（thinking {thinking} 字符），请提高输出预算后重试")
    return text


def build_model_assessors(model: ChatModelBase) -> Sequence[ModelSubjectiveAssessor]:
    """组装 053 规定的三个主观评估器，共享同一个稳定模型角色。"""
    return [
        ModelSubjectiveAssessor(ReviewSource.EXPERIENCE, model),
        ModelSubjectiveAssessor(ReviewSource.RESILIENCE, model),
        ModelSubjectiveAssessor(ReviewSource.PREFERENCE, model),
    ]
