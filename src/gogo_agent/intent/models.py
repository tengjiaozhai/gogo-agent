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


class RecognitionLayer(StrEnum):
    """识别结果的实际来源层级。"""

    RULE = "rule"  # L1 规则
    VECTOR = "vector"  # L2 向量
    LLM = "llm"  # L3 模型


class MatchStatus(StrEnum):
    """快速匹配器的三种结论；歧义表示后续快速层应弃权。"""

    HIT = "hit"
    MISS = "miss"
    AMBIGUOUS = "ambiguous"


class IntentCandidate(BaseModel):
    """快速层返回的一个候选类别及其可观察的匹配证据。"""

    model_config = ConfigDict(extra="forbid")

    intent: IntentCategory = Field(..., description="候选意图类别")
    layer: Literal[RecognitionLayer.RULE, RecognitionLayer.VECTOR] = Field(
        ..., description="产生候选的实际快速层：rule 或 vector"
    )
    score: float | None = Field(
        ..., ge=0, le=1, allow_inf_nan=False,
        description="L2 归一化相似度；规则候选为 null，不把数值当作模型授权",
    )
    reason: _NonEmptyText = Field(..., description="候选命中的规则证据或近邻样例说明")


class FastMatch(BaseModel):
    """L1/L2 匹配器的结构化输出，可独立表示命中、未命中与复合歧义。"""

    model_config = ConfigDict(extra="forbid")

    status: MatchStatus = Field(..., description="快速层结论：hit、miss 或 ambiguous")
    result: IntentResult | None = Field(
        ..., description="单意图命中时的 008 结果；未命中或歧义时必须为 null"
    )
    candidates: list[IntentCandidate] = Field(
        default_factory=list, description="本层观察到的候选，保持匹配器给出的顺序"
    )
    threshold: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False,
        description="向量层本次判定使用的阈值；规则层为 null",
    )
    reason: _NonEmptyText = Field(..., description="本层命中、弃权或未命中的具体理由")

    @model_validator(mode="after")
    def validate_hit_state(self) -> Self:
        """快速层只交付单意图命中；歧义交给完整识别步骤。"""
        if (self.status is MatchStatus.HIT) != (self.result is not None):
            raise ValueError("只有 hit 状态可以携带 result")
        if self.result is not None and self.result.multi_intent:
            raise ValueError("快速层不得把多意图当作单意图命中")
        return self


class RecognitionDecision(BaseModel):
    """统一识别出口；快速模式未命中时 result 和 hit_layer 为 null。"""

    model_config = ConfigDict(extra="forbid")

    result: IntentResult | None = Field(..., description="最终意图；仅快速模式未命中时为 null")
    hit_layer: RecognitionLayer | None = Field(..., description="实际命中层级；快速模式未命中时为 null")
    confidence: ConfidenceLevel | None = Field(
        ..., description="主要意图的置信等级；无命中时为 null"
    )
    vector_threshold: float | None = Field(
        ..., ge=0, le=1, allow_inf_nan=False,
        description="本次若尝试向量层，其使用的阈值；未尝试时为 null",
    )
    candidates: list[IntentCandidate] = Field(
        ..., description="已尝试的快速层候选，按实际尝试顺序保留"
    )
    attempted_layers: list[RecognitionLayer] = Field(
        ..., description="实际调用过的层级，按规则、向量、模型的顺序记录"
    )
    reason: _NonEmptyText = Field(..., description="各层短路、弃权或回退的可观察原因")

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        """最终结论必须来自实际尝试过的层，且置信度与主要意图一致。"""
        if self.result is None:
            if self.hit_layer is not None or self.confidence is not None:
                raise ValueError("未命中时不得填写命中层级或置信等级")
        else:
            if self.hit_layer is None or self.hit_layer not in self.attempted_layers:
                raise ValueError("命中层级必须来自实际尝试的层")
            primary = next(item for item in self.result.intents if item.intent is self.result.primary_intent)
            if self.confidence is not primary.confidence:
                raise ValueError("决策置信等级必须与主要意图一致")
        return self
