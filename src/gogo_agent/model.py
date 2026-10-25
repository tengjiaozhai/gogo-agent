"""026 共用模型构造与单次调用观测。"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any, AsyncGenerator

from agentscope.credential import OpenAICredential
from agentscope.model import ChatResponse, OpenAIChatModel
from pydantic import BaseModel


logger = logging.getLogger("uvicorn.error")


class ObservedOpenAIChatModel(OpenAIChatModel):
    """在 AgentScope 模型调用完成时记录角色、模型、耗时和可用 token。"""

    def __init__(self, *, role: str, **kwargs: Any) -> None:
        self.role = role
        super().__init__(**kwargs)

    def _record(self, started: float, response: ChatResponse | None, status: str) -> None:
        usage = response.usage if response is not None else None
        logger.info(
            "model_call role=%s model=%r duration_ms=%.1f input_tokens=%s output_tokens=%s cached_input_tokens=%s status=%s",
            self.role,
            self.model,
            (perf_counter() - started) * 1000,
            usage.input_tokens if usage is not None else "unavailable",
            usage.output_tokens if usage is not None else "unavailable",
            usage.cache_input_tokens if usage is not None else "unavailable",
            status,
        )

    async def __call__(self, *args: Any, **kwargs: Any) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        started = perf_counter()
        try:
            result = await super().__call__(*args, **kwargs)
        except BaseException as exc:
            self._record(started, None, type(exc).__name__)
            raise
        if isinstance(result, ChatResponse):
            self._record(started, result, str(result.finished_reason))
            return result

        async def observed_stream() -> AsyncGenerator[ChatResponse, None]:
            final: ChatResponse | None = None
            status = "incomplete"
            try:
                async for chunk in result:
                    if chunk.is_last:
                        final = chunk
                        status = str(chunk.finished_reason)
                    yield chunk
            except BaseException as exc:
                status = type(exc).__name__
                raise
            finally:
                self._record(started, final, status)

        return observed_stream()


def create_chat_model(
    credential: OpenAICredential,
    model_name: str,
    *,
    role: str,
    stream: bool,
    timeout_seconds: float,
    parameters: OpenAIChatModel.Parameters | None = None,
) -> ObservedOpenAIChatModel:
    """统一网关模型的重试、超时与观测配置。"""
    return ObservedOpenAIChatModel(
        role=role,
        credential=credential,
        model=model_name,
        parameters=parameters,
        stream=stream,
        max_retries=0,
        client_kwargs={"max_retries": 0, "timeout": timeout_seconds},
    )


def structured_response_format(result_type: type[BaseModel]) -> dict[str, Any]:
    """071：通过锁定 SDK 的 response_format 接口发送 schema；服务端仍以 Pydantic 为最终校验。"""
    return {"type": "json_schema", "json_schema": {
        "name": result_type.__name__, "schema": result_type.model_json_schema(), "strict": False,
    }}
