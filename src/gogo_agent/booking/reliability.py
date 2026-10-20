"""066/067 工具可靠性：只读重试、写入不重试、有界并行搜索，以及工具级熔断。

Java 基线（BaseSubAgent.getToolConfig）：最多 3 次尝试（含首次）、初始退避 3 秒、按 2 倍增长、
仅对 IOException 重试。Python 沿用同样的次数与退避，并把“重试”限定在只读调用上。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_INITIAL_BACKOFF_S = 3.0
DEFAULT_BACKOFF_MULTIPLIER = 2.0
DEFAULT_SEARCH_CONCURRENCY = 4
DEFAULT_SEARCH_TIMEOUT_S = 60.0
BREAKER_FAILURE_THRESHOLD = 3
BREAKER_COOLDOWN_S = 60.0
MONITORED_TOOLS = frozenset({"query_weather", "query_destination_news"})


def is_retryable(exc: BaseException) -> bool:
    """只把 IO 类瞬时错误视为可重试，对应 Java 的 `retryOn(IOException)`。"""
    return isinstance(exc, OSError)


@dataclass(frozen=True)
class RetryPolicy:
    """只读调用的重试策略；max_attempts 含首次调用。"""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    initial_backoff_s: float = DEFAULT_INITIAL_BACKOFF_S
    multiplier: float = DEFAULT_BACKOFF_MULTIPLIER


def call_read_with_retry(
    fn: Callable[[], T], policy: RetryPolicy = RetryPolicy(), *, sleep: Callable[[float], None] = time.sleep,
) -> T:
    """同步只读调用的重试；非瞬时错误立即抛出，不做任何外部重复调用。"""
    backoff = policy.initial_backoff_s
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except Exception as exc:
            if attempt >= policy.max_attempts or not is_retryable(exc):
                raise
            sleep(backoff)
            backoff *= policy.multiplier


async def async_call_read_with_retry(
    fn: Callable[[], Awaitable[T]], policy: RetryPolicy = RetryPolicy(),
    *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """异步只读调用的重试，规则与同步版本一致。"""
    backoff = policy.initial_backoff_s
    attempt = 0
    while True:
        attempt += 1
        try:
            return await fn()
        except Exception as exc:
            if attempt >= policy.max_attempts or not is_retryable(exc):
                raise
            await sleep(backoff)
            backoff *= policy.multiplier


class WriteOutcome(StrEnum):
    """写入调用的结果分类：未知结果不能被当作失败后重试。"""

    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class SearchOutcome(BaseModel):
    """单个供应商搜索的结果；失败只带异常类型，不带供应商原文。"""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    supplier: str = Field(..., description="供应商名称")
    user_id: str = Field(..., description="发起搜索的认证用户标识，用于验证并发不串用户")
    ok: bool = Field(..., description="搜索是否成功返回")
    items: list[Any] = Field(default_factory=list, description="成功时的候选列表")
    error_type: str | None = Field(default=None, description="失败时的异常类型名")


async def search_in_parallel(
    user_id: str,
    searches: Mapping[str, Callable[[str], Awaitable[list[Any]]]],
    *,
    concurrency: int = DEFAULT_SEARCH_CONCURRENCY,
    timeout_s: float = DEFAULT_SEARCH_TIMEOUT_S,
) -> dict[str, SearchOutcome]:
    """并发执行只读搜索：并发数与单次超时有界，单个供应商失败不影响其他结果。

    每个搜索函数只接收 user_id 参数，结果对象携带同一个 user_id，便于验证并发时不串用户。
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def run_one(name: str, search: Callable[[str], Awaitable[list[Any]]]) -> SearchOutcome:
        async with semaphore:
            try:
                async with asyncio.timeout(timeout_s):
                    items = await search(user_id)
                return SearchOutcome(supplier=name, user_id=user_id, ok=True, items=list(items))
            except TimeoutError:
                return SearchOutcome(supplier=name, user_id=user_id, ok=False, error_type="TimeoutError")
            except Exception as exc:  # 只保留类型名，避免把供应商错误文本带入上下文
                return SearchOutcome(supplier=name, user_id=user_id, ok=False, error_type=type(exc).__name__)

    results = await asyncio.gather(*(run_one(name, fn) for name, fn in searches.items()))
    return {item.supplier: item for item in results}


class CircuitState(StrEnum):
    """熔断状态：关闭时正常调用，打开时拒绝调用，半开时只放行一次探测。"""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(RuntimeError):
    """熔断打开时拒绝外呼，不向外部服务发送请求。"""

    code = "CIRCUIT_OPEN"

    def __init__(self, tool_name: str) -> None:
        super().__init__(f"{tool_name} 连续失败，暂停外呼，等待冷却后探测")
        self.tool_name = tool_name


@dataclass
class CircuitBreaker:
    """单个工具的熔断器；时钟可注入，便于测试冷却时间。"""

    name: str
    failure_threshold: int = BREAKER_FAILURE_THRESHOLD
    cooldown_s: float = BREAKER_COOLDOWN_S
    clock: Callable[[], float] = time.monotonic
    state: CircuitState = CircuitState.CLOSED
    failures: int = 0
    opened_at: float = 0.0
    probe_in_flight: bool = False

    def before_call(self) -> None:
        """调用前检查状态；打开且未冷却时拒绝，冷却后进入半开并只放行一次探测。"""
        if self.state is CircuitState.OPEN:
            if self.clock() - self.opened_at < self.cooldown_s:
                raise CircuitOpenError(self.name)
            self.state = CircuitState.HALF_OPEN
            self.probe_in_flight = False
        if self.state is CircuitState.HALF_OPEN:
            if self.probe_in_flight:
                raise CircuitOpenError(self.name)
            self.probe_in_flight = True

    def on_success(self) -> None:
        """成功即关闭熔断并清空失败计数。"""
        self.state = CircuitState.CLOSED
        self.failures = 0
        self.probe_in_flight = False

    def on_failure(self) -> None:
        """半开探测失败立即重新打开；关闭状态下累计到阈值才打开。"""
        self.probe_in_flight = False
        if self.state is CircuitState.HALF_OPEN:
            self._open()
            return
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self._open()

    def _open(self) -> None:
        self.state = CircuitState.OPEN
        self.opened_at = self.clock()


@dataclass
class ToolBreakerRegistry:
    """只对被监控的工具名套用熔断；其他工具（如纯函数、数据库工具）不受影响。"""

    failure_threshold: int = BREAKER_FAILURE_THRESHOLD
    cooldown_s: float = BREAKER_COOLDOWN_S
    clock: Callable[[], float] = time.monotonic
    monitored: frozenset[str] = MONITORED_TOOLS
    _breakers: dict[str, CircuitBreaker] = field(default_factory=dict)

    def breaker(self, tool_name: str) -> CircuitBreaker:
        if tool_name not in self._breakers:
            self._breakers[tool_name] = CircuitBreaker(
                name=tool_name, failure_threshold=self.failure_threshold,
                cooldown_s=self.cooldown_s, clock=self.clock,
            )
        return self._breakers[tool_name]

    async def call(self, tool_name: str, fn: Callable[[], Awaitable[T]]) -> T:
        """未监控工具直接调用；监控工具先经熔断检查，成功或失败都更新状态。"""
        if tool_name not in self.monitored:
            return await fn()
        breaker = self.breaker(tool_name)
        breaker.before_call()
        try:
            result = await fn()
        except Exception:
            breaker.on_failure()
            raise
        except BaseException:
            breaker.probe_in_flight = False  # 被取消不计为失败，但必须释放半开探测位
            raise
        breaker.on_success()
        return result
