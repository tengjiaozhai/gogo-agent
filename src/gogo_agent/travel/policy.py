"""032 版本化政策规则与确定性合规判断；034 文档检索另行提供。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import re

from .models import BookingType, PolicyCheckInput, PolicyCheckResult, PolicyRule, PolicyView


DEFAULT_POLICY_PATH = Path(__file__).with_name("policy_seed.json")


class PolicyUnavailable(ValueError):
    """职级、城市或日期没有可用的精确政策规则。"""


class PolicyCatalog:
    """从固定版本数据读取政策和城市交通配置，不让模型解释金额规则。"""

    def __init__(self, path: Path = DEFAULT_POLICY_PATH):
        data = json.loads(path.read_text(encoding="utf-8"))
        self.version: str = data["version"]
        self.tier1_cities = frozenset(data["tier1_cities"])
        self.new_tier1_cities = frozenset(data["new_tier1_cities"])
        self.tier2_cities = frozenset(data["tier2_cities"])
        self.city_pair_minutes = {
            frozenset(pair.split("-", 1)): int(minutes)
            for pair, minutes in data["city_pair_minutes"].items()
        }
        common = {
            "version": self.version,
            "effective_from": data["effective_from"],
            "effective_until": data["effective_until"],
        }
        self.rules = tuple(PolicyRule.model_validate({**row, **common}) for row in data["rules"])

    def city_tier(self, city: str) -> str:
        """城市分级沿用 Java 配置；二线最终匹配其他政策档。"""
        name = city.strip()
        if name in self.tier1_cities:
            return "一线"
        if name in self.new_tier1_cities:
            return "新一线"
        if name in self.tier2_cities:
            return "二线"
        return "其他"

    def travel_minutes(self, departure_city: str, destination: str) -> int | None:
        """只返回显式维护的城市对保守最短衔接时间。"""
        if departure_city == destination:
            return 0
        return self.city_pair_minutes.get(frozenset({departure_city.strip(), destination.strip()}))

    def select(self, user_level: str, city: str, *, on_date: date | None = None) -> PolicyView:
        """按 P 数字职级、目的地等级和生效日选择唯一规则。"""
        matched = re.fullmatch(r"P(\d+)", user_level.strip().upper())
        if matched is None or not city.strip():
            raise PolicyUnavailable("职级或目的地缺失，无法确定适用政策")
        level_num = int(matched.group(1))
        actual_tier = self.city_tier(city)
        policy_tier = actual_tier if actual_tier in ("一线", "新一线") else "其他"
        today = on_date or date.today()
        candidates = [
            rule for rule in self.rules
            if rule.level_min <= level_num <= rule.level_max
            and rule.city_tier == policy_tier
            and rule.effective_from <= today
            and (rule.effective_until is None or today <= rule.effective_until)
        ]
        if len(candidates) != 1:
            raise PolicyUnavailable("没有唯一且仍在有效期内的差旅政策规则")
        return PolicyView(
            destination_city=city.strip(), user_level=user_level.strip().upper(),
            city_tier=actual_tier, rule=candidates[0],
        )


_FLIGHT_RANK = {"经济舱": 1, "商务舱": 2, "头等舱": 3}
_TRAIN_RANK = {"二等座": 1, "一等座": 2, "商务座": 3}


def check_policy(order: PolicyCheckInput, policy: PolicyView) -> PolicyCheckResult:
    """只用结构化字段判断合规；关键金额或舱位缺失时显式拒绝。"""
    violations: list[str] = []
    suggestions: list[str] = []
    if order.amount is None or not order.amount.is_finite() or order.amount <= Decimal("0"):
        violations.append("订单金额缺失或无效，无法判断合规")
        suggestions.append("提供正数金额后重新校验")
    rule = policy.rule
    if order.type is BookingType.HOTEL:
        if order.amount is not None and order.amount > rule.hotel_limit:
            violations.append(f"酒店每晚金额超过 {rule.hotel_limit} 元上限")
            suggestions.append("选择不超过政策上限的酒店")
        if order.hotel_star is not None and order.hotel_star > rule.hotel_star_limit:
            violations.append(f"酒店星级超过 {rule.hotel_star_limit} 星上限")
    elif order.type is BookingType.FLIGHT:
        allowed = max((_FLIGHT_RANK.get(name, 0) for name in rule.flight_class.split("/")), default=0)
        current = _FLIGHT_RANK.get(order.flight_class or "", 0)
        if current == 0 or current > allowed:
            violations.append("机票舱位缺失、未知或超过政策标准")
    elif order.type is BookingType.TRAIN:
        allowed = _TRAIN_RANK.get(rule.train_seat_class, 0)
        current = _TRAIN_RANK.get(order.seat_class or "", 0)
        if current == 0 or current > allowed:
            violations.append("高铁座位缺失、未知或超过政策标准")
    return PolicyCheckResult(compliant=not violations, violations=violations, suggestions=suggestions)
