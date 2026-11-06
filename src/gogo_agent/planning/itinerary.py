"""用户手工给出的去程、住宿与返程安排：校验、试算后生成已关联差旅单的计划。

安排不经途牛搜索，计划候选的来源标记为 user_provided；审核与预订门禁不因此放宽。
"""

from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from gogo_agent.travel.models import TravelOrderDraft
from gogo_agent.travel.service import BusinessError

from .models import CandidateSet, HotelCandidate, PlanConditions, TransportCandidate, TransportType
from .scoring import PlanningError, compute_plan
from .service import USER_PROVIDED_SOURCE, PlanError, PlanService

PREFERENCE_NOTE = "用户手工提供的行程安排"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UserTransport(BaseModel):
    """用户给出的单程交通安排。"""

    model_config = ConfigDict(extra="forbid")

    type: TransportType = Field(default=TransportType.FLIGHT, description="交通类型：flight 航班，train 火车或高铁")
    carrier: str | None = Field(default=None, max_length=64, description="航空公司或车次归属，如 东航")
    code: str = Field(..., min_length=1, max_length=32, description="航班号或车次，如 MU6309")
    departure_time: datetime = Field(..., description="出发时间，本地时间，格式 YYYY-MM-DDTHH:MM，不带时区")
    arrival_time: datetime = Field(..., description="到达时间，本地时间，格式 YYYY-MM-DDTHH:MM，不带时区")
    price: float = Field(..., gt=0, description="含税价格，元")
    cabin_class: str | None = Field(default=None, max_length=64, description="舱位或席别，如 经济舱")

    @model_validator(mode="after")
    def check_times(self) -> Self:
        """时间必须是不带时区的本地时间，且到达晚于出发。"""
        if self.departure_time.tzinfo is not None or self.arrival_time.tzinfo is not None:
            raise ValueError("时间请填写本地时间，不带时区偏移")
        if self.arrival_time <= self.departure_time:
            raise ValueError("到达时间必须晚于出发时间")
        return self


class UserHotel(BaseModel):
    """用户给出的住宿安排。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128, description="酒店名称")
    check_in: date = Field(..., description="入住日期，YYYY-MM-DD")
    check_out: date = Field(..., description="离店日期，YYYY-MM-DD")
    price_per_night: float = Field(..., gt=0, description="每晚价格，元")
    breakfast_included: bool | None = Field(default=None, description="是否含早餐")
    room_type: str | None = Field(default=None, max_length=64, description="房型")

    @model_validator(mode="after")
    def check_nights(self) -> Self:
        """离店必须晚于入住。"""
        if self.check_out <= self.check_in:
            raise ValueError("离店日期必须晚于入住日期")
        return self


class UserItinerary(BaseModel):
    """用户手工给出的完整行程安排：去程、住宿、返程。"""

    model_config = ConfigDict(extra="forbid")

    outbound: UserTransport = Field(..., description="去程交通")
    hotel: UserHotel = Field(..., description="住宿")
    inbound: UserTransport = Field(..., description="返程交通")


def _load(itinerary_json: str) -> UserItinerary:
    """解析并校验用户安排；错误信息指向用户填写的字段。"""
    try:
        return UserItinerary.model_validate_json(itinerary_json)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "行程安排"
        raise BusinessError("ITINERARY_INVALID", f"行程安排格式不正确（{where}）：{first['msg']}", 422) from None


def _conditions(draft: TravelOrderDraft) -> PlanConditions:
    return PlanConditions(
        origin=draft.departure_city, destination=draft.destination,
        departure_date=draft.departure_date, return_date=draft.return_date,
        preferences=PREFERENCE_NOTE,
    )


def build_candidates(itinerary: UserItinerary, draft: TravelOrderDraft, *, captured_at: datetime) -> CandidateSet:
    """把用户安排映射为候选集；日期必须与差旅单一致，不一致时指明用户填写的字段。"""
    out, back, hotel = itinerary.outbound, itinerary.inbound, itinerary.hotel
    if out.departure_time.date() != draft.departure_date:
        raise BusinessError(
            "ITINERARY_DATE_MISMATCH",
            f"去程 {out.code} 的出发日期应为 {draft.departure_date.isoformat()}，当前填写为 "
            f"{out.departure_time.date().isoformat()}，请核对后重新提交",
            422,
        )
    if back.departure_time.date() != draft.return_date:
        raise BusinessError(
            "ITINERARY_DATE_MISMATCH",
            f"返程 {back.code} 的出发日期应为 {draft.return_date.isoformat()}，当前填写为 "
            f"{back.departure_time.date().isoformat()}，请核对后重新提交",
            422,
        )
    if hotel.check_in != draft.departure_date or hotel.check_out != draft.return_date:
        raise BusinessError(
            "ITINERARY_DATE_MISMATCH",
            f"酒店入住与离店日期应为 {draft.departure_date.isoformat()} 至 {draft.return_date.isoformat()}，"
            f"当前填写为 {hotel.check_in.isoformat()} 至 {hotel.check_out.isoformat()}",
            422,
        )
    transports = [
        TransportCandidate(
            id="U1", type=out.type, carrier=out.carrier, code=out.code,
            departure_time=out.departure_time.isoformat(), arrival_time=out.arrival_time.isoformat(),
            origin=draft.departure_city, destination=draft.destination, price=out.price,
            cabin_class=out.cabin_class, direction="outbound",
        ),
        TransportCandidate(
            id="U2", type=back.type, carrier=back.carrier, code=back.code,
            departure_time=back.departure_time.isoformat(), arrival_time=back.arrival_time.isoformat(),
            origin=draft.destination, destination=draft.departure_city, price=back.price,
            cabin_class=back.cabin_class, direction="return",
        ),
    ]
    hotel_candidate = HotelCandidate(
        id="H1", name=hotel.name, room_type=hotel.room_type, price_per_night=hotel.price_per_night,
        nights=(hotel.check_out - hotel.check_in).days, breakfast_included=hotel.breakfast_included,
    )
    return CandidateSet(transports=transports, hotels=[hotel_candidate], source=USER_PROVIDED_SOURCE, captured_at=captured_at)


def _signature(candidates: CandidateSet | None) -> tuple:
    """只比较决定方案的字段：交通班次与时刻、价格，以及酒店名称、单价与晚数。"""
    if candidates is None:
        return ()
    return (
        tuple((t.code, t.departure_time, t.arrival_time, t.price) for t in candidates.transports),
        tuple((h.name, h.price_per_night, h.nights) for h in candidates.hotels),
    )


class ItineraryBinder:
    """差旅提交时的行程绑定：提交前完成校验与试算，提交成功后写入计划并关联差旅单。"""

    def __init__(self, plans: PlanService, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._plans = plans
        self._clock = clock

    def parse(self, itinerary_json: str, draft: TravelOrderDraft) -> dict[str, Any]:
        """无副作用：校验安排并试算方案，任何无法形成方案的输入都在写库前拒绝。"""
        itinerary = _load(itinerary_json)
        now = self._clock()
        candidates = build_candidates(itinerary, draft, captured_at=now)
        try:
            compute_plan(
                candidates, origin=draft.departure_city, destination=draft.destination,
                return_date=draft.return_date.isoformat(), now=now,
            )
        except PlanningError as exc:
            raise BusinessError("ITINERARY_NOT_PLANNABLE", f"行程安排无法形成方案：{exc}", 422) from None
        return itinerary.model_dump(mode="json")

    def bind(
        self, user_id: str, draft: TravelOrderDraft, normalized: dict[str, Any], *,
        order_id: str, session_id: str,
    ) -> dict[str, Any]:
        """生成计划、保存用户候选并计算方案；同一差旅单已有计划时直接返回，重复提交不产生第二份计划。"""
        candidates = build_candidates(UserItinerary.model_validate(normalized), draft, captured_at=self._clock())
        existing = self._plans.list_for_order(user_id, order_id)
        if existing:
            if _signature(existing[0].candidates) != _signature(candidates):
                raise BusinessError("ORDER_ITINERARY_CONFLICT", "该差旅单已有不同行程安排的计划，请先核对原计划后再提交", 409)
            return {"plan_id": existing[0].plan_id}
        try:
            plan = self._plans.create(user_id, _conditions(draft), order_id=order_id, session_id=session_id)
            plan = self._plans.attach_user_candidates(user_id, plan.plan_id, candidates)
            plan = self._plans.compute(user_id, plan.plan_id)
        except PlanError as exc:
            raise BusinessError(exc.code, str(exc), exc.status) from None
        return {"plan_id": plan.plan_id}
