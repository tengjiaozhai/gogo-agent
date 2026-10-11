"""阶段 D 的业务契约；字段以服务端身份和确定性规则为准。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TravelOrderStatus(StrEnum):
    """差旅单状态，沿用 Java 的六种业务值。"""

    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class ApprovalStatus(StrEnum):
    """本地审批记录状态。"""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class BookingStatus(StrEnum):
    """本地预订记录主要状态。"""

    CREATED = "CREATED"
    PENDING_PAYMENT = "PENDING_PAYMENT"
    PAID = "PAID"
    CONFIRMED = "CONFIRMED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"
    FAILED = "FAILED"


class BookingType(StrEnum):
    """当前管理契约中的三种预订业务类型。"""

    FLIGHT = "FLIGHT"
    HOTEL = "HOTEL"
    TRAIN = "TRAIN"


class TravelOrderDraft(BaseModel):
    """用户确认前的完整差旅申请字段。"""

    model_config = ConfigDict(extra="forbid")

    destination: str = Field(..., min_length=1, max_length=256, description="出差目的地城市")
    departure_city: str = Field(..., min_length=1, max_length=128, description="出发城市")
    departure_date: date = Field(..., description="出发日期，YYYY-MM-DD")
    return_date: date = Field(..., description="返回日期，YYYY-MM-DD")
    purpose: str = Field(..., min_length=1, max_length=512, description="出差事由")

    @model_validator(mode="after")
    def validate_date_range(self) -> Self:
        """返回日不得早于出发日；同日往返有效。"""
        if self.return_date < self.departure_date:
            raise ValueError("返回日期不能早于出发日期")
        return self


class TravelOrderPatch(BaseModel):
    """已授权用户对差旅单的非空局部修改。"""

    model_config = ConfigDict(extra="forbid")

    destination: str | None = Field(default=None, min_length=1, max_length=256, description="新的出差目的地城市")
    departure_city: str | None = Field(default=None, min_length=1, max_length=128, description="新的出发城市")
    departure_date: date | None = Field(default=None, description="新的出发日期")
    return_date: date | None = Field(default=None, description="新的返回日期")
    purpose: str | None = Field(default=None, min_length=1, max_length=512, description="新的出差事由")

    @model_validator(mode="after")
    def require_change(self) -> Self:
        """空补丁不能生成新审批记录。"""
        if not self.model_fields_set or all(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("至少提供一个待修改字段")
        return self


class ConfirmedWrite(BaseModel):
    """用户从鉴权 HTTP 入口显式确认的业务写入请求。"""

    model_config = ConfigDict(extra="forbid")

    confirmed: bool = Field(..., description="用户在当前请求中显式确认本次业务写入")
    idempotency_key: str = Field(..., min_length=1, max_length=128, description="用户请求的幂等键；同键不同请求体拒绝")


class SubmitTravelRequest(TravelOrderDraft, ConfirmedWrite):
    """创建差旅单并同步提交本地审批的用户请求。"""


class ModifyTravelRequest(ConfirmedWrite):
    """局部修改差旅单并撤销旧审批后重新提交的用户请求。"""

    changes: TravelOrderPatch = Field(..., description="本次修改的差旅字段")
    force: bool = Field(default=False, description="已审批单修改时用户再次确认强制撤审")


class CancelTravelRequest(ConfirmedWrite):
    """取消本人差旅单及当前审批的用户请求。"""

    force: bool = Field(default=False, description="已审批单取消时用户再次确认强制撤审")


class TravelOrderView(TravelOrderDraft):
    """对用户展示的差旅单及当前审批关联。"""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    order_id: str = Field(..., description="差旅单唯一标识")
    user_id: str = Field(..., description="申请人的认证用户标识")
    status: TravelOrderStatus = Field(..., description="当前差旅单状态")
    approval_id: str | None = Field(default=None, description="当前关联的审批流程实例标识")
    plan_html_url: str | None = Field(default=None, description="行程方案 HTML 对象存储 key")
    created_at: datetime = Field(..., description="差旅单创建 UTC 时间")
    updated_at: datetime = Field(..., description="差旅单最近修改 UTC 时间")


class ApprovalView(BaseModel):
    """一条审批历史记录及其提交时的表单快照。"""

    model_config = ConfigDict(extra="forbid")

    process_instance_id: str = Field(..., description="审批流程实例唯一标识")
    user_id: str = Field(..., description="审批申请人的认证用户标识")
    order_id: str = Field(..., description="关联差旅单标识")
    title: str = Field(..., description="审批展示标题")
    status: ApprovalStatus = Field(..., description="当前审批状态")
    approval_form: dict[str, Any] = Field(..., description="提交时固定的差旅单字段快照")
    remark: str | None = Field(default=None, description="管理员审批备注")
    submit_time: datetime = Field(..., description="审批提交 UTC 时间")
    update_time: datetime = Field(..., description="审批最近更新 UTC 时间")


class AdminDecisionRequest(BaseModel):
    """管理员对待处理审批的明确决定。"""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["agree", "refuse"] = Field(..., description="同意或拒绝审批")
    remark: str | None = Field(default=None, max_length=512, description="管理员审批备注")


class UserProfileView(BaseModel):
    """本人可读的联系资料；日志和模型工具结果需控制披露范围。"""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    user_id: str = Field(..., description="档案所有者的认证用户标识")
    base_city: str | None = Field(default=None, description="常驻城市")
    level: str | None = Field(default=None, description="职级，如 P5/P6/P7/P8")
    name_pinyin: str | None = Field(default=None, description="酒店预订使用的姓名拼音")
    email: str | None = Field(default=None, description="酒店预订使用的邮箱")
    chinese_name: str | None = Field(default=None, description="机票预订使用的中文姓名")
    id_type: int | None = Field(default=None, ge=0, le=9, description="证件类型编号")
    id_number: str | None = Field(default=None, description="旅客证件号")
    phone: str | None = Field(default=None, description="联系手机号")
    gender: Literal["M", "F"] | None = Field(default=None, description="性别：M 男、F 女")


class UserProfilePatch(BaseModel):
    """只允许修改本人的已列出联系字段。"""

    model_config = ConfigDict(extra="forbid")

    base_city: str | None = Field(default=None, max_length=128, description="新的常驻城市")
    name_pinyin: str | None = Field(default=None, max_length=128, description="新的姓名拼音")
    email: str | None = Field(default=None, max_length=128, description="新的联系邮箱")
    chinese_name: str | None = Field(default=None, max_length=64, description="新的中文姓名")
    id_type: int | None = Field(default=None, ge=0, le=9, description="新的证件类型")
    id_number: str | None = Field(default=None, max_length=64, description="新的旅客证件号")
    phone: str | None = Field(default=None, pattern=r"^1[3-9]\d{9}$", description="新的中国大陆手机号")
    gender: Literal["M", "F"] | None = Field(default=None, description="新的性别")

    @model_validator(mode="after")
    def require_change(self) -> Self:
        """拒绝无字段的更新。"""
        if not self.model_fields_set:
            raise ValueError("至少提供一个资料字段")
        return self


class PolicyRule(BaseModel):
    """按职级、城市档位和日期版本匹配的精确差旅规则。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    level_min: int = Field(..., ge=1, description="适用职级数字下限")
    level_max: int = Field(..., ge=1, description="适用职级数字上限")
    city_tier: Literal["一线", "新一线", "其他"] = Field(..., description="政策城市档位")
    flight_class: str = Field(..., description="允许的最高机票舱位")
    train_seat_class: str = Field(..., description="允许的最高高铁座位")
    hotel_limit: Decimal = Field(..., ge=0, description="酒店每晚金额上限，元")
    hotel_star_limit: int = Field(..., ge=0, description="酒店星级上限")
    daily_meal_limit: Decimal = Field(..., ge=0, description="每日餐补上限，元")
    daily_transport_limit: Decimal = Field(..., ge=0, description="每日交通补贴上限，元")
    approval_threshold: Decimal = Field(..., ge=0, description="需审批的金额阈值，元")
    advance_booking_days: int = Field(..., ge=0, description="最少提前预订天数")
    effective_from: date = Field(..., description="规则开始生效日期")
    effective_until: date | None = Field(default=None, description="规则失效日期；为空表示长期有效")
    version: str = Field(..., min_length=1, description="政策数据版本")

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        """职级和日期上下界保持有序。"""
        if self.level_max < self.level_min:
            raise ValueError("职级上限不能低于下限")
        if self.effective_until is not None and self.effective_until < self.effective_from:
            raise ValueError("规则失效日期不能早于生效日期")
        return self


class PolicyView(BaseModel):
    """某用户某城市某日适用的精确政策及来源版本。"""

    model_config = ConfigDict(extra="forbid")

    destination_city: str = Field(..., description="政策适用的目的地城市")
    user_level: str = Field(..., description="查询用户的当前职级")
    city_tier: str = Field(..., description="实际城市等级")
    rule: PolicyRule = Field(..., description="匹配的结构化规则及版本")


class PolicyCheckInput(BaseModel):
    """一项待检验的机票、酒店或高铁订单摘要。"""

    model_config = ConfigDict(extra="forbid")

    type: BookingType = Field(..., description="待检验订单类型")
    amount: Decimal | None = Field(default=None, description="订单金额，元；酒店检查必填")
    flight_class: str | None = Field(default=None, description="机票舱位；机票检查必填")
    seat_class: str | None = Field(default=None, description="高铁座位等级；火车检查必填")
    hotel_star: int | None = Field(default=None, ge=0, description="酒店星级，可选")


class PolicyCheckResult(BaseModel):
    """确定性政策检查结果；缺失必要字段绝不报合规。"""

    model_config = ConfigDict(extra="forbid")

    compliant: bool = Field(..., description="是否通过全部可适用政策检查")
    violations: list[str] = Field(..., description="违反政策或缺少必要订单字段的原因")
    suggestions: list[str] = Field(..., description="给用户的具体修正建议")


class BookingView(BaseModel):
    """只读预订记录摘要，归属由服务层按当前用户过滤。"""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    booking_id: str = Field(..., description="内部预订业务标识")
    user_id: str = Field(..., description="预订所属用户标识")
    travel_order_id: str | None = Field(default=None, description="关联差旅单标识")
    biz_type: BookingType = Field(..., description="预订业务类型")
    platform: str = Field(..., description="供应商平台")
    external_order_no: str | None = Field(default=None, description="供应商订单标识")
    status: BookingStatus = Field(..., description="内部预订状态")
    external_status: str | None = Field(default=None, description="供应商原始状态；为空表示未经外部确认")
    title: str | None = Field(default=None, description="预订展示标题")
    total_amount: Decimal | None = Field(default=None, description="预订总金额，元")
    currency: str = Field(default="CNY", description="金额币种")
    remark: str | None = Field(default=None, description="内部备注或取消原因")


class BookingCancelRequest(ConfirmedWrite):
    """本人主动确认的模拟预订取消请求。"""

    reason: str | None = Field(default=None, max_length=512, description="用户提供的取消原因")


class CancelResult(BaseModel):
    """差旅单取消及审批撤销的确定性结果。"""

    model_config = ConfigDict(extra="forbid")

    cancelled: bool = Field(..., description="差旅单是否已处于取消状态")
    order_id: str = Field(..., description="本次取消的差旅单标识")
    approval_cancelled: bool = Field(..., description="关联的待处理审批是否同步撤销")


class BookingCancelResult(BaseModel):
    """模拟预订取消或本地酒店状态变更的结果。"""

    model_config = ConfigDict(extra="forbid")

    cancelled: bool = Field(..., description="本地预订记录是否已处于取消状态")
    booking_id: str = Field(..., description="本次处理的预订业务标识")
    supplier_cancelled: bool = Field(..., description="外部供应商是否确认取消；酒店本地标记时为假")
    message: str = Field(..., description="给用户的取消结果及供应商边界说明")


class ProfileCompleteness(BaseModel):
    """按预订类型列出本人联系资料缺项，不返回证件号码原值。"""

    model_config = ConfigDict(extra="forbid")

    biz_type: BookingType = Field(..., description="要办理的机票、酒店或火车预订类型")
    complete: bool = Field(..., description="该类型所需字段是否齐全")
    missing_fields: list[str] = Field(..., description="需本人补齐的资料字段名称")


class ToolResponse(BaseModel):
    """管理 Agent 只读查询或待确认写入建议的稳定工具结果。"""

    model_config = ConfigDict(extra="forbid")

    success: bool = Field(..., description="工具是否返回可用查询数据；待确认写入为假")
    errorCode: str | None = Field(default=None, description="稳定业务错误码；成功时为空")
    message: str = Field(..., description="不包含完整个人资料的结果说明")
    data: dict[str, Any] | None = Field(default=None, description="可供 Agent 使用的脱敏结构化数据")


class ConflictItem(BaseModel):
    """与现有差旅单的一个确定性时间或衔接冲突。"""

    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(..., description="发生冲突的本人既有差旅单标识")
    severity: Literal["LOW", "MEDIUM", "HIGH"] = Field(..., description="冲突严重程度")
    reason: str = Field(..., description="导致冲突的日期、城市或交通时间规则")


class ConflictReport(BaseModel):
    """新行程与本人差旅单的冲突检测结果。"""

    model_config = ConfigDict(extra="forbid")

    conflicts: list[ConflictItem] = Field(..., description="按既有差旅单逐项列出的冲突")
    uncertain: list[str] = Field(..., description="缺少交通时长等数据时不能确认的衔接")


class MyTravelBooking(BaseModel):
    """与 Java 前端列表兼容的本人预订摘要。"""

    model_config = ConfigDict(extra="forbid")

    bookingId: str = Field(..., description="内部预订业务标识")
    bizType: str = Field(..., description="机票、酒店或火车类型")
    bizTypeLabel: str = Field(..., description="预订类型中文名称")
    title: str | None = Field(default=None, description="预订展示标题")
    status: str = Field(..., description="本地预订状态码")
    statusLabel: str = Field(..., description="本地预订状态中文名称")
    externalStatus: str | None = Field(default=None, description="供应商原始状态")
    totalAmount: str | None = Field(default=None, description="十进制金额字符串")
    currency: str = Field(..., description="金额币种")
    platform: str = Field(..., description="供应商平台")
    externalOrderNo: str | None = Field(default=None, description="供应商订单标识")
    paymentStatus: str | None = Field(default=None, description="付款状态")
    startTime: int | None = Field(default=None, description="服务开始毫秒时间戳")
    endTime: int | None = Field(default=None, description="服务结束毫秒时间戳")
    bookedAt: int | None = Field(default=None, description="下单毫秒时间戳")
    orderUrl: str | None = Field(default=None, description="供应商详情 URL；未验证时为空")


class MyTravelOrder(BaseModel):
    """与 Java 前端列表兼容的差旅单、当前审批和预订聚合视图。"""

    model_config = ConfigDict(extra="forbid")

    orderId: str = Field(..., description="差旅单唯一标识")
    destination: str = Field(..., description="出差目的地")
    departureCity: str = Field(..., description="出发城市")
    departureDate: str = Field(..., description="出发日期，YYYY-MM-DD")
    returnDate: str = Field(..., description="返回日期，YYYY-MM-DD")
    purpose: str = Field(..., description="出差事由")
    status: str = Field(..., description="差旅单状态码")
    statusLabel: str = Field(..., description="差旅单状态中文名称")
    createdAt: int = Field(..., description="创建毫秒时间戳")
    updatedAt: int = Field(..., description="最近更新毫秒时间戳")
    international: bool = Field(..., description="目的地是否命中已知境外地名或拉丁字母")
    approvalId: str | None = Field(default=None, description="当前审批流程实例标识")
    approvalStatus: str | None = Field(default=None, description="当前审批状态码")
    approvalStatusLabel: str | None = Field(default=None, description="当前审批状态中文名称")
    approvalRemark: str | None = Field(default=None, description="当前审批备注")
    approvalSubmitTime: int | None = Field(default=None, description="审批提交毫秒时间戳")
    approvalUpdateTime: int | None = Field(default=None, description="审批最近更新毫秒时间戳")
    bookingCount: int = Field(..., description="有效预订摘要数量")
    bookings: list[MyTravelBooking] = Field(..., description="本人该差旅单下的预订摘要")
    planHtmlUrl: str | None = Field(default=None, description="方案 HTML 对象 key；不是公开可访问链接")
