"""008 问题改写与意图识别的数据契约，不执行模型调用或 Agent 调度。"""

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


_NonEmptyText = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)
]


class IntentCategory(StrEnum):
    """与 Java 基线一致的十六类意图；类别存在不表示对应业务已经可执行。"""

    TRAVEL_APPLICATION = "travel_application"  # 差旅申请
    TRAVEL_CANCEL = "travel_cancel"  # 取消差旅申请
    TRAVEL_MODIFY = "travel_modify"  # 修改差旅申请
    APPROVAL_QUERY = "approval_query"  # 查询审批状态
    TRAVEL_ORDER_QUERY = "travel_order_query"  # 查询差旅单
    ITINERARY_PLANNING = "itinerary_planning"  # 行程规划
    FLIGHT_SEARCH = "flight_search"  # 查询机票
    TRAIN_SEARCH = "train_search"  # 查询火车票
    HOTEL_SEARCH = "hotel_search"  # 查询酒店
    BOOKING = "booking"  # 预订、改签或取消供应商订单
    REIMBURSEMENT = "reimbursement"  # 报销意图，对应业务尚未实现
    POLICY_QUERY = "policy_query"  # 查询差旅政策
    ATTRACTIONS_QUERY = "attractions_query"  # 查询目的地景点
    GENERAL_INFO = "general_info"  # 查询通用公共信息
    GREETING = "greeting"  # 问候
    UNKNOWN = "unknown"  # 有输入但无法归入已知意图


class ConfidenceLevel(StrEnum):
    """意图识别的置信等级，沿用 Java 的小写传输值，不表示统计概率。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class QueryInput(BaseModel):
    """进入改写与识别步骤的非空用户问题，可信身份由调用方单独传递。"""

    model_config = ConfigDict(extra="forbid")

    question: _NonEmptyText = Field(..., description="用户本轮问题，去除首尾空白后不能为空")


class HistoryMessage(BaseModel):
    """供改写读取的业务历史片段，角色仅作数据标签，不提升为模型指令。"""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "agent", "system"] = Field(..., description="业务消息的原始角色")
    content: _NonEmptyText = Field(..., description="该条历史的可见文本内容")
    truncated: bool = Field(default=False, strict=True, description="该条文本是否因长度限制仅保留尾部")


class RewriteContext(BaseModel):
    """最近十条跨 Agent 历史与本轮问题组成的改写输入快照。"""

    model_config = ConfigDict(extra="forbid")

    query: QueryInput = Field(..., description="本轮已保存的问题，在上下文中单独出现一次")
    history: list[HistoryMessage] = Field(..., max_length=10, description="本轮之前最多十条消息，按时间正序排列")
    reference_date: date = Field(..., description="服务端提供的业务当地日期，用于解释明天、后天等相对日期")
    timezone: Literal["Asia/Shanghai"] = Field(default="Asia/Shanghai", description="参考日期所属业务时区")
    history_truncated: bool = Field(default=False, strict=True, description="十条窗口内是否有历史因字符预算被删除或截断")


class RewriteResult(BaseModel):
    """问题改写结果；缺少必要上下文时明确保留未完成状态。"""

    model_config = ConfigDict(extra="forbid")

    related: bool = Field(..., strict=True, description="本轮问题是否关联已提供的历史对话")
    rewritten_question: _NonEmptyText | None = Field(
        ..., description="补全后的独立问题；无需改写时为原问题，缺少必要上下文时为 null"
    )
    reason: _NonEmptyText = Field(..., description="改写、保持原句或无法完成改写的原因")
    evidence: list[_NonEmptyText] = Field(
        ..., min_length=1, description="改写依据的本轮/历史原文或服务端提供的日期等上下文"
    )
    missing_context: list[_NonEmptyText] = Field(
        ..., description="缺少的必要上下文；改写完成或无需改写时必须为空列表"
    )

    @model_validator(mode="after")
    def validate_context_completeness(self) -> Self:
        """保持改写问题与缺失上下文的一致性，避免把未完成结果伪装成可用问题。"""
        if (self.rewritten_question is None) != bool(self.missing_context):
            raise ValueError("缺少上下文时 rewritten_question 必须为 null，否则 missing_context 必须为空")
        return self


class IntentItem(BaseModel):
    """一个已识别意图及其置信等级、判断理由和文本依据。"""

    model_config = ConfigDict(extra="forbid")

    intent: IntentCategory = Field(..., description="意图类别；无法判断时显式使用 unknown")
    confidence: ConfidenceLevel = Field(..., description="识别置信等级：high、medium 或 low")
    reason: _NonEmptyText = Field(..., description="将当前诉求归入该意图的判断理由")
    evidence: list[_NonEmptyText] = Field(
        ..., min_length=1, description="支持该意图判断的问题或上下文片段，不作为身份或执行授权"
    )


class IntentResult(BaseModel):
    """一次识别的有序意图结果；结构有效不代表已经路由或执行。"""

    model_config = ConfigDict(extra="forbid")

    intents: list[IntentItem] = Field(
        ..., min_length=1, description="按执行依赖或优先顺序排列的意图事项，至少一项"
    )
    primary_intent: IntentCategory = Field(
        ..., description="本轮核心意图，必须出现在 intents 中，但不要求位于第一项"
    )
    multi_intent: bool = Field(
        ..., strict=True, description="是否包含多个意图事项，必须与 intents 长度大于一一致"
    )
    overall_reason: _NonEmptyText = Field(..., description="整轮意图判断及事项排序的理由")

    @model_validator(mode="after")
    def validate_intent_consistency(self) -> Self:
        """校验主要意图的归属和多意图标记，不擅自重排或去重识别结果。"""
        if self.primary_intent not in {item.intent for item in self.intents}:
            raise ValueError("primary_intent 必须出现在 intents 中")
        if self.multi_intent != (len(self.intents) > 1):
            raise ValueError("multi_intent 必须与 intents 的数量一致")
        return self
