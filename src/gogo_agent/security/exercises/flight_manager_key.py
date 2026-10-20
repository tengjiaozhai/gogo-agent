"""050 航班管家调用契约与 Key 占位符替换练习。

**当前未启用，不属于已迁移业务功能。** Java 的 ``flight-manager`` Skill 资源与 ``FlightApiKeyHook`` 存在，
但规划/预订 Agent 的 SkillBox 没有注册该 Skill，``ApiKeyTools`` 里的 flight 工具也已注释。
本文件只用假数据练习契约，不接入规划/预订路径，也不访问真实航班管家服务。

契约来源：``skills/flight-manager/SKILL.md`` 与 ``inputSchema.json``（端点 ``fly.huoli.com/mcp/flight_server``、
``Authorization: Bearer ${FLIGHT_API_KEY}``、舱位 Y/S/C/F）。供应商**返回**字段名（``flightNo`` 等）在 Java 资源中
没有样例，下面的 ``parse_offer`` 用的是练习自拟的假数据结构，未对照真实响应验证。

与 Java ``FlightApiKeyHook`` 的差异：
- 占位符只在**请求头值**里替换，且只对 ``fly.huoli.com`` 的 HTTPS 请求生效；出现在 URL/请求体里直接拒绝；
- 用户来自 ``user_scope``（contextvar，对应 Java 的 Reactor Context），不来自模型参数；
- 没有该用户的 Key 时失败，而不是像 Java 那样带着未替换的占位符继续执行；
- 明文只存在于 ``send`` 的局部变量里，不放进任何返回对象。
"""

import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date as Date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gogo_agent.security.apikey import ApiKeyService
from gogo_agent.security.errors import SecurityError

PROVIDER = "flight-manager"
ENDPOINT = "https://fly.huoli.com/mcp/flight_server"
PLACEHOLDER = "${FLIGHT_API_KEY}"
_HOST = "fly.huoli.com"

INVALID_FLIGHT_NO = "FLIGHT_INVALID_FLIGHT_NO"
INVALID_TIME = "FLIGHT_INVALID_TIME"
INVALID_CABIN = "FLIGHT_INVALID_CABIN"
INVALID_PRICE = "FLIGHT_INVALID_PRICE"
NO_USER_SCOPE = "FLIGHT_NO_USER_SCOPE"
PLACEHOLDER_MISPLACED = "FLIGHT_PLACEHOLDER_MISPLACED"
TARGET_NOT_ALLOWED = "FLIGHT_TARGET_NOT_ALLOWED"

_CABINS = {"Y": "经济舱", "S": "超级经济舱", "C": "商务舱", "F": "头等舱"}
_CABIN_BY_NAME = {name: code for code, name in _CABINS.items()}
_FLIGHT_NO = re.compile(r"^[A-Z0-9]{2}\d{1,4}[A-Z]?$")

_current_user: ContextVar[Optional[str]] = ContextVar("flight_exercise_user", default=None)


class FlightExerciseError(SecurityError):
    """练习中的转换或注入失败；文本不含 Key。"""


@contextmanager
def user_scope(user_id: str) -> Iterator[None]:
    """绑定当前任务的可信用户；退出、异常和取消时恢复原值，并发任务互不可见。"""
    token = _current_user.set(user_id)
    try:
        yield
    finally:
        _current_user.reset(token)


def normalize_flight_no(text: str) -> str:
    """``" ca1501 "`` → ``"CA1501"``；格式不符抛错。"""
    value = text.strip().upper().replace(" ", "")
    if not _FLIGHT_NO.match(value):
        raise FlightExerciseError(INVALID_FLIGHT_NO, "航班号格式不合法")
    return value


def parse_flight_time(text: str) -> datetime:
    """接受 ``YYYY-MM-DD HH:MM`` 或 ISO ``T`` 分隔；练习不处理时区，保持供应商本地时间。"""
    try:
        return datetime.fromisoformat(text.strip().replace(" ", "T", 1))
    except ValueError:
        raise FlightExerciseError(INVALID_TIME, "时间格式不合法") from None


def cabin_code(text: str) -> str:
    """``Y/S/C/F`` 或中文舱位名 → 供应商舱位代码。"""
    value = text.strip()
    code = value.upper() if value.upper() in _CABINS else _CABIN_BY_NAME.get(value)
    if code is None:
        raise FlightExerciseError(INVALID_CABIN, "舱位不在 Y/S/C/F 范围内")
    return code


def parse_price(text: str | int | float | Decimal) -> Decimal:
    """``"¥1,280.5"`` → ``Decimal("1280.50")``；负数、零和非数字拒绝。"""
    cleaned = re.sub(r"[¥￥,\s]", "", str(text))
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise FlightExerciseError(INVALID_PRICE, "价格不是数字") from None
    if not value.is_finite() or value <= 0:
        raise FlightExerciseError(INVALID_PRICE, "价格必须是大于 0 的有限数值")
    return value.quantize(Decimal("0.01"))


class FlightSearchArgs(BaseModel):
    """模型可填写的航班搜索参数；不含任何凭证。"""

    model_config = ConfigDict(frozen=True)

    departure_city_code: str = Field(..., description="出发城市三字码，如 BJS")
    arrival_city_code: str = Field(..., description="到达城市三字码，如 SHA")
    date: Date = Field(..., description="出发日期")
    adult_num: int = Field(default=1, ge=1, le=9, description="成人人数")
    cabin_preference: Optional[str] = Field(default=None, description="舱位偏好 Y/S/C/F，也接受中文舱位名")

    @field_validator("departure_city_code", "arrival_city_code")
    @classmethod
    def _city(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError("城市代码必须是 3 位字母")
        return value

    @field_validator("cabin_preference")
    @classmethod
    def _cabin(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else cabin_code(value)


class PlannedCall(BaseModel):
    """模型/提示词层可见的请求：认证头只含占位符，永远不含 Key。"""

    model_config = ConfigDict(frozen=True)

    url: str = Field(..., description="目标端点")
    headers: dict[str, str] = Field(..., description="请求头；Authorization 值是 ${FLIGHT_API_KEY} 占位符")
    body: dict[str, Any] = Field(..., description="JSON-RPC 请求体")


def build_search_call(args: FlightSearchArgs) -> PlannedCall:
    """按 SKILL.md 的 ``tools/call search_flights`` 约定生成带占位符的请求。"""
    arguments: dict[str, Any] = {
        "departureCityCode": args.departure_city_code, "arrivalCityCode": args.arrival_city_code,
        "date": args.date.isoformat(), "adultNum": args.adult_num,
    }
    if args.cabin_preference:
        arguments["cabinPreference"] = args.cabin_preference
    return PlannedCall(
        url=ENDPOINT,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {PLACEHOLDER}"},
        body={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_flights", "arguments": arguments}},
    )


class FlightOffer(BaseModel):
    """转换后的航班报价，可安全返回给模型。"""

    model_config = ConfigDict(frozen=True)

    flight_no: str = Field(..., description="规范化航班号，如 CA1501")
    departure_time: datetime = Field(..., description="起飞时间（供应商本地时间）")
    arrival_time: datetime = Field(..., description="到达时间（供应商本地时间）")
    cabin: str = Field(..., description="舱位代码 Y/S/C/F")
    cabin_name: str = Field(..., description="舱位中文名")
    price: Decimal = Field(..., description="价格，元，保留两位小数")


def parse_offer(raw: dict[str, Any]) -> FlightOffer:
    """把假供应商字段转换成内部结构；到达不晚于起飞视为非法。"""
    departure, arrival = parse_flight_time(raw["departureTime"]), parse_flight_time(raw["arrivalTime"])
    if arrival <= departure:
        raise FlightExerciseError(INVALID_TIME, "到达时间不晚于起飞时间")
    cabin = cabin_code(raw["cabin"])
    return FlightOffer(
        flight_no=normalize_flight_no(raw["flightNo"]), departure_time=departure, arrival_time=arrival,
        cabin=cabin, cabin_name=_CABINS[cabin], price=parse_price(raw["price"]),
    )


class FlightKeyInjector:
    """对应 Java ``FlightApiKeyHook``：在发出请求的最后一刻，用当前用户自己的 Key 替换占位符。"""

    def __init__(self, api_keys: ApiKeyService):
        self._api_keys = api_keys

    def send(self, call: PlannedCall, client: httpx.Client) -> httpx.Response:
        """校验目标与占位符位置、注入当前用户的 Key 并发送；返回值不含 Key。"""
        url = httpx.URL(call.url)
        if url.scheme != "https" or url.host != _HOST:
            raise FlightExerciseError(TARGET_NOT_ALLOWED, "只允许向航班管家 HTTPS 端点注入 Key")
        if PLACEHOLDER in call.url or PLACEHOLDER in str(call.body):
            raise FlightExerciseError(PLACEHOLDER_MISPLACED, "Key 占位符只能出现在请求头里")
        user_id = _current_user.get()
        if user_id is None:
            raise FlightExerciseError(NO_USER_SCOPE, "没有可信用户上下文，不能注入 Key")
        secret = self._api_keys.resolve_for_use(user_id, PROVIDER).get_secret_value()
        headers = {name: value.replace(PLACEHOLDER, secret) for name, value in call.headers.items()}
        return client.post(call.url, headers=headers, json=call.body)
