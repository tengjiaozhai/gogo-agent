"""046 目的地实时联网查询：Python 客户端、返回 schema 与给模型看的文本渲染。

对照 Java `DestinationLiveTools`：
- `query_weather` 调 wttr.in（免费，无 Key），只支持今天起 3 天（`WEATHER_FORECAST_DAYS = 2`），
  超出时不发请求，只返回 `beyond_range=true`，由模型改用天气 MCP（见 `weather_mcp.py`）；
- `query_destination_news` 调 NewsData.io，未配置 Key 时降级为 `available=false`。

与 Java 的差别：
- Java 返回自由结构 JSON，失败时 `detail` 直接带异常消息（新闻 URL 含 apikey，可能泄露）。这里统一为
  `LiveQueryResult`，失败只给错误类型名，不回显异常文本。
- 每条数据带来源、抓取时间和有效期。**有效期是 Python 新增的假设值**（天气 3 小时、资讯 6 小时），Java 没有这个字段。
- 传输层是可注入的异步可调用对象；本模块不含真实 HTTP 传输，真实 wttr.in/NewsData.io 未验证。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FREE_WEATHER_FORECAST_DAYS = 2
WEATHER_VALID_HOURS = 3
NEWS_VALID_HOURS = 6
NEWS_TOPICS = frozenset({"general", "traffic", "event", "safety", "flight", "hotel", "policy"})
_CST = timezone(timedelta(hours=8))
_WEATHER_SOURCE = "wttr.in"
_NEWS_SOURCE = "newsdata.io"

LiveTransport = Callable[[str, Mapping[str, str]], Awaitable[Mapping[str, Any]]]
"""传输层：参数为 (kind, params)，返回对应服务的原始 JSON 对象；失败时抛异常。"""


class LiveStatus(StrEnum):
    """实时查询状态。"""

    OK = "ok"
    TIMEOUT = "timeout"
    NO_RESULT = "no_result"
    UNAVAILABLE = "unavailable"


class LiveQueryKind(StrEnum):
    """实时查询类型。"""

    WEATHER = "weather"
    NEWS = "news"


class LiveUnavailableError(Exception):
    """传输层表示服务不可用（如未配置 Key、网络不可达）；消息只写给日志，不会进入模型文本。"""


class LiveDataItem(BaseModel):
    """一条查到的数据：内容加来源、抓取时间和有效期。"""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., description="数据标题，如“实况”“2026-10-12 预报”或资讯标题")
    text: str = Field(..., description="数据内容的中文描述")
    source: str = Field(..., description="数据来源标识，如 wttr.in、newsdata.io")
    fetched_at: datetime = Field(..., description="抓取 UTC 时间")
    valid_until: datetime = Field(..., description="建议有效期截止 UTC 时间，过期后须重新查询")
    published_at: str | None = Field(default=None, description="来源给出的发布时间文本，无则为空")


class LiveQueryResult(BaseModel):
    """一次实时查询的结果；失败或无结果时 items 为空，不含任何看似确定的事实。"""

    model_config = ConfigDict(extra="forbid")

    kind: LiveQueryKind = Field(..., description="查询类型：weather 或 news")
    status: LiveStatus = Field(..., description="状态：ok、timeout、no_result 或 unavailable")
    city: str = Field(..., description="查询城市")
    items: list[LiveDataItem] = Field(default_factory=list, description="查到的数据；仅 status=ok 时非空")
    beyond_range: bool = Field(
        default=False, description="日期超出免费天气窗口（今天起 3 天）；此时未发请求，应改用长期预报",
    )
    message: str | None = Field(default=None, description="给调用方的补充说明，如失败的错误类型名；不含密钥或异常原文")


def is_beyond_free_window(target_date: date, today: date) -> bool:
    """目标日期晚于今天 + 2 天即超出免费天气窗口；这是 Java 与 047 共用的路由边界。"""
    return target_date > today + timedelta(days=FREE_WEATHER_FORECAST_DAYS)


class DestinationLiveClient:
    """目的地实时查询客户端：设置超时，把传输层异常映射为明确状态。"""

    def __init__(
        self,
        transport: LiveTransport,
        *,
        weather_timeout_s: float = 8.0,
        news_timeout_s: float = 10.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """超时默认值沿用 Java 的 8 秒（天气）与 10 秒（资讯）；`now` 仅供测试固定时钟。"""
        self._transport = transport
        self._timeouts = {LiveQueryKind.WEATHER: weather_timeout_s, LiveQueryKind.NEWS: news_timeout_s}
        self._now = now or (lambda: datetime.now(UTC))

    async def query_weather(self, city: str, target_date: date | None = None) -> LiveQueryResult:
        """查询天气；target_date 超出免费窗口时不发请求，返回 beyond_range。"""
        city = _clean_city(city)
        today = self._now().astimezone(_CST).date()
        if target_date is not None and is_beyond_free_window(target_date, today):
            return LiveQueryResult(
                kind=LiveQueryKind.WEATHER, status=LiveStatus.NO_RESULT, city=city, beyond_range=True,
                message=f"{target_date.isoformat()} 超出免费天气范围（今天起 {FREE_WEATHER_FORECAST_DAYS + 1} 天内）",
            )
        raw, failure = await self._fetch(LiveQueryKind.WEATHER, city, {"city": city})
        if failure is not None:
            return failure
        fetched_at = self._now()
        items = _parse_weather(raw, target_date, today, fetched_at)
        return _finish(LiveQueryKind.WEATHER, city, items, "服务返回的天气数据为空")

    async def query_news(self, city: str, topic: str = "general") -> LiveQueryResult:
        """查询目的地资讯；topic 必须是 Java 工具描述里列出的七个主题之一。"""
        city = _clean_city(city)
        if topic not in NEWS_TOPICS:
            raise ValueError(f"不支持的资讯主题：{topic}")
        raw, failure = await self._fetch(LiveQueryKind.NEWS, city, {"city": city, "topic": topic})
        if failure is not None:
            return failure
        items = _parse_news(raw, self._now())
        return _finish(LiveQueryKind.NEWS, city, items, "服务没有检索到相关资讯")

    async def _fetch(
        self, kind: LiveQueryKind, city: str, params: Mapping[str, str],
    ) -> tuple[Mapping[str, Any], LiveQueryResult | None]:
        """调用传输层；超时、不可用或响应不是对象时返回失败结果。"""
        try:
            async with asyncio.timeout(self._timeouts[kind]):
                raw = await self._transport(kind.value, params)
        except TimeoutError:
            return {}, LiveQueryResult(
                kind=kind, status=LiveStatus.TIMEOUT, city=city,
                message=f"超过 {self._timeouts[kind]:g} 秒未返回",
            )
        except Exception as exc:  # noqa: BLE001 - 任何传输失败都映射为不可用，且只暴露类型名
            return {}, LiveQueryResult(
                kind=kind, status=LiveStatus.UNAVAILABLE, city=city, message=f"服务不可用（{type(exc).__name__}）",
            )
        if not isinstance(raw, Mapping):
            return {}, LiveQueryResult(
                kind=kind, status=LiveStatus.UNAVAILABLE, city=city, message="服务响应格式异常",
            )
        return raw, None


def _clean_city(city: str) -> str:
    if not isinstance(city, str) or not city.strip():
        raise ValueError("城市不能为空")
    return city.strip()


def _finish(kind: LiveQueryKind, city: str, items: list[LiveDataItem], empty_message: str) -> LiveQueryResult:
    if not items:
        return LiveQueryResult(kind=kind, status=LiveStatus.NO_RESULT, city=city, message=empty_message)
    return LiveQueryResult(kind=kind, status=LiveStatus.OK, city=city, items=items)


def _first_desc(obj: Mapping[str, Any]) -> str | None:
    """wttr.in 的 weatherDesc 是 [{"value": "..."}]。"""
    desc = obj.get("weatherDesc")
    if isinstance(desc, list) and desc and isinstance(desc[0], Mapping):
        value = desc[0].get("value")
        return None if value is None else str(value)
    return None


def _parse_weather(
    raw: Mapping[str, Any], target_date: date | None, today: date, fetched_at: datetime,
) -> list[LiveDataItem]:
    """按 Java 规则：未指定日期或指定今天时含实况与全部预报；指定其他日期只含该日预报。"""
    valid_until = fetched_at + timedelta(hours=WEATHER_VALID_HOURS)
    items: list[LiveDataItem] = []

    def item(label: str, text: str) -> LiveDataItem:
        return LiveDataItem(
            label=label, text=text, source=_WEATHER_SOURCE, fetched_at=fetched_at, valid_until=valid_until,
        )

    include_all = target_date is None or target_date == today
    current = raw.get("current_condition")
    if include_all and isinstance(current, list) and current and isinstance(current[0], Mapping):
        cur = current[0]
        parts = [f"当前 {cur.get('temp_C')}°C", f"体感 {cur.get('FeelsLikeC')}°C",
                 f"湿度 {cur.get('humidity')}%", f"风速 {cur.get('windspeedKmph')} km/h"]
        desc = _first_desc(cur)
        items.append(item("实况", "，".join(parts) + (f"，{desc}" if desc else "")))

    forecast = raw.get("weather")
    for day in forecast if isinstance(forecast, list) else []:
        if not isinstance(day, Mapping):
            continue
        day_text = str(day.get("date", ""))
        if not include_all and day_text != target_date.isoformat():
            continue
        hourly = [slot for slot in day.get("hourly", []) if isinstance(slot, Mapping)] if isinstance(day.get("hourly"), list) else []
        noon = next((slot for slot in hourly if str(slot.get("time")) == "1200"), hourly[0] if hourly else None)
        text = f"最高 {day.get('maxtempC')}°C，最低 {day.get('mintempC')}°C"
        if noon is not None and _first_desc(noon):
            text += f"，白天{_first_desc(noon)}"
        rains = [int(slot["chanceofrain"]) for slot in hourly if str(slot.get("chanceofrain", "")).isdigit()]
        if rains:
            text += f"，最大降雨概率 {max(rains)}%"
        items.append(item(f"{day_text} 预报", text))
    return items


def _parse_news(raw: Mapping[str, Any], fetched_at: datetime) -> list[LiveDataItem]:
    """资讯只保留标题、摘要、发布时间与来源四项，与 Java 的投影一致。"""
    articles = raw.get("results")
    items: list[LiveDataItem] = []
    for article in articles if isinstance(articles, list) else []:
        if not isinstance(article, Mapping) or not article.get("title"):
            continue
        publisher = article.get("source_id")
        items.append(LiveDataItem(
            label=str(article["title"]),
            text=str(article.get("description") or "（无摘要）"),
            source=f"{_NEWS_SOURCE}/{publisher}" if publisher else _NEWS_SOURCE,
            fetched_at=fetched_at,
            valid_until=fetched_at + timedelta(hours=NEWS_VALID_HOURS),
            published_at=None if article.get("pubDate") is None else str(article["pubDate"]),
        ))
    return items


def _fmt(moment: datetime) -> str:
    return moment.astimezone(_CST).strftime("%Y-%m-%d %H:%M")


_KIND_CN = {LiveQueryKind.WEATHER: "天气", LiveQueryKind.NEWS: "出行资讯"}
_STATUS_CN = {
    LiveStatus.OK: "已查到", LiveStatus.TIMEOUT: "查询超时",
    LiveStatus.NO_RESULT: "没有结果", LiveStatus.UNAVAILABLE: "服务不可用",
}


def render_live_result(result: LiveQueryResult) -> str:
    """渲染给模型看的文本：“查到的数据”带来源与时间，“未验证信息”单独成段；失败时不输出任何看似确定的事实。"""
    kind_cn = _KIND_CN[result.kind]
    lines = [f"【目的地实时查询：{kind_cn} · {result.city}】"]
    if result.beyond_range:
        lines.append("状态：日期超出免费天气范围，本次未查询")
    else:
        lines.append(f"状态：{_STATUS_CN[result.status]}（{result.status.value}）")

    if result.items:
        lines.append("查到的数据（时间均为 UTC+8）：")
        for index, entry in enumerate(result.items, start=1):
            published = f"；发布：{entry.published_at}" if entry.published_at else ""
            lines.append(
                f"  {index}. {entry.label}：{entry.text}"
                f"（来源：{entry.source}；抓取：{_fmt(entry.fetched_at)}；有效至：{_fmt(entry.valid_until)}{published}）"
            )
        lines.append("未验证信息：")
        lines.append(f"  - 上面没有列出的{kind_cn}内容本次均未查到，不得当作已确认事实。")
        lines.append("  - 超过“有效至”时间后，以上数据只能作参考，需要重新查询。")
        return "\n".join(lines)

    lines.append("查到的数据：无。")
    lines.append("未验证信息：")
    if result.beyond_range:
        lines.append(f"  - {result.city}该日期的天气尚未查询，目前没有任何可引用的天气数据；不得描述该日期的天气。")
        lines.append("  - 下一步应改用长期天气预报能力查询；拿到结果前一律按“未验证”处理。")
    else:
        reason = f"原因：{result.message}。" if result.message else ""
        lines.append(f"  - {result.city}的{kind_cn}本次没有拿到可用数据。{reason}")
        lines.append("  - 任何具体数值、天气判断或事件结论都属于未验证信息，不得用确定语气陈述。")
        lines.append("  - 应如实告诉用户暂时查不到，并建议稍后重试或查看官方渠道。")
    return "\n".join(lines)


def simulated_transport(mode: Literal["normal", "timeout", "no_result", "unavailable"]) -> LiveTransport:
    """模拟传输层：normal 返回固定样例，timeout 一直挂起，no_result 返回空结构，unavailable 抛异常。"""

    async def transport(kind: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        if mode == "timeout":
            await asyncio.sleep(3600)
        if mode == "unavailable":
            raise LiveUnavailableError("simulated: https://example.invalid/?apikey=SECRET-IN-URL")
        if mode == "no_result":
            return {"results": []} if kind == "news" else {}
        if kind == "news":
            return _SIMULATED_NEWS
        return _SIMULATED_WEATHER

    return transport


_SIMULATED_WEATHER: dict[str, Any] = {
    "current_condition": [{
        "temp_C": "21", "FeelsLikeC": "20", "humidity": "63", "windspeedKmph": "11",
        "weatherDesc": [{"value": "多云"}],
    }],
    "weather": [
        {
            "date": "2026-10-11", "maxtempC": "24", "mintempC": "17",
            "hourly": [
                {"time": "0", "tempC": "18", "windspeedKmph": "8", "chanceofrain": "5", "weatherDesc": [{"value": "晴"}]},
                {"time": "1200", "tempC": "24", "windspeedKmph": "12", "chanceofrain": "10", "weatherDesc": [{"value": "多云"}]},
            ],
        },
        {
            "date": "2026-10-12", "maxtempC": "22", "mintempC": "16",
            "hourly": [
                {"time": "1200", "tempC": "22", "windspeedKmph": "15", "chanceofrain": "70", "weatherDesc": [{"value": "小雨"}]},
            ],
        },
        {
            "date": "2026-10-13", "maxtempC": "20", "mintempC": "15",
            "hourly": [
                {"time": "1200", "tempC": "20", "windspeedKmph": "9", "chanceofrain": "30", "weatherDesc": [{"value": "阴"}]},
            ],
        },
    ],
}

_SIMULATED_NEWS: dict[str, Any] = {
    "results": [
        {"title": "市区部分路段周末临时管制", "description": "周六 8:00 至 14:00 举办马拉松，沿线限行。",
         "pubDate": "2026-10-10 09:30:00", "source_id": "city_daily"},
        {"title": "会展中心举办行业大会", "description": None, "pubDate": "2026-10-09 18:00:00", "source_id": None},
    ],
}
