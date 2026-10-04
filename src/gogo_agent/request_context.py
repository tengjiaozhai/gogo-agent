"""服务端可信请求参数与日志追踪作用域；身份不从模型消息解析。"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


_NonEmptyId = Annotated[str, StringConstraints(strict=True, min_length=1)]
_trace_id: ContextVar[str | None] = ContextVar("gogo_trace_id", default=None)


class RequestContext(BaseModel):
    """认证入口创建的不可变请求身份、会话和追踪数据，只供应用与工具使用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: _NonEmptyId = Field(..., description="登录令牌解析出的可信用户标识，不能由模型覆盖")
    session_id: _NonEmptyId = Field(..., description="已校验归属的业务会话标识，不能由模型覆盖")
    request_id: _NonEmptyId = Field(..., description="本轮已保存用户消息的标识，用于追踪与同请求去重")
    trace_id: _NonEmptyId = Field(
        default_factory=lambda: uuid4().hex,
        description="服务端生成的一次请求追踪标识，贯穿预处理与子步骤",
    )
    plan_reference: _NonEmptyId | None = Field(
        default=None,
        description="服务端验证后传递的计划引用；尚无计划时为空，不采信模型自报值",
    )


def current_trace_id() -> str | None:
    """只供日志读取当前异步任务的追踪号，不提供身份或授权数据。"""
    return _trace_id.get()


@contextmanager
def trace_scope(request: RequestContext) -> Iterator[None]:
    """在请求执行期间绑定追踪号，退出、异常和取消时恢复原值。"""
    token = _trace_id.set(request.trace_id)
    try:
        yield
    finally:
        _trace_id.reset(token)
