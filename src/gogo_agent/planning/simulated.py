"""模拟外部响应：固定候选、固定政策与确定性评估器，只用于练习、脚本和测试。

这些替身不代表真实途牛、天气或模型服务；真实供应商与模型路径未在此验证。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import (
    CandidateSet, HotelCandidate, PlanConditions, PlanPolicy, ReviewResult, ReviewSource,
    ReviewVerdict, TransportCandidate, TransportType,
)
from .review import Itinerary

SIMULATED_SOURCE = "simulated"
SIMULATED_CAPTURED_AT = datetime(2026, 10, 11, 4, 0, tzinfo=timezone.utc)

SIMULATED_POLICY = PlanPolicy(
    hotel_limit=450, hotel_star_limit=4, flight_class="经济舱", train_seat_class="二等座",
    approval_threshold=5000, advance_booking_days=3, version="simulated-policy-v1",
)


def simulated_provider(user_id: str, conditions: PlanConditions) -> CandidateSet:
    """与工具层的搜索签名一致的模拟入口；user_id 不参与模拟数据生成。"""
    return simulated_search(conditions)


def simulated_search(conditions: PlanConditions) -> CandidateSet:
    """按条件生成固定形状的候选：含显式方向、城市推断方向、红眼、长通勤、超标酒店等典型样例。"""
    dep = conditions.departure_date.isoformat()
    ret = conditions.return_date.isoformat()
    origin, destination = conditions.origin, conditions.destination
    nights = (conditions.return_date - conditions.departure_date).days
    next_day = (conditions.departure_date + timedelta(days=1)).isoformat()
    flight, train = TransportType.FLIGHT, TransportType.TRAIN
    transports = [
        TransportCandidate(id="F1", type=flight, carrier="模拟航空A", code="SA101",
                           departure_time=f"{dep}T07:30:00+08:00", arrival_time=f"{dep}T09:40:00+08:00",
                           origin=origin, destination=destination, price=1200, cabin_class="经济舱",
                           is_direct=True, refund_policy="可退", direction="outbound"),
        TransportCandidate(id="F2", type=flight, carrier="模拟航空B", code="SB202",
                           departure_time=f"{dep}T23:30:00+08:00", arrival_time=f"{next_day}T01:50:00+08:00",
                           origin=origin, destination=destination, price=800, cabin_class="经济舱",
                           is_direct=True, refund_policy="不可退"),
        TransportCandidate(id="T1", type=train, carrier="模拟铁路", code="G101",
                           departure_time=f"{dep}T06:00:00+08:00", arrival_time=f"{dep}T11:30:00+08:00",
                           origin=origin, destination=destination, price=553, cabin_class="二等座",
                           is_direct=True, refund_policy="可退", direction="outbound"),
        TransportCandidate(id="F3", type=flight, carrier="模拟航空A", code="SA303",
                           departure_time=f"{ret}T18:00:00+08:00", arrival_time=f"{ret}T20:10:00+08:00",
                           origin=destination, destination=origin, price=1100, cabin_class="经济舱",
                           is_direct=True, refund_policy="可退", direction="return"),
        TransportCandidate(id="F4", type=flight, carrier="模拟航空C", code="SC404",
                           departure_time=f"{ret}T06:00:00+08:00", arrival_time=f"{ret}T08:10:00+08:00",
                           origin=destination, destination=origin, price=700, cabin_class="经济舱",
                           is_direct=True, refund_policy="不可退"),
        TransportCandidate(id="T2", type=train, carrier="模拟铁路", code="G102",
                           departure_time=f"{ret}T19:00:00+08:00", arrival_time=f"{ret}T23:30:00+08:00",
                           origin=destination, destination=origin, price=380, cabin_class="二等座",
                           is_direct=True, refund_policy="可退"),
    ]
    hotels = [
        HotelCandidate(id="H1", name="模拟酒店A", brand="A", star_rating=4, room_type="大床房",
                       price_per_night=420, nights=nights, distance_to_dest_km=3.5,
                       breakfast_included=True, cancel_policy="免费取消"),
        HotelCandidate(id="H2", name="模拟酒店B", brand="B", star_rating=3, room_type="标准间",
                       price_per_night=320, nights=nights, distance_to_dest_km=6.0,
                       breakfast_included=False, cancel_policy="到店前一天免费取消"),
        HotelCandidate(id="H3", name="模拟酒店C", brand="C", star_rating=5, room_type="行政房",
                       price_per_night=780, nights=nights, distance_to_dest_km=20.0,
                       breakfast_included=True, cancel_policy="不可取消"),
    ]
    return CandidateSet(transports=transports, hotels=hotels, source=SIMULATED_SOURCE, captured_at=SIMULATED_CAPTURED_AT)


class StaticAssessor:
    """固定结论的主观评估替身；可指定 fail 或抛出异常，用于验证仲裁与可见性。"""

    def __init__(self, source: ReviewSource, verdict: ReviewVerdict = ReviewVerdict.PASS,
                 error: Exception | None = None, evidence: str = "模拟主观评估") -> None:
        self.source = source
        self._verdict = verdict
        self._error = error
        self._evidence = evidence

    async def assess(self, itinerary: Itinerary) -> ReviewResult:
        """返回固定结论；只读输入，不访问模型或业务数据。"""
        if self._error is not None:
            raise self._error
        return ReviewResult(source=self.source, verdict=self._verdict, details={"evidence": self._evidence})


def simulated_assessors(verdict: ReviewVerdict = ReviewVerdict.PASS) -> list[StaticAssessor]:
    """三个有限主观评估器（体验、韧性、偏好）的替身，数量与 053 设计一致。"""
    return [
        StaticAssessor(ReviewSource.EXPERIENCE, verdict),
        StaticAssessor(ReviewSource.RESILIENCE, verdict),
        StaticAssessor(ReviewSource.PREFERENCE, verdict),
    ]
