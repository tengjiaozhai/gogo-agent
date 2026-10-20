"""047 天气 MCP 接入：用 AgentScope 2.0.8 的 `MCPClient` 封装连接生命周期、工具发现、调用与错误转换。

对照 Java：
- `WeatherMcpConfig` 用 `McpClientBuilder` 建 Streamable HTTP 客户端（超时 30 秒、握手 15 秒），失败时返回 null 降级；
- `app.weather-mcp.enabled-tools` 白名单六个工具（城市天气实况、15 日预报、天气预警、空气实况、24 小时预报、40 日预报）；
- “短期免费 vs 长期 MCP”的边界由 `query_weather` 返回 `beyond_range=true` 后**由模型自行改调 MCP 工具**，
  Java 没有代码层强制；Python 保持同一约定：路由判断在 `destination_live.is_beyond_free_window`，
  本服务不强制日期窗口，只校验日期范围合法（起止顺序、最长 40 天，对应 Java 最长的 40 日预报）。

隐私：`get_forecast` 的签名只有城市与起止日期，不接受用户标识、会话或差旅事由；
实际发出的参数还会按服务端工具 schema 过滤，schema 没声明的名字不发送。

生命周期：连接与关闭由一个专用 asyncio 任务完成（`MCPClient` 内部的 anyio 作用域要求 connect/close 同任务），
因此一个 `WeatherMcpService` 可以被多个任务调用；请用 `async with WeatherMcpService(...)` 保证 close。
已知的 AgentScope 2.0.8 缺陷（框架行为，本仓库不改框架）：握手被取消（超时）或 stdio 服务在握手阶段直接退出时，
`MCPClient.connect()` 的清理在另一个任务里执行，会留下仍在持续取消宿主任务的 anyio 作用域、一条未处理异常日志，
并且**可能遗留不响应 stdin 关闭的 stdio 子进程**。专用任务把前两者隔离在调用方之外，并转换为
MCP_TIMEOUT / MCP_UNAVAILABLE；遗留子进程无法在本服务内可靠清理，部署侧需要进程监督。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime
from enum import StrEnum
from types import TracebackType
from typing import Any

import anyio
from agentscope.mcp import HttpMCPConfig, MCPClient, StdioMCPConfig
from agentscope.message import ToolResultState
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

DEFAULT_ENABLED_TOOLS = (
    "城市天气实况", "城市15日预报", "城市天气预警", "城市空气实况", "城市24小时预报", "城市40日预报",
)
MAX_RANGE_DAYS = 40
_MAX_CITY_CHARS = 40
_REQUEST_TIMEOUT_CODE = 408
_ARGUMENT_NAMES = ("city", "start_date", "end_date")


class WeatherMcpErrorCode(StrEnum):
    """天气 MCP 调用失败的稳定错误码。"""

    MCP_UNAVAILABLE = "MCP_UNAVAILABLE"
    MCP_TIMEOUT = "MCP_TIMEOUT"
    MCP_NOT_CONNECTED = "MCP_NOT_CONNECTED"
    MCP_TOOL_NOT_ALLOWED = "MCP_TOOL_NOT_ALLOWED"
    MCP_SCHEMA_MISMATCH = "MCP_SCHEMA_MISMATCH"
    MCP_TOOL_ERROR = "MCP_TOOL_ERROR"
    MCP_PROTOCOL_ERROR = "MCP_PROTOCOL_ERROR"
    MCP_INVALID_RESPONSE = "MCP_INVALID_RESPONSE"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"


class WeatherMcpError(Exception):
    """天气 MCP 异常；消息不包含端点 URL（Java 的端点内嵌 AppCode）或请求参数。"""

    def __init__(self, code: WeatherMcpErrorCode, message: str) -> None:
        super().__init__(f"[{code.value}] {message}")
        self.code = code
        self.message = message


class WeatherToolInfo(BaseModel):
    """服务端声明的一个天气工具及其参数 schema。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="MCP 服务端的原始工具名")
    description: str = Field(default="", description="工具说明")
    input_schema: dict[str, Any] = Field(default_factory=dict, description="工具入参的 JSON Schema，原样记录")


class WeatherForecast(BaseModel):
    """一次预报调用的结果；服务端返回体结构未在源项目中固定，因此 payload 原样保留。"""

    model_config = ConfigDict(extra="forbid")

    city: str = Field(..., description="查询城市")
    start_date: date = Field(..., description="预报起始日期")
    end_date: date = Field(..., description="预报结束日期")
    tool_name: str = Field(..., description="实际调用的 MCP 工具名")
    sent_arguments: list[str] = Field(..., description="实际发送给服务端的参数名，用于核对未发送多余字段")
    payload: dict[str, Any] = Field(..., description="服务端返回的 JSON 对象，未做字段映射")
    fetched_at: datetime = Field(..., description="收到响应的 UTC 时间")


class WeatherMcpService:
    """天气 MCP 服务：`connect/list_tools/get_forecast/close`，也可作为异步上下文管理器使用。"""

    def __init__(
        self,
        config: StdioMCPConfig | HttpMCPConfig,
        *,
        name: str = "weather-mcp",
        enabled_tools: Sequence[str] | None = None,
        forecast_tool: str = "forecast",
        connect_timeout_s: float = 15.0,
        call_timeout_s: float = 30.0,
    ) -> None:
        """超时默认值沿用 Java（握手 15 秒，调用 30 秒）；`enabled_tools` 为空表示不过滤，对应 Java 的白名单配置。"""
        self._client = MCPClient(
            name=name, is_stateful=True, mcp_config=config,
            enable_tools=list(enabled_tools) if enabled_tools is not None else None,
        )
        self._name = name
        self._forecast_tool = forecast_tool
        self._connect_timeout_s = connect_timeout_s
        self._call_timeout_s = call_timeout_s
        self._tools: dict[str, WeatherToolInfo] | None = None
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._closing = asyncio.Event()

    @property
    def is_connected(self) -> bool:
        """当前是否持有 MCP 连接。"""
        return self._task is not None and self._ready.is_set() and self._client.is_connected

    async def __aenter__(self) -> WeatherMcpService:
        await self.connect()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def connect(self) -> None:
        """建立连接并完成握手；超时为 MCP_TIMEOUT，其余启动失败为 MCP_UNAVAILABLE；已连接时什么也不做。"""
        if self._task is not None:
            return
        self._ready = asyncio.Event()
        self._closing = asyncio.Event()
        self._task = asyncio.create_task(self._lifecycle(), name=f"{self._name}-lifecycle")
        waiter = asyncio.create_task(self._ready.wait())
        try:
            done, _ = await asyncio.wait(
                {self._task, waiter}, timeout=self._connect_timeout_s, return_when=asyncio.FIRST_COMPLETED,
            )
        except BaseException:
            await self._abort_lifecycle()
            raise
        finally:
            waiter.cancel()
        if self._ready.is_set():
            self._tools = None
            return
        if self._task not in done:
            await self._abort_lifecycle()
            raise WeatherMcpError(
                WeatherMcpErrorCode.MCP_TIMEOUT, f"{self._name} 握手超过 {self._connect_timeout_s:g} 秒",
            )
        failure = None if self._task.cancelled() else self._task.exception()
        self._task = None
        if failure is None:  # 生命周期任务被 AgentScope 内部的 anyio 作用域取消，对应服务启动即退出
            raise WeatherMcpError(WeatherMcpErrorCode.MCP_UNAVAILABLE, f"{self._name} 在握手阶段退出")
        raise WeatherMcpError(
            WeatherMcpErrorCode.MCP_UNAVAILABLE, f"{self._name} 无法启动或连接（{type(failure).__name__}）",
        )

    async def _lifecycle(self) -> None:
        """在专用任务里完成 connect 与 close：MCPClient 内部的 anyio 作用域要求二者同任务，
        且握手失败时的取消污染只会留在这个任务里，不会波及调用方。"""
        await self._client.connect()
        try:
            self._ready.set()
            await self._closing.wait()
        finally:
            await self._client.close(ignore_errors=True)

    async def _abort_lifecycle(self) -> None:
        task, self._task = self._task, None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def close(self) -> None:
        """关闭连接；未连接时什么也不做，保证可在 finally 或 `async with` 退出时调用。"""
        task, self._task = self._task, None
        if task is None:
            return
        self._closing.set()
        await asyncio.gather(task, return_exceptions=True)
        self._tools = None

    async def list_tools(self) -> list[WeatherToolInfo]:
        """向服务端发现工具（已应用白名单），并记录名称、说明与参数 schema。"""
        raw_tools = await self._guarded(self._client.list_raw_tools)
        self._tools = {
            tool.name: WeatherToolInfo(
                name=tool.name, description=tool.description or "", input_schema=dict(tool.inputSchema or {}),
            )
            for tool in raw_tools
        }
        return list(self._tools.values())

    async def get_forecast(self, city: str, start_date: date, end_date: date) -> WeatherForecast:
        """查询城市在日期范围内的预报；只向服务端发送城市与日期范围。"""
        city = _validate_request(city, start_date, end_date)
        if self._tools is None:
            await self.list_tools()
        tool_info = self._tools.get(self._forecast_tool)
        if tool_info is None:
            raise WeatherMcpError(
                WeatherMcpErrorCode.MCP_TOOL_NOT_ALLOWED, f"工具 {self._forecast_tool} 不存在或不在白名单内",
            )
        values = {"city": city, "start_date": start_date.isoformat(), "end_date": end_date.isoformat()}
        arguments = _arguments_for_schema(values, tool_info.input_schema)

        tool = await self._guarded(self._client.get_tool, self._forecast_tool)
        chunk = await self._guarded(tool.call, **arguments)
        text = "".join(block.text for block in chunk.content if getattr(block, "type", None) == "text")
        if chunk.state == ToolResultState.ERROR:
            raise WeatherMcpError(WeatherMcpErrorCode.MCP_TOOL_ERROR, f"天气工具返回错误：{text[:200]}")
        try:
            payload = json.loads(text)
        except ValueError:
            raise WeatherMcpError(WeatherMcpErrorCode.MCP_INVALID_RESPONSE, "天气工具返回的不是 JSON") from None
        if not isinstance(payload, dict):
            raise WeatherMcpError(WeatherMcpErrorCode.MCP_INVALID_RESPONSE, "天气工具返回的 JSON 不是对象")
        return WeatherForecast(
            city=city, start_date=start_date, end_date=end_date, tool_name=self._forecast_tool,
            sent_arguments=sorted(arguments), payload=payload, fetched_at=datetime.now(UTC),
        )

    async def _guarded(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """给一次 MCP 往返加超时，并把连接状态与协议异常转换为稳定错误码。"""
        if not self._client.is_connected:
            raise WeatherMcpError(WeatherMcpErrorCode.MCP_NOT_CONNECTED, "尚未连接，请先 connect()")
        try:
            async with asyncio.timeout(self._call_timeout_s):
                return await func(*args, **kwargs)
        except TimeoutError:
            raise WeatherMcpError(
                WeatherMcpErrorCode.MCP_TIMEOUT, f"{self._name} 超过 {self._call_timeout_s:g} 秒未响应",
            ) from None
        except McpError as exc:
            code = exc.error.code
            if code == CONNECTION_CLOSED:
                raise WeatherMcpError(WeatherMcpErrorCode.MCP_UNAVAILABLE, f"{self._name} 连接已断开") from None
            if code == _REQUEST_TIMEOUT_CODE:
                raise WeatherMcpError(WeatherMcpErrorCode.MCP_TIMEOUT, f"{self._name} 请求超时") from None
            raise WeatherMcpError(
                WeatherMcpErrorCode.MCP_PROTOCOL_ERROR, f"{self._name} 协议错误（code={code}）：{exc.error.message[:200]}",
            ) from None
        except (BrokenPipeError, ConnectionError, EOFError, anyio.ClosedResourceError, anyio.BrokenResourceError) as exc:
            raise WeatherMcpError(
                WeatherMcpErrorCode.MCP_UNAVAILABLE, f"{self._name} 连接已断开（{type(exc).__name__}）",
            ) from None


def _validate_request(city: str, start_date: date, end_date: date) -> str:
    if not isinstance(city, str) or not city.strip() or len(city.strip()) > _MAX_CITY_CHARS:
        raise WeatherMcpError(WeatherMcpErrorCode.INVALID_ARGUMENT, f"城市必须是 1 到 {_MAX_CITY_CHARS} 个字符")
    if end_date < start_date:
        raise WeatherMcpError(WeatherMcpErrorCode.INVALID_ARGUMENT, "结束日期不能早于起始日期")
    if (end_date - start_date).days + 1 > MAX_RANGE_DAYS:
        raise WeatherMcpError(WeatherMcpErrorCode.INVALID_ARGUMENT, f"日期范围不能超过 {MAX_RANGE_DAYS} 天")
    return city.strip()


def _arguments_for_schema(values: dict[str, str], schema: dict[str, Any]) -> dict[str, str]:
    """只发送服务端 schema 声明过的参数；缺 city 或有必填项无法提供时报 schema 不匹配。"""
    declared = set(schema.get("properties", {}))
    arguments = {name: values[name] for name in _ARGUMENT_NAMES if name in declared}
    missing = (set(schema.get("required", [])) | {"city"}) - set(arguments)
    if missing:
        raise WeatherMcpError(
            WeatherMcpErrorCode.MCP_SCHEMA_MISMATCH, f"服务端工具需要本客户端不会提供的参数：{sorted(missing)}",
        )
    return arguments
