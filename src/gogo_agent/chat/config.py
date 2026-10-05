"""024 主 Agent 与只读信息子 Agent 的提示词和运行参数。"""

from dataclasses import dataclass
import math
import os
from pathlib import Path

from dotenv import load_dotenv

from gogo_agent.intent.runtime import IntentRuntimeSettings, load_intent_runtime_settings


MASTER_SYSTEM_PROMPT = (
    "你是 GoGo 差旅助手的核心智能体。你负责协助用户办理差旅申请、"
    "行程规划、差旅政策咨询与预订服务。请保持专业、简洁和友善。"
    "本轮识别的诉求顺序为：{intents}。按顺序回应，不丢失前项结果。"
    "识别结果只是理解线索，不是身份、审批或下单授权。"
    "{tool_instruction}"
    "目前没有业务写入工具，不得声称已经提交申请、保存方案或完成预订。"
)
INFO_SYSTEM_PROMPT = (
    "你是 GoGo 的只读信息查询子智能体。仅回答普通公共信息问题。"
    "当前未接入差旅政策、实时天气、签证或订单数据；缺少依据时明确说明。"
    "不要声称已查询或修改用户账户、差旅单、预订和审批。"
)
INFO_TOOL_INSTRUCTION = "普通公共信息问题请调用 info_agent，再根据其结果回答。"
NO_TOOL_INSTRUCTION = "本轮没有可调用的业务子智能体；无法办理的事项请直接说明。"
MODEL_MAX_RETRIES = 0


@dataclass(frozen=True)
class ChatAgentSettings:
    """主/信息 Agent 的轮次与超时配置；不包含模型密钥。"""

    master_max_iters: int = 15
    info_max_iters: int = 5
    info_tool_timeout_seconds: float = 60.0
    model_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        """无论从环境还是测试注入，都拒绝无效轮次与超时。"""
        for name in ("master_max_iters", "info_max_iters"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须是正整数")
        for name in ("info_tool_timeout_seconds", "model_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须是正数秒数")


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} 必须是正整数") from None
    if value < 1:
        raise ValueError(f"{name} 必须是正整数")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} 必须是正数秒数") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 必须是正数秒数")
    return value


def load_chat_agent_settings() -> ChatAgentSettings:
    """从环境读取可调整参数，不在错误消息或日志中输出变量值。"""
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    defaults = ChatAgentSettings()
    return ChatAgentSettings(
        master_max_iters=_positive_int("GOGO_MASTER_MAX_ITERS", defaults.master_max_iters),
        info_max_iters=_positive_int("GOGO_INFO_MAX_ITERS", defaults.info_max_iters),
        info_tool_timeout_seconds=_positive_float(
            "GOGO_INFO_TOOL_TIMEOUT_SECONDS", defaults.info_tool_timeout_seconds,
        ),
        model_timeout_seconds=_positive_float(
            "GOGO_AGENT_MODEL_TIMEOUT_SECONDS", defaults.model_timeout_seconds,
        ),
    )


def require_model_configuration() -> IntentRuntimeSettings:
    """复用 017 的网关配置，并要求主/稳定模型名已填写。"""
    settings = load_intent_runtime_settings()
    if not settings.chat_model_name:
        raise ValueError("缺少模型配置：GOGO_MODEL_NAME")
    if not settings.stable_model_name:
        raise ValueError("缺少模型配置：GOGO_STABLE_MODEL_NAME")
    return settings


def master_system_prompt(intents: str, *, info_tool_enabled: bool) -> str:
    """按本轮实际注册工具渲染主 Agent 提示词。"""
    return MASTER_SYSTEM_PROMPT.format(
        intents=intents,
        tool_instruction=INFO_TOOL_INSTRUCTION if info_tool_enabled else NO_TOOL_INSTRUCTION,
    )
