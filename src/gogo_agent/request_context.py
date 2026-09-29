"""021 服务端可信请求上下文；身份和状态引用不从模型消息解析。"""

from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


_NonEmptyId = Annotated[str, StringConstraints(strict=True, min_length=1)]


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
