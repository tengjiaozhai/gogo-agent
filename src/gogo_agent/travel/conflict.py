"""036 不依赖模型的时间重叠和跨城衔接检查。"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import ConflictItem, ConflictReport, TravelOrderView


class TripSegment(BaseModel):
    """一个带明确起止时刻的出差占用区间；结束时刻不计入区间。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: str = Field(..., description="既有差旅单标识；新行程可用候选标识")
    departure_city: str = Field(..., min_length=1, description="本段出发城市")
    destination: str = Field(..., min_length=1, description="本段结束城市")
    start_at: datetime = Field(..., description="开始时刻，业务当地时间")
    end_at: datetime = Field(..., description="不包含的结束时刻，业务当地时间")

    @model_validator(mode="after")
    def validate_window(self):
        """统一使用上海业务当地的无时区墙上时间，拒绝混用 UTC 时间。"""
        if self.start_at.utcoffset() is not None or self.end_at.utcoffset() is not None:
            raise ValueError("冲突检测时刻须使用 Asia/Shanghai 当地无时区时间")
        if self.end_at <= self.start_at:
            raise ValueError("结束时刻必须晚于开始时刻")
        return self


def whole_day_segment(order: TravelOrderView) -> TripSegment:
    """Java 差旅单只有日期，按当地整日占用作保守冲突检查。"""
    return TripSegment(
        order_id=order.order_id,
        departure_city=order.departure_city,
        destination=order.destination,
        start_at=datetime.combine(order.departure_date, time.min),
        end_at=datetime.combine(order.return_date + timedelta(days=1), time.min),
    )


def detect_conflicts(
    candidate: TripSegment,
    existing: list[TripSegment],
    travel_minutes: Callable[[str, str], int | None],
) -> ConflictReport:
    """按闭开区间检测本人行程；未知交通时长列为不确定，不猜测可衔接。"""
    conflicts: list[ConflictItem] = []
    uncertain: list[str] = []
    for other in existing:
        if other.order_id == candidate.order_id:
            continue
        overlaps = candidate.start_at < other.end_at and other.start_at < candidate.end_at
        if overlaps:
            same_route = (
                candidate.departure_city == other.departure_city
                and candidate.destination == other.destination
            )
            conflicts.append(ConflictItem(
                order_id=other.order_id,
                severity="LOW" if same_route else "HIGH",
                reason="同路线日期重叠" if same_route else "跨路线日期重叠",
            ))
            continue

        earlier, later = (candidate, other) if candidate.end_at <= other.start_at else (other, candidate)
        if earlier.destination == later.departure_city:
            continue
        minimum = travel_minutes(earlier.destination, later.departure_city)
        if minimum is None:
            uncertain.append(f"{other.order_id}：缺少 {earlier.destination} 至 {later.departure_city} 的交通衔接数据")
            continue
        gap_minutes = (later.start_at - earlier.end_at).total_seconds() / 60
        if gap_minutes < minimum:
            conflicts.append(ConflictItem(
                order_id=other.order_id, severity="HIGH",
                reason=f"跨城衔接只有 {gap_minutes:g} 分钟，至少需要 {minimum} 分钟",
            ))
        elif minimum < gap_minutes < minimum + 30:
            conflicts.append(ConflictItem(
                order_id=other.order_id, severity="LOW",
                reason="跨城衔接虽可行，但余量少于 30 分钟",
            ))
    return ConflictReport(conflicts=conflicts, uncertain=uncertain)
