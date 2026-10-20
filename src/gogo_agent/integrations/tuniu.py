"""049 途牛 CLI 只读搜索适配器：机票、火车、酒店搜索，并规范化为规划候选。

对照 Java：
- 命令形态与退出码来自 `resources/skills/tuniu-cli/SKILL.md`、`references/common.md`；
- 响应解包与候选转换对应 `SearchResultCaptureHook.unwrapMcpContent` 与
  `SearchResultConverter.convertFlights/convertTrains/convertHotels`；
- 凭证改为经运行器的环境变量传入，替代 `TuniuApiKeyHook` 拼 `env TUNIU_API_KEY=... tuniu ...` 命令文本。

本适配器只放行三个搜索工具。`saveOrder`、`cancelOrder`、`bookTrain`、`tuniuHotelCreateOrder`
被策略显式拒绝；规划路径不得下单或取消，预订写操作属于阶段 F。

未迁移的 Java 行为：翻页合并与去重（`mergeEntries`）、详情工具、按 Redis field 精确读取。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from gogo_agent.integrations.skill_runner import (
    SkillError,
    SkillErrorCode,
    SkillPolicy,
    SkillRunner,
    redact_secrets,
)
from gogo_agent.planning.models import (
    CandidateSet,
    HotelCandidate,
    TransportCandidate,
    TransportType,
)

SEARCH_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("call", "flight", "searchLowestPriceFlight"),
    ("call", "train", "searchLowestPriceTrain"),
    ("call", "hotel", "tuniuHotelSearch"),
)
WRITE_SUBCOMMANDS = frozenset({"saveOrder", "cancelOrder", "bookTrain", "tuniuHotelCreateOrder"})
EXIT_CODE_MESSAGES = {
    101: "连接失败", 102: "工具不存在", 103: "参数错误", 104: "认证失败",
    105: "超时", 108: "未配置 API Key", 199: "未知错误",
}
_API_KEY_ENV = "TUNIU_API_KEY"
_SEAT_CLASSES = (("edz", "二等座"), ("ydz", "一等座"), ("swz", "商务座"))
_MAX_CITY_CHARS = 40


class TuniuSearchResult(BaseModel):
    """一次途牛搜索的规范化结果；供应商请求 ID 单独保存，不进入候选模型。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["flight", "train", "hotel"] = Field(..., description="搜索类型")
    field_key: str = Field(
        ..., description="沿用 Java SearchCandidateStore 的 field 名（如 flight_北京_上海_2026-03-15），用于跨搜索稳定排序",
    )
    transports: list[TransportCandidate] = Field(default_factory=list, description="本次搜索得到的交通候选，编号从 1 起")
    hotels: list[HotelCandidate] = Field(default_factory=list, description="本次搜索得到的酒店候选，编号从 1 起")
    vendor_request_id: str | None = Field(
        default=None, description="供应商原始请求 ID：优先取响应 metadata.requestId，其次取业务数据 queryId；都没有则为空",
    )


def tuniu_policy(
    *, executable: str = "tuniu", fixed_args: Sequence[str] = (),
    timeout_s: float = 30.0, extra_env_names: Sequence[str] = (),
) -> SkillPolicy:
    """途牛只读搜索的运行策略：三个搜索命令白名单，下单类子命令显式拒绝。"""
    return SkillPolicy(
        name="tuniu-cli",
        executable=executable,
        fixed_args=tuple(fixed_args),
        allowed_commands=SEARCH_COMMANDS,
        denied_tokens=WRITE_SUBCOMMANDS,
        max_args=5,
        timeout_s=timeout_s,
        env_allowlist=(_API_KEY_ENV, *extra_env_names),
        exit_code_messages=EXIT_CODE_MESSAGES,
    )


class TuniuCliAdapter:
    """途牛 CLI 只读搜索适配器；每次调用的 API Key 与用户标识由调用方传入。"""

    def __init__(
        self,
        *,
        executable: str = "tuniu",
        fixed_args: Sequence[str] = (),
        base_dir: Path | None = None,
        timeout_s: float = 30.0,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        """`extra_env` 是除 API Key 外显式传给子进程的环境变量（如真实环境的 PATH）；名称自动加入白名单。"""
        self._extra_env = dict(extra_env or {})
        self._runner = SkillRunner(
            tuniu_policy(
                executable=executable, fixed_args=fixed_args, timeout_s=timeout_s,
                extra_env_names=tuple(self._extra_env),
            ),
            base_dir=base_dir,
        )

    async def search_flights(
        self, *, user_id: str, api_key: str, departure_city: str, arrival_city: str, departure_date: date,
    ) -> TuniuSearchResult:
        """搜索机票并规范化。"""
        dep, arr = _city(departure_city), _city(arrival_city)
        payload = {"departureCityName": dep, "arrivalCityName": arr, "departureDate": departure_date.isoformat()}
        body = await self._call(user_id, api_key, ("call", "flight", "searchLowestPriceFlight"), payload)
        inner = _unwrap_content(body)
        return TuniuSearchResult(
            kind="flight",
            field_key=f"flight_{dep}_{arr}_{departure_date.isoformat()}",
            transports=normalize_flights(inner.get("data"), dep, arr),
            vendor_request_id=_request_id(body, inner),
        )

    async def search_trains(
        self, *, user_id: str, api_key: str, departure_city: str, arrival_city: str, departure_date: date,
    ) -> TuniuSearchResult:
        """搜索火车票并按席别展开规范化。"""
        dep, arr = _city(departure_city), _city(arrival_city)
        payload = {"departureCityName": dep, "arrivalCityName": arr, "departureDate": departure_date.isoformat()}
        body = await self._call(user_id, api_key, ("call", "train", "searchLowestPriceTrain"), payload)
        inner = _unwrap_content(body)
        return TuniuSearchResult(
            kind="train",
            field_key=f"train_{dep}_{arr}_{departure_date.isoformat()}",
            transports=normalize_trains(inner.get("data"), dep, arr),
            vendor_request_id=_request_id(body, inner),
        )

    async def search_hotels(
        self, *, user_id: str, api_key: str, city: str, check_in: date, check_out: date,
    ) -> TuniuSearchResult:
        """搜索酒店并规范化；入住晚数取 check_out 与 check_in 之差。"""
        name = _city(city)
        if check_out <= check_in:
            raise SkillError(SkillErrorCode.INVALID_ARGUMENT, "退房日期必须晚于入住日期")
        payload = {"cityName": name, "checkIn": check_in.isoformat(), "checkOut": check_out.isoformat()}
        body = await self._call(user_id, api_key, ("call", "hotel", "tuniuHotelSearch"), payload)
        inner = _unwrap_content(body)
        return TuniuSearchResult(
            kind="hotel",
            field_key=f"hotel_{name}_{check_in.isoformat()}_{check_out.isoformat()}",
            hotels=normalize_hotels(inner.get("hotels"), max(1, (check_out - check_in).days)),
            vendor_request_id=_request_id(body, inner),
        )

    async def _call(
        self, user_id: str, api_key: str, command: tuple[str, str, str], payload: dict[str, str],
    ) -> dict[str, Any]:
        """经运行器执行一次搜索；缺 Key 不启动进程，`success=false` 映射为 VENDOR_ERROR。"""
        if not isinstance(api_key, str) or not api_key.strip():
            raise SkillError(SkillErrorCode.INVALID_ARGUMENT, "未配置途牛 API Key，未发出请求")
        result = await self._runner.run_json(
            user_id=user_id,
            args=[*command, "-a", json.dumps(payload, ensure_ascii=False)],
            env={_API_KEY_ENV: api_key, **self._extra_env},
            secrets=(api_key,),
        )
        body = result.data
        if not isinstance(body, dict):
            raise SkillError(SkillErrorCode.VENDOR_ERROR, "途牛响应不是 JSON 对象")
        if body.get("success") is False:
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            message = redact_secrets(str(error.get("message", "供应商返回失败")), (api_key,))
            raise SkillError(
                SkillErrorCode.VENDOR_ERROR, f"途牛返回失败（code={error.get('code')}）：{message}",
                exit_code=0,
            )
        return body


def to_candidate_set(results: Sequence[TuniuSearchResult], *, captured_at: datetime | None = None) -> CandidateSet:
    """合并多次搜索：按 field_key 字典序重新编号，机票与火车共用交通计数器（F1、F2、T3…），酒店独立（H1…）。"""
    transports: list[TransportCandidate] = []
    hotels: list[HotelCandidate] = []
    for result in sorted(results, key=lambda item: item.field_key):
        for candidate in result.transports:
            prefix = "F" if candidate.type is TransportType.FLIGHT else "T"
            transports.append(candidate.model_copy(update={"id": f"{prefix}{len(transports) + 1}"}))
        for hotel in result.hotels:
            hotels.append(hotel.model_copy(update={"id": f"H{len(hotels) + 1}"}))
    return CandidateSet(
        transports=transports, hotels=hotels, source="tuniu_cli", captured_at=captured_at or datetime.now(UTC),
    )


def normalize_flights(items: Any, departure_city: str | None, arrival_city: str | None) -> list[TransportCandidate]:
    """机票：价格=basePrice+totalTax（四舍五入到分，半数向上）；“直飞”才算直达；退改规则固定为不可退。"""
    candidates: list[TransportCandidate] = []
    for item in _dict_items(items):
        departure_time, arrival_time = _str(item, "departureTime"), _str(item, "arrivalTime")
        origin = departure_city or _str(item, "departureAirport")
        destination = arrival_city or _str(item, "arrivalAirport")
        if not (departure_time and arrival_time and origin and destination):
            continue
        candidates.append(TransportCandidate(
            id=f"F{len(candidates) + 1}",
            type=TransportType.FLIGHT,
            carrier=_str(item, "airlineCompany"),
            code=_str(item, "flightNumber"),
            departure_time=departure_time,
            arrival_time=arrival_time,
            origin=origin,
            destination=destination,
            price=_round2(_parse_double(_str(item, "basePrice")) + _parse_double(_str(item, "totalTax"))),
            cabin_class=_str(item, "cabinClass"),
            is_direct=_str(item, "type") == "直飞",
            refund_policy="不可退",
        ))
    return candidates


def normalize_trains(items: Any, departure_city: str | None, arrival_city: str | None) -> list[TransportCandidate]:
    """火车：按二等座/一等座/商务座展开；无该席别价格或余票为 0 的席别跳过；余票字段缺失则保留。"""
    candidates: list[TransportCandidate] = []
    for item in _dict_items(items):
        price, seats = item.get("price"), item.get("seatAvailable")
        if not isinstance(price, dict):
            continue
        departure_time, arrival_time = _str(item, "departureTime"), _str(item, "arrivalTime")
        origin = departure_city or _str(item, "departStationName")
        destination = arrival_city or _str(item, "destStationName")
        if not (departure_time and arrival_time and origin and destination):
            continue
        for key, label in _SEAT_CLASSES:
            price_text = _str(price, f"{key}Price")
            if price_text is None or not price_text.strip():
                continue
            available = _get_int(seats, f"{key}Num") if isinstance(seats, dict) else None
            if available == 0:
                continue
            candidates.append(TransportCandidate(
                id=f"T{len(candidates) + 1}",
                type=TransportType.TRAIN,
                carrier=_str(item, "trainNum"),
                code=_str(item, "trainNum"),
                departure_time=departure_time,
                arrival_time=arrival_time,
                origin=origin,
                destination=destination,
                price=_round2(_parse_double(price_text)),
                cabin_class=label,
                is_direct=_str(item, "trainType") == "direct",
                refund_policy="可退",
            ))
    return candidates


def normalize_hotels(items: Any, nights: int) -> list[HotelCandidate]:
    """酒店：星级按豪华型5/高档型4/舒适型3/经济型2/其余3；含“早餐”字样即含早；缺酒店名称的条目跳过。"""
    candidates: list[HotelCandidate] = []
    for item in _dict_items(items):
        name = _str(item, "hotelName")
        if not name:
            continue
        meal = _str(item, "meal")
        candidates.append(HotelCandidate(
            id=f"H{len(candidates) + 1}",
            name=name,
            brand=_str(item, "brandName"),
            star_rating=_star_rating(_str(item, "starName")),
            room_type=_str(item, "roomName"),
            price_per_night=_get_double(item, "lowestPrice", 0.0),
            nights=nights,
            distance_to_dest_km=0,
            breakfast_included=meal is not None and "早餐" in meal,
            cancel_policy=_str(item, "refund"),
        ))
    return candidates


def _star_rating(star_name: str | None) -> int:
    if star_name is None:
        return 3
    for word, rating in (("豪华", 5), ("高档", 4), ("舒适", 3), ("经济", 2)):
        if word in star_name:
            return rating
    return 3


def _unwrap_content(body: dict[str, Any]) -> dict[str, Any]:
    """取 result.content[].text 中第一个可解析的 JSON 对象；没有则返回 result 本身。"""
    result = body.get("result") if isinstance(body.get("result"), dict) else body
    content = result.get("content")
    if isinstance(content, list):
        for entry in content:
            if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                try:
                    inner = json.loads(entry["text"])
                except ValueError:
                    continue
                if isinstance(inner, dict):
                    return inner
    return result


def _request_id(body: dict[str, Any], inner: dict[str, Any]) -> str | None:
    metadata = body.get("metadata")
    for value in (metadata.get("requestId") if isinstance(metadata, dict) else None, inner.get("queryId")):
        if value not in (None, ""):
            return str(value)
    return None


def _city(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > _MAX_CITY_CHARS:
        raise SkillError(SkillErrorCode.INVALID_ARGUMENT, f"城市名必须是 1 到 {_MAX_CITY_CHARS} 个字符")
    if any(ord(ch) < 32 for ch in value):
        raise SkillError(SkillErrorCode.INVALID_ARGUMENT, "城市名不能包含控制字符")
    return value.strip()


def _dict_items(items: Any) -> list[dict[str, Any]]:
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _str(obj: Mapping[str, Any], key: str) -> str | None:
    """对应 Java getString：null 为空，其余转字符串。"""
    value = obj.get(key)
    return None if value is None else str(value)


def _parse_double(text: str | None) -> float:
    """对应 Java parseDoubleSafe：空白或无法解析为 0；额外把 NaN/Infinity 也当 0。"""
    if text is None or not text.strip():
        return 0.0
    try:
        value = float(text.strip())
    except ValueError:
        return 0.0
    return value if math.isfinite(value) else 0.0


def _get_double(obj: Mapping[str, Any], key: str, default: float) -> float:
    """对应 Java getDblOr：数字直接用，字符串按浮点解析，失败或非有限数取默认值；布尔不当数字。"""
    value = obj.get(key)
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value if isinstance(value, (int, float)) else str(value).strip())
    except ValueError:
        return default
    return number if math.isfinite(number) else default


def _get_int(obj: Mapping[str, Any], key: str) -> int | None:
    """对应 Java getInt：数字取整数部分，字符串按整数解析，失败为空。"""
    value = obj.get(key)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if math.isfinite(value) else None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _round2(value: float) -> float:
    """对应 Java Math.round(x*100)/100：半数向上，而不是 Python round 的银行家舍入。"""
    return math.floor(value * 100.0 + 0.5) / 100.0
