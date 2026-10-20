"""042–060 行程规划的数据契约：候选、方案、计划记录与审核结果。

这些模型只描述规划数据；差旅单、审批和供应商订单仍归阶段 D / F 的服务。
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TransportType(StrEnum):
    """交通候选类型。"""

    FLIGHT = "flight"
    TRAIN = "train"


class TransportCandidate(BaseModel):
    """一段去程或返程交通候选；时间保留供应商给出的 ISO 文本，由评分模块解析。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="候选编号，同一次搜索内唯一，如 F1、T3")
    type: TransportType = Field(..., description="交通类型：flight 或 train")
    carrier: str | None = Field(default=None, description="航空公司或列车车次归属")
    code: str | None = Field(default=None, description="航班号或车次")
    departure_time: str = Field(..., description="出发时间，ISO 文本；无时区偏移时按东八区解释")
    arrival_time: str = Field(..., description="到达时间，ISO 文本；带偏移时按真实时刻计算时长")
    origin: str = Field(..., description="出发城市")
    destination: str = Field(..., description="到达城市")
    price: float = Field(..., description="含税价格，元；必须为有限数值")
    cabin_class: str | None = Field(default=None, description="舱位或座位等级")
    is_direct: bool | None = Field(default=None, description="是否直达")
    refund_policy: str | None = Field(default=None, description="退改规则摘要")
    direction: Literal["outbound", "return"] | None = Field(
        default=None, description="显式去程/返程方向；为空时由城市与日期推断",
    )

    @field_validator("price")
    @classmethod
    def price_must_be_finite(cls, value: float) -> float:
        """价格必须是有限数；负数与零在规划阶段作为异常价格显式剔除。"""
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("价格必须是有限数值")
        return value


class HotelCandidate(BaseModel):
    """一家酒店候选及其整个行程的入住晚数。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="候选编号，如 H1")
    name: str = Field(..., min_length=1, description="酒店名称")
    brand: str | None = Field(default=None, description="酒店品牌")
    star_rating: int | None = Field(default=None, ge=0, le=5, description="星级，0–5")
    room_type: str | None = Field(default=None, description="房型")
    price_per_night: float = Field(..., description="每晚价格，元")
    nights: int = Field(default=1, ge=1, description="入住晚数，至少 1 晚")
    distance_to_dest_km: float | None = Field(default=None, ge=0, description="距目的地中心距离，公里")
    breakfast_included: bool | None = Field(default=None, description="是否含早餐")
    cancel_policy: str | None = Field(default=None, description="取消政策摘要")

    @field_validator("price_per_night")
    @classmethod
    def price_must_be_finite(cls, value: float) -> float:
        """每晚价格必须是有限数值。"""
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("价格必须是有限数值")
        return value


class CandidateSet(BaseModel):
    """一次搜索得到的交通与酒店候选，带来源与抓取时间。"""

    model_config = ConfigDict(extra="forbid")

    transports: list[TransportCandidate] = Field(default_factory=list, description="去程与返程交通候选")
    hotels: list[HotelCandidate] = Field(default_factory=list, description="酒店候选")
    source: str = Field(..., min_length=1, description="数据来源标识，如 simulated 或 tuniu_cli")
    captured_at: datetime = Field(..., description="候选抓取 UTC 时间")


class PlanPolicy(BaseModel):
    """规划使用的数值政策；任何字段为空表示该项不检查。"""

    model_config = ConfigDict(extra="forbid")

    hotel_limit: float | None = Field(default=None, ge=0, description="酒店每晚上限，元")
    hotel_star_limit: int | None = Field(default=None, ge=0, description="酒店星级上限")
    flight_class: str | None = Field(default=None, description="允许的最高机票舱位，可含 / 分隔多项")
    train_seat_class: str | None = Field(default=None, description="允许的最高高铁席别")
    approval_threshold: float | None = Field(default=None, ge=0, description="需额外审批的总价阈值，元")
    advance_booking_days: int | None = Field(default=None, ge=0, description="最少提前预订天数")
    version: str | None = Field(default=None, description="政策数据版本，用于追踪来源")


class DimensionWeights(BaseModel):
    """综合评分的四个维度权重，使用时先归一化；基线为 Java 当前 0.20/0.10/0.40/0.30。"""

    model_config = ConfigDict(extra="forbid")

    time: float = Field(default=0.20, ge=0, description="通勤时间维度权重")
    price: float = Field(default=0.10, ge=0, description="价格维度权重")
    preference: float = Field(default=0.40, ge=0, description="用户偏好维度权重")
    experience: float = Field(default=0.30, ge=0, description="出行体验维度权重")


class PreferenceScore(BaseModel):
    """模型事先对单个候选给出的偏好分和理由；工具不解读偏好文本。"""

    model_config = ConfigDict(extra="forbid")

    score: float = Field(..., ge=0, le=100, description="偏好分，0–100")
    basis: list[str] = Field(default_factory=list, description="打分理由，中文短句")


class PreferenceScores(BaseModel):
    """按候选编号索引的偏好分；未出现的编号按 0 分处理。"""

    model_config = ConfigDict(extra="forbid")

    transport_scores: dict[str, PreferenceScore] = Field(default_factory=dict, description="交通候选偏好分")
    hotel_scores: dict[str, PreferenceScore] = Field(default_factory=dict, description="酒店候选偏好分")


class ProposalMetrics(BaseModel):
    """代表方案的客观指标。"""

    model_config = ConfigDict(extra="forbid")

    total_price: float = Field(..., description="去程+返程+住宿总价，元")
    total_transit_min: int = Field(..., description="去程与返程总通勤分钟数")
    total_transit_hhmm: str = Field(..., description="总通勤时长文本，如 5h20m")
    stay_hours: float | None = Field(default=None, description="到达目的地到返程出发的停留小时数")


class ProposalScores(BaseModel):
    """代表方案的各维度 0–100 分与综合分。"""

    model_config = ConfigDict(extra="forbid")

    time: float = Field(..., description="时间分，通勤越短越高")
    price: float = Field(..., description="价格分，总价越低越高")
    preference: float = Field(..., description="偏好分，去程/住宿/返程偏好分的平均")
    policy: float = Field(..., description="政策软约束分，违规与告警扣分")
    experience: float = Field(..., description="出行体验归一化分")
    overall: float = Field(..., description="按维度权重合成的综合分")


class Proposal(BaseModel):
    """一个被选为代表的去程+住宿+返程组合。"""

    model_config = ConfigDict(extra="forbid")

    tags: list[str] = Field(..., description="命中的代表标签：综合最佳/时间最短/价格最低/最符合偏好")
    combo_id: str = Field(..., description="去程|酒店|返程 候选编号拼接的组合标识")
    outbound: TransportCandidate = Field(..., description="去程交通")
    hotel: HotelCandidate = Field(..., description="住宿")
    inbound: TransportCandidate = Field(..., description="返程交通")
    metrics: ProposalMetrics = Field(..., description="客观指标")
    scores: ProposalScores = Field(..., description="评分明细")
    preference_basis: list[str] = Field(default_factory=list, description="偏好打分理由，带去程/住宿/返程前缀")
    policy_violations: list[str] = Field(default_factory=list, description="政策硬性超标项，仅提示不剔除组合")
    warnings: list[str] = Field(default_factory=list, description="政策告警项")
    experience_flags: list[str] = Field(default_factory=list, description="出行体验风险标记")
    selection_basis: list[str] = Field(default_factory=list, description="入选依据短记录，如“综合最佳：综合分最高”")


class ExcludedCombo(BaseModel):
    """被过滤的组合或候选及原因，使入选与淘汰可对照。"""

    model_config = ConfigDict(extra="forbid")

    combo_id: str = Field(..., description="被淘汰的组合或候选标识")
    reason: str = Field(..., description="淘汰原因")


class PlanResult(BaseModel):
    """一次方案计算的完整结果，随计划版本保存。"""

    model_config = ConfigDict(extra="forbid")

    weights: DimensionWeights = Field(..., description="归一化后实际使用的维度权重")
    policy: PlanPolicy = Field(..., description="计算时使用的政策，审核据此复核预算与舱位")
    combo_count: int = Field(..., ge=0, description="通过过滤的组合数")
    excluded: list[ExcludedCombo] = Field(default_factory=list, description="被过滤的组合或候选及原因")
    proposals: list[Proposal] = Field(..., description="代表方案，综合分高者在前")
    preferences: str = Field(default="", description="用户偏好原文，仅供追溯")
    weather_summary: str = Field(default="", description="参与体验评分的天气摘要")
    candidate_source: str = Field(..., description="本次计算所用候选的数据来源")
    generated_at: datetime = Field(..., description="计算完成 UTC 时间")


class PlanStepName(StrEnum):
    """Plan-and-Execute 的固定步骤顺序。"""

    COLLECT_CONDITIONS = "collect_conditions"
    SEARCH_CANDIDATES = "search_candidates"
    COMPUTE_PROPOSALS = "compute_proposals"
    REVIEW = "review"
    AWAIT_CONFIRMATION = "await_confirmation"


class PlanStepStatus(StrEnum):
    """单个步骤状态。"""

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_INPUT = "needs_input"
    STALE = "stale"


class PlanStepState(BaseModel):
    """一个步骤的状态、重试次数和失败原因。"""

    model_config = ConfigDict(extra="forbid")

    name: PlanStepName = Field(..., description="步骤名称")
    status: PlanStepStatus = Field(default=PlanStepStatus.PENDING, description="步骤状态")
    attempts: int = Field(default=0, ge=0, description="已失败的次数")
    error: str | None = Field(default=None, description="最近一次失败或缺失输入说明")
    data_version: int | None = Field(default=None, description="步骤完成时所基于的计划版本")


class PlanProgress(BaseModel):
    """计划进度：步骤状态与已确认的外部写操作；只做状态记录，不执行业务。"""

    model_config = ConfigDict(extra="forbid")

    steps: list[PlanStepState] = Field(
        default_factory=lambda: [PlanStepState(name=name) for name in PlanStepName],
        description="按固定顺序排列的步骤状态",
    )
    committed_writes: dict[str, str] = Field(
        default_factory=dict, description="已确认的外部写操作：幂等键到外部引用，重规划不得重复执行",
    )
    terminated: bool = Field(default=False, description="达到终止条件后不再继续执行")
    termination_reason: str | None = Field(default=None, description="终止原因")


class PlanConditions(BaseModel):
    """收集到的规划条件；日期与城市变化会使搜索结果失效。"""

    model_config = ConfigDict(extra="forbid")

    origin: str = Field(..., min_length=1, description="出发城市")
    destination: str = Field(..., min_length=1, description="目的地城市")
    departure_date: date = Field(..., description="去程日期")
    return_date: date = Field(..., description="返程日期")
    preferences: str = Field(default="", max_length=500, description="用户偏好原文，可为空")
    budget: float | None = Field(default=None, gt=0, description="用户预算，元；为空表示未给出")

    @model_validator(mode="after")
    def validate_dates(self) -> "PlanConditions":
        """返程必须晚于去程，否则不创建计划。"""
        if self.return_date <= self.departure_date:
            raise ValueError("返程日期必须晚于去程日期")
        return self


class ReviewVerdict(StrEnum):
    """审核判定。"""

    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"


class ReviewSource(StrEnum):
    """审核处理器标识。"""

    OBJECTIVE = "objective"
    EXPERIENCE = "experience"
    RESILIENCE = "resilience"
    PREFERENCE = "preference"
    ARBITRATOR = "arbitrator"


class ReviewIssue(BaseModel):
    """一条审核问题：维度、严重程度、证据和可执行建议。"""

    model_config = ConfigDict(extra="forbid")

    dimension: str = Field(..., description="审核维度，如 完整性、预算、时间")
    severity: ReviewVerdict = Field(..., description="严重程度：warning 或 fail")
    field: str | None = Field(default=None, description="涉及的方案字段路径")
    evidence: str = Field(..., description="问题的事实证据")
    suggestion: str = Field(..., description="具体可执行的整改建议")


class ReviewCheck(BaseModel):
    """客观六维中某一维的检查结果。"""

    model_config = ConfigDict(extra="forbid")

    dimension: str = Field(..., description="检查维度名称")
    status: ReviewVerdict = Field(..., description="该维度判定")
    detail: str = Field(..., description="判定说明")


class ReviewResult(BaseModel):
    """单个处理器（客观校验、主观评估或仲裁）的结构化输出。"""

    model_config = ConfigDict(extra="forbid")

    source: ReviewSource = Field(..., description="处理器标识")
    verdict: ReviewVerdict = Field(..., description="综合判定")
    available: bool = Field(default=True, description="处理器是否成功执行；失败时为 false 且原因写入 issues")
    checks: list[ReviewCheck] = Field(default_factory=list, description="逐维检查，仅客观处理器填写")
    issues: list[ReviewIssue] = Field(default_factory=list, description="发现的问题")
    details: dict[str, Any] = Field(default_factory=dict, description="处理器特有的补充信息")


class FinalReview(BaseModel):
    """最终审核结论：绑定计划版本，硬规则失败不可被主观意见覆盖。"""

    model_config = ConfigDict(extra="forbid")

    review_id: str = Field(..., description="审核记录标识")
    plan_id: str = Field(..., description="被审核计划标识")
    plan_version: int = Field(..., ge=1, description="审核所针对的计划内容版本")
    proposal_tag: str = Field(..., description="被审核的代表方案标签")
    verdict: ReviewVerdict = Field(..., description="最终判定")
    needs_rework: bool = Field(..., description="是否必须整改后才能进入确认")
    objective: ReviewResult = Field(..., description="客观规则引擎结果")
    subjective: list[ReviewResult] = Field(default_factory=list, description="有限次数的主观评估结果")
    arbitration: ReviewResult = Field(..., description="仲裁结果")
    reviewed_at: datetime = Field(..., description="审核完成 UTC 时间")


class PlanView(BaseModel):
    """一份行程规划的完整持久化状态；不含用户标识，归属由存储键保证。"""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(..., min_length=1, description="计划稳定标识")
    version: int = Field(..., ge=1, description="保存版本；每次成功保存递增，用于乐观并发控制")
    content_version: int = Field(..., ge=1, description="内容版本；条件、候选或方案变化时递增，审核绑定此版本")
    order_id: str | None = Field(default=None, description="关联的本人差旅单标识，只保存引用")
    conditions: PlanConditions = Field(..., description="当前规划条件")
    progress: PlanProgress = Field(..., description="步骤进度")
    candidates: CandidateSet | None = Field(default=None, description="当前候选；未搜索或已失效时为空")
    result: PlanResult | None = Field(default=None, description="当前方案结果；未计算或已失效时为空")
    last_review: FinalReview | None = Field(default=None, description="最近一次审核，绑定其计划版本")
    created_at: datetime = Field(..., description="创建 UTC 时间")
    updated_at: datetime = Field(..., description="最近更新 UTC 时间")

    def review_is_current(self) -> bool:
        """审核必须绑定当前内容版本；条件、候选或方案变化都会使其失效，仅保存进度不会。"""
        return self.last_review is not None and self.last_review.plan_version == self.content_version
