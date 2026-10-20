"""044/045 确定性方案计算：组合、过滤、体验与政策评分、归一化和代表方案选择。

对照 Java `ItineraryPlannerTools` 与 `planner` 包；权重基线为当前 Java 的
0.20/0.10/0.40/0.30。模型只提供偏好分，所有数学都在这里，提示词变化不会改变结果。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .models import (
    CandidateSet, DimensionWeights, ExcludedCombo, HotelCandidate, PlanPolicy, PlanResult,
    PreferenceScore, PreferenceScores, Proposal, ProposalMetrics, ProposalScores,
    TransportCandidate, TransportType,
)

# 无偏移的候选时间按东八区解释；带偏移的按真实时刻计算跨时区时长。
DEFAULT_TZ = timezone(timedelta(hours=8))

TAG_ORDER = ("综合最佳", "时间最短", "价格最低", "最符合偏好")
RED_EYE_DEPARTURE_HOUR = 22
RED_EYE_ARRIVAL_HOUR = 6
LATE_ARRIVAL_HOUR = 21
LONG_COMMUTE_MINUTES = 240
SEVERE_COMMUTE_MINUTES = 360
RED_EYE_PENALTY = 30.0
LATE_ARRIVAL_PENALTY = 15.0
LAST_TRANSPORT_PENALTY = 10.0
LONG_COMMUTE_PENALTY = 15.0
SEVERE_COMMUTE_PENALTY = 25.0
BAD_WEATHER_FLIGHT_PENALTY = 25.0
BAD_WEATHER_KEYWORDS = ("暴雨", "大雾", "大风", "雷暴", "暴雪", "雾霾", "台风", "冰雹")
POLICY_BASE_SCORE = 100.0
POLICY_VIOLATION_PENALTY = 40.0
POLICY_WARNING_PENALTY = 15.0
NEUTRAL_SCORE = 50.0

_CABIN_RANK = {
    "经济舱": 1, "economy": 1, "经济": 1,
    "商务舱": 2, "business": 2, "商务": 2,
    "头等舱": 3, "first": 3, "头等": 3,
    "二等座": 1, "second": 1, "一等座": 2, "商务座": 3,
}


def round2(value: float) -> float:
    """与 Java Math.round 一致的“四舍五入到半数向上”，保证两端结果可对照。"""
    return math.floor(value * 100.0 + 0.5) / 100.0


def round1(value: float) -> float:
    """保留一位小数，规则同 round2。"""
    return math.floor(value * 10.0 + 0.5) / 10.0


def parse_dt(text: str | None) -> datetime | None:
    """解析候选时间；无法解析返回 None，调用方必须显式处理。"""
    if text is None:
        return None
    value = text.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=DEFAULT_TZ)


def duration_minutes(departure: str, arrival: str) -> int | None:
    """按真实时刻计算分钟数；任一时间不可解析返回 None。"""
    dep, arr = parse_dt(departure), parse_dt(arrival)
    if dep is None or arr is None:
        return None
    return int(round((arr - dep).total_seconds() / 60.0))


def format_duration(minutes: int) -> str:
    """把分钟格式化为 5h20m。"""
    return f"{minutes // 60}h{minutes % 60:02d}m"


def minmax_score(value: float, low: float, high: float, higher_is_better: bool) -> float:
    """线性归一化到 0–100；最大最小相等时全部满分。"""
    if high == low:
        return 100.0
    ratio = (value - low) / (high - low)
    if not higher_is_better:
        ratio = 1.0 - ratio
    return round2(min(1.0, max(0.0, ratio)) * 100.0)


def normalize_weights(weights: DimensionWeights) -> DimensionWeights:
    """按和为 1 归一化；全为 0 时回到基线权重。"""
    total = weights.time + weights.price + weights.preference + weights.experience
    if total <= 0:
        return DimensionWeights()
    return DimensionWeights(
        time=round2(weights.time / total), price=round2(weights.price / total),
        preference=round2(weights.preference / total), experience=round2(weights.experience / total),
    )


def cabin_rank(name: str | None) -> int:
    """舱位等级；未知名称返回 -1。"""
    return _CABIN_RANK.get((name or "").strip().lower(), -1)


def is_cabin_compliant(actual: str | None, allowed_spec: str | None) -> bool:
    """实际等级不高于政策允许的任一等级即合规；缺失数据不拦截。"""
    if not actual or not actual.strip() or not allowed_spec or not allowed_spec.strip():
        return True
    allowed_items = [item.strip() for item in allowed_spec.replace("，", ",").replace("/", ",").split(",") if item.strip()]
    actual_rank = cabin_rank(actual)
    for allowed in allowed_items:
        allowed_rank = cabin_rank(allowed)
        if actual_rank >= 0 and allowed_rank >= 0:
            if actual_rank <= allowed_rank:
                return True
        else:
            left, right = actual.lower(), allowed.lower()
            if left == right or right in left or left in right:
                return True
    return False


def has_severe_weather(summary: str) -> bool:
    """天气摘要含恶劣天气关键词。"""
    return any(keyword in summary for keyword in BAD_WEATHER_KEYWORDS)


def _is_red_eye(leg: "Leg") -> bool:
    if leg.candidate.type != TransportType.FLIGHT:
        return False
    return leg.departure.hour >= RED_EYE_DEPARTURE_HOUR or leg.arrival.hour < RED_EYE_ARRIVAL_HOUR


@dataclass(frozen=True)
class Leg:
    """已解析时间与时长的交通候选，避免反复解析文本。"""

    candidate: TransportCandidate
    departure: datetime
    arrival: datetime
    transit_min: int


def experience_score(outbound: Leg, inbound: Leg, weather_summary: str) -> float:
    """出行体验原始分：红眼、晚到、末班、长通勤、恶劣天气选飞机均扣分，最低 0。"""
    score = 100.0
    for leg in (outbound, inbound):
        if _is_red_eye(leg):
            score -= RED_EYE_PENALTY
    if outbound.arrival.hour >= LATE_ARRIVAL_HOUR:
        score -= LATE_ARRIVAL_PENALTY
    if inbound.departure.hour >= LATE_ARRIVAL_HOUR and inbound.candidate.type == TransportType.FLIGHT:
        score -= LAST_TRANSPORT_PENALTY
    for leg in (outbound, inbound):
        if leg.transit_min >= SEVERE_COMMUTE_MINUTES:
            score -= SEVERE_COMMUTE_PENALTY
        elif leg.transit_min >= LONG_COMMUTE_MINUTES:
            score -= LONG_COMMUTE_PENALTY
    if has_severe_weather(weather_summary):
        for leg in (outbound, inbound):
            if leg.candidate.type == TransportType.FLIGHT:
                score -= BAD_WEATHER_FLIGHT_PENALTY
    return round2(max(0.0, score))


def experience_flags(outbound: Leg, inbound: Leg, weather_summary: str) -> list[str]:
    """出行体验风险标记，供方案展示和“避开体验风险”的代表方案选择。"""
    flags: list[str] = []
    if _is_red_eye(outbound):
        flags.append("去程为红眼航班")
    if _is_red_eye(inbound):
        flags.append("返程为红眼航班")
    if outbound.arrival.hour >= LATE_ARRIVAL_HOUR:
        flags.append(f"去程到达时间过晚（{outbound.arrival.hour}:00 后）")
    if outbound.transit_min >= LONG_COMMUTE_MINUTES:
        flags.append("去程单程通勤" + format_duration(outbound.transit_min))
    if inbound.transit_min >= LONG_COMMUTE_MINUTES:
        flags.append("返程单程通勤" + format_duration(inbound.transit_min))
    if has_severe_weather(weather_summary) and TransportType.FLIGHT in (outbound.candidate.type, inbound.candidate.type):
        flags.append("恶劣天气下选择飞机出行")
    return flags


def _fmt_num(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)


def check_policy(
    outbound: TransportCandidate, hotel: HotelCandidate, inbound: TransportCandidate,
    total_price: float, policy: PlanPolicy,
) -> tuple[list[str], list[str], float]:
    """政策软约束：只记录违规与告警并扣分，绝不剔除组合。"""
    violations: list[str] = []
    warnings: list[str] = []
    if policy.hotel_limit is not None and hotel.price_per_night > policy.hotel_limit:
        violations.append(f"酒店单晚 {_fmt_num(hotel.price_per_night)} 超过限额 {_fmt_num(policy.hotel_limit)}")
    if policy.hotel_star_limit is not None and hotel.star_rating is not None and hotel.star_rating > policy.hotel_star_limit:
        warnings.append(f"酒店星级 {hotel.star_rating} 超过政策上限 {policy.hotel_star_limit} 星")
    if policy.approval_threshold is not None and total_price > policy.approval_threshold:
        violations.append(f"总价 {total_price:.0f} 超过审批阈值 {_fmt_num(policy.approval_threshold)}")
    for label, leg in (("去程", outbound), ("返程", inbound)):
        cabin = (leg.cabin_class or "").strip()
        if not cabin:
            continue
        allowed = policy.train_seat_class if leg.type == TransportType.TRAIN else policy.flight_class
        if allowed and not is_cabin_compliant(cabin, allowed):
            warnings.append(f"{label}舱位 '{cabin}' 超出政策允许最高等级 {allowed}")
    score = POLICY_BASE_SCORE - POLICY_VIOLATION_PENALTY * len(violations) - POLICY_WARNING_PENALTY * len(warnings)
    return violations, warnings, round2(max(0.0, score))


def _city_matches(left: str, right: str) -> bool:
    prefix = left[:2] if len(left) >= 2 else left
    return left.startswith(right) or right.startswith(prefix)


def classify_direction(candidate: TransportCandidate, origin: str, destination: str, departure: datetime, return_date: str) -> str:
    """显式方向 > 出发城市匹配 > 日期匹配；与 Java 的判定优先级一致。"""
    if candidate.direction:
        return candidate.direction
    if candidate.origin and origin:
        if _city_matches(candidate.origin, origin):
            return "outbound"
        if destination and _city_matches(candidate.origin, destination):
            return "return"
    ret = parse_dt(return_date)
    if ret is not None and departure.date() >= ret.date():
        return "return"
    return "outbound"


@dataclass
class _Combo:
    outbound: Leg
    hotel: HotelCandidate
    inbound: Leg
    total_price: float
    total_transit_min: int
    stay_hours: float | None
    policy_score: float
    violations: list[str]
    warnings: list[str]
    experience_raw: float
    flags: list[str]
    time_score: float = 0.0
    price_score: float = 0.0
    experience_norm: float = 0.0
    preference_score: float = 0.0
    preference_basis: list[str] = field(default_factory=list)
    overall: float = 0.0

    @property
    def combo_id(self) -> str:
        return f"{self.outbound.candidate.id}|{self.hotel.id}|{self.inbound.candidate.id}"


class PlanningError(ValueError):
    """规划输入无法形成任何组合时的明确错误，带稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _to_leg(candidate: TransportCandidate, excluded: list[ExcludedCombo]) -> Leg | None:
    """校验并解析单条交通；异常价格或时间的候选被淘汰并记录原因。"""
    if candidate.price <= 0:
        excluded.append(ExcludedCombo(combo_id=candidate.id, reason=f"价格异常（{candidate.price}），候选被剔除"))
        return None
    departure, arrival = parse_dt(candidate.departure_time), parse_dt(candidate.arrival_time)
    if departure is None or arrival is None:
        excluded.append(ExcludedCombo(combo_id=candidate.id, reason="出发或到达时间无法解析，候选被剔除"))
        return None
    minutes = int(round((arrival - departure).total_seconds() / 60.0))
    if minutes <= 0:
        excluded.append(ExcludedCombo(combo_id=candidate.id, reason="到达时间不晚于出发时间，候选被剔除"))
        return None
    return Leg(candidate, departure, arrival, minutes)


def build_combos(
    candidates: CandidateSet, origin: str, destination: str, return_date: str,
    policy: PlanPolicy, weather_summary: str, excluded: list[ExcludedCombo],
) -> list[_Combo]:
    """去程×酒店×返程笛卡尔积，过滤返程早于去程到达的组合。"""
    outbound: list[Leg] = []
    inbound: list[Leg] = []
    for candidate in candidates.transports:
        leg = _to_leg(candidate, excluded)
        if leg is None:
            continue
        side = classify_direction(candidate, origin, destination, leg.departure, return_date)
        (outbound if side == "outbound" else inbound).append(leg)
    hotels: list[HotelCandidate] = []
    for hotel in candidates.hotels:
        if hotel.price_per_night <= 0:
            excluded.append(ExcludedCombo(combo_id=hotel.id, reason=f"酒店价格异常（{hotel.price_per_night}），候选被剔除"))
        else:
            hotels.append(hotel)
    if not candidates.transports or not candidates.hotels:
        raise PlanningError("NO_CANDIDATES", "缺少交通或酒店候选，无法规划")
    if not outbound or not inbound:
        raise PlanningError(
            "NO_LEG", f"去程或返程为空（去程 {len(outbound)} 条 / 返程 {len(inbound)} 条），请检查方向、城市和日期",
        )
    if not hotels:
        raise PlanningError("NO_CANDIDATES", "全部酒店候选价格异常，无法规划")
    combos: list[_Combo] = []
    for out_leg in outbound:
        for hotel in hotels:
            for in_leg in inbound:
                if in_leg.departure <= out_leg.arrival:
                    excluded.append(ExcludedCombo(
                        combo_id=f"{out_leg.candidate.id}|{hotel.id}|{in_leg.candidate.id}",
                        reason="返程出发不晚于去程到达",
                    ))
                    continue
                total_price = out_leg.candidate.price + in_leg.candidate.price + hotel.price_per_night * max(1, hotel.nights)
                violations, warnings, policy_score = check_policy(
                    out_leg.candidate, hotel, in_leg.candidate, total_price, policy,
                )
                combos.append(_Combo(
                    outbound=out_leg, hotel=hotel, inbound=in_leg, total_price=round2(total_price),
                    total_transit_min=out_leg.transit_min + in_leg.transit_min,
                    stay_hours=round1((in_leg.departure - out_leg.arrival).total_seconds() / 3600.0),
                    policy_score=policy_score, violations=violations, warnings=warnings,
                    experience_raw=experience_score(out_leg, in_leg, weather_summary),
                    flags=experience_flags(out_leg, in_leg, weather_summary),
                ))
    if not combos:
        raise PlanningError("ALL_FILTERED", "所有组合都被过滤，可能是返程时间都早于去程到达")
    return combos


def _normalize(combos: list[_Combo]) -> None:
    times = [c.total_transit_min for c in combos]
    prices = [c.total_price for c in combos]
    experiences = [c.experience_raw for c in combos]
    for combo in combos:
        combo.time_score = minmax_score(combo.total_transit_min, min(times), max(times), False)
        combo.price_score = minmax_score(combo.total_price, min(prices), max(prices), False)
        combo.experience_norm = minmax_score(combo.experience_raw, min(experiences), max(experiences), True)


def neutral_scores(candidates: CandidateSet) -> PreferenceScores:
    """无偏好或快速初稿时为全部候选生成中性分 50。"""
    basis = ["无偏好设置，使用中性分"]
    return PreferenceScores(
        transport_scores={c.id: PreferenceScore(score=NEUTRAL_SCORE, basis=basis) for c in candidates.transports},
        hotel_scores={h.id: PreferenceScore(score=NEUTRAL_SCORE, basis=basis) for h in candidates.hotels},
    )


def _pick_best(combos: list[_Combo], key1, key2, higher_is_better: bool) -> _Combo:
    best = combos[0]
    for combo in combos[1:]:
        first = (key1(combo) > key1(best)) - (key1(combo) < key1(best))
        cmp = first if first != 0 else (key2(combo) > key2(best)) - (key2(combo) < key2(best))
        if (cmp > 0) if higher_is_better else (cmp < 0):
            best = combo
    return best


def _pick_experience_aware(combos: list[_Combo], key1, key2, higher_is_better: bool) -> _Combo:
    """先取极值；若有体验风险，则在 15% 容差内优先无风险组合。"""
    best = _pick_best(combos, key1, key2, higher_is_better)
    if not best.flags:
        return best
    best_value = key1(best)
    tolerance = best_value * 0.85 if higher_is_better else best_value * 1.15
    clean = [
        c for c in combos
        if not c.flags and (key1(c) >= tolerance if higher_is_better else key1(c) <= tolerance)
    ]
    return _pick_best(clean, key1, key2, higher_is_better) if clean else best


def compute_plan(
    candidates: CandidateSet, *, origin: str, destination: str, return_date: str,
    policy: PlanPolicy | None = None, scores: PreferenceScores | None = None,
    weights: DimensionWeights | None = None, weather_summary: str = "", preferences: str = "",
    now: datetime | None = None,
) -> PlanResult:
    """主入口：组合→归一化→合成偏好与综合分→选代表方案。纯函数，不读写存储。"""
    policy = policy or PlanPolicy()
    scores = scores or neutral_scores(candidates)
    used_weights = normalize_weights(weights or DimensionWeights())
    excluded: list[ExcludedCombo] = []
    combos = build_combos(candidates, origin, destination, return_date, policy, weather_summary, excluded)
    _normalize(combos)
    for combo in combos:
        out_s = scores.transport_scores.get(combo.outbound.candidate.id, PreferenceScore(score=0))
        hotel_s = scores.hotel_scores.get(combo.hotel.id, PreferenceScore(score=0))
        in_s = scores.transport_scores.get(combo.inbound.candidate.id, PreferenceScore(score=0))
        combo.preference_score = round2((out_s.score + hotel_s.score + in_s.score) / 3.0)
        combo.preference_basis = (
            [f"去程: {item}" for item in out_s.basis] + [f"住宿: {item}" for item in hotel_s.basis]
            + [f"返程: {item}" for item in in_s.basis]
        )
        combo.overall = round2(
            combo.time_score * used_weights.time + combo.price_score * used_weights.price
            + combo.preference_score * used_weights.preference + combo.experience_norm * used_weights.experience
        )
    raw_fastest = _pick_best(combos, lambda c: c.total_transit_min, lambda c: c.total_price, False)
    raw_cheapest = _pick_best(combos, lambda c: c.total_price, lambda c: c.total_transit_min, False)
    picks = {
        "综合最佳": _pick_best(combos, lambda c: c.overall, lambda c: c.preference_score, True),
        "时间最短": _pick_experience_aware(combos, lambda c: c.total_transit_min, lambda c: c.total_price, False),
        "价格最低": _pick_experience_aware(combos, lambda c: c.total_price, lambda c: c.total_transit_min, False),
        "最符合偏好": _pick_best(combos, lambda c: c.preference_score, lambda c: c.overall, True),
    }
    reasons = {
        "综合最佳": lambda c: f"综合最佳：综合分 {c.overall} 为 {len(combos)} 个组合中最高",
        "时间最短": lambda c: f"时间最短：总通勤 {format_duration(c.total_transit_min)}"
                              + (f"，已避开更快但有体验风险的 {raw_fastest.combo_id}"
                                 if c.combo_id != raw_fastest.combo_id else ""),
        "价格最低": lambda c: f"价格最低：总价 {c.total_price:g}"
                              + (f"，已避开更便宜但有体验风险的 {raw_cheapest.combo_id}"
                                 if c.combo_id != raw_cheapest.combo_id else ""),
        "最符合偏好": lambda c: f"最符合偏好：偏好分 {c.preference_score:g}",
    }
    merged: dict[str, tuple[_Combo, list[str], list[str]]] = {}
    for tag in TAG_ORDER:
        combo = picks[tag]
        entry = merged.setdefault(combo.combo_id, (combo, [], []))
        entry[1].append(tag)
        entry[2].append(reasons[tag](combo))
    proposals = [
        Proposal(
            tags=tags, combo_id=combo.combo_id, outbound=combo.outbound.candidate, hotel=combo.hotel,
            inbound=combo.inbound.candidate,
            metrics=ProposalMetrics(
                total_price=combo.total_price, total_transit_min=combo.total_transit_min,
                total_transit_hhmm=format_duration(combo.total_transit_min), stay_hours=combo.stay_hours,
            ),
            scores=ProposalScores(
                time=combo.time_score, price=combo.price_score, preference=combo.preference_score,
                policy=combo.policy_score, experience=combo.experience_norm, overall=combo.overall,
            ),
            preference_basis=combo.preference_basis, policy_violations=combo.violations,
            warnings=combo.warnings, experience_flags=combo.flags, selection_basis=basis,
        )
        for combo, tags, basis in merged.values()
    ]
    proposals.sort(key=lambda item: item.scores.overall, reverse=True)
    return PlanResult(
        weights=used_weights, policy=policy, combo_count=len(combos), excluded=excluded, proposals=proposals,
        preferences=preferences, weather_summary=weather_summary, candidate_source=candidates.source,
        generated_at=now or datetime.now(timezone.utc),
    )
