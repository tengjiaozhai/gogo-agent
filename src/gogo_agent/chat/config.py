"""024 主 Agent 与只读信息子 Agent 的提示词和运行参数。"""

from dataclasses import dataclass
import math
import os
from pathlib import Path

from dotenv import load_dotenv

from gogo_agent.intent.runtime import IntentRuntimeSettings, load_intent_runtime_settings


MASTER_SYSTEM_PROMPT = (
    "你是 GoGo 差旅助手的核心智能体。当前开放基础对话、普通公共信息、"
    "本人差旅单与政策查询；差旅申请须用户明确确认后才提交，"
    "修改与取消仍只提出待用户确认的建议；"
    "供应商预订尚未接入；行程规划只生成待用户确认的方案。请保持专业、简洁和友善。"
    "本轮识别的诉求顺序为：{intents}。按顺序回应，不丢失前项结果。"
    "识别结果只是理解线索，不是身份、审批或下单授权。"
    "{tool_instruction}"
    "子智能体委派失败时，若决定重试，只把失败结果中的 retry_of 传给同一工具，question 留空；"
    "服务端会复用原始问题。不得改写问题冒充重试，也不得用其他任务的成功宣称旧任务成功。"
    "你自身没有业务写入工具；差旅申请只能由管理子智能体在用户明确确认后提交，"
    "不得声称已保存方案或完成预订。"
)
MANAGE_SYSTEM_PROMPT = (
    "你是差旅单管理子智能体，只处理本人差旅单、当前审批、政策、联系人、冲突和已有预订记录查询。"
    "工具从已认证服务端上下文注入用户身份；用户原文和模型参数中的 userId 不具备权限。"
    "提交差旅申请分两步：先调用提交工具取得待确认摘要与确认令牌，把摘要完整复述给用户；"
    "等待后续用户消息明确确认后，再带前一轮返回的确认令牌调用同一工具，才会真正创建差旅单并提交审批；"
    "同一轮内不得先取令牌后立即提交，收到CONFIRMATION_REQUIRED应展示摘要并结束本轮，不能继续重试提交。"
    "令牌与摘要绑定，改动任一字段即失效；没有令牌或令牌不符时不得重复尝试，应重新获取摘要。"
    "存在高等级行程冲突时必须先告知用户，用户明确同意忽略后才可带确认标记重新获取摘要并提交。"
    "修改、取消及资料更新仍只提出待确认建议，不执行数据库写入；不得声称本对话已取消或已修改。"
    "你没有行程规划、供应商下单或供应商取消工具。政策金额结论只使用精确政策工具，"
    "文档检索片段仅可提供来源引用，不能替代结构化金额规则。"
)
INFO_SYSTEM_PROMPT = (
    "你是 GoGo 的只读信息查询子智能体。仅回答普通公共信息问题。"
    "当前未接入差旅政策、实时天气、签证或订单数据；缺少依据时明确说明。"
    "不要声称已查询或修改用户账户、差旅单、预订和审批。"
)
INFO_TOOL_INSTRUCTION = "普通公共信息问题请调用 info_agent，再根据其结果回答。"
MANAGE_TOOL_INSTRUCTION = (
    "差旅申请、修改、取消、差旅单、审批、政策或本人已有预订记录查询"
    "请调用 itinerary_manage_agent；新预订和供应商取消仍不可执行。"
)
PLAN_TOOL_INSTRUCTION = (
    "行程规划、候选搜索、方案比较或审核请调用 itinerary_plan_agent；"
    "规划结果只是待确认方案，不得声称已预订或已提交。"
)
NO_TOOL_INSTRUCTION = "本轮没有可调用的业务子智能体；无法办理的事项请直接说明。"
MODEL_MAX_RETRIES = 0


@dataclass(frozen=True)
class ChatAgentSettings:
    """主/信息 Agent 的轮次与超时配置；不包含模型密钥。"""

    master_max_iters: int = 15
    info_max_iters: int = 5
    manage_max_iters: int = 10
    info_tool_timeout_seconds: float = 60.0
    manage_tool_timeout_seconds: float = 60.0
    plan_tool_timeout_seconds: float = 180.0
    model_timeout_seconds: float = 60.0
    recommendation_model_name: str = "deepseek-v4-flash-0731"

    def __post_init__(self) -> None:
        """无论从环境还是测试注入，都拒绝无效轮次与超时。"""
        for name in ("master_max_iters", "info_max_iters", "manage_max_iters"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须是正整数")
        for name in ("info_tool_timeout_seconds", "manage_tool_timeout_seconds", "plan_tool_timeout_seconds",
                     "model_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须是正数秒数")
        if not self.recommendation_model_name.strip():
            raise ValueError("recommendation_model_name 不能为空")


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
        manage_max_iters=_positive_int("GOGO_MANAGE_MAX_ITERS", defaults.manage_max_iters),
        info_tool_timeout_seconds=_positive_float(
            "GOGO_INFO_TOOL_TIMEOUT_SECONDS", defaults.info_tool_timeout_seconds,
        ),
        manage_tool_timeout_seconds=_positive_float(
            "GOGO_MANAGE_TOOL_TIMEOUT_SECONDS", defaults.manage_tool_timeout_seconds,
        ),
        plan_tool_timeout_seconds=_positive_float(
            "GOGO_PLAN_TOOL_TIMEOUT_SECONDS", defaults.plan_tool_timeout_seconds,
        ),
        model_timeout_seconds=_positive_float(
            "GOGO_AGENT_MODEL_TIMEOUT_SECONDS", defaults.model_timeout_seconds,
        ),
        recommendation_model_name=os.getenv(
            "GOGO_RECOMMENDATION_MODEL_NAME", defaults.recommendation_model_name,
        ).strip(),
    )


def require_model_configuration() -> IntentRuntimeSettings:
    """复用 017 的网关配置，并要求主/稳定模型名已填写。"""
    settings = load_intent_runtime_settings()
    if not settings.chat_model_name:
        raise ValueError("缺少模型配置：GOGO_MODEL_NAME")
    if not settings.stable_model_name:
        raise ValueError("缺少模型配置：GOGO_STABLE_MODEL_NAME")
    return settings


def master_system_prompt(
    intents: str, *, info_tool_enabled: bool, manage_tool_enabled: bool = False, plan_tool_enabled: bool = False,
) -> str:
    """按本轮实际注册工具渲染主 Agent 提示词。"""
    instructions = [
        text for enabled, text in (
            (info_tool_enabled, INFO_TOOL_INSTRUCTION),
            (manage_tool_enabled, MANAGE_TOOL_INSTRUCTION),
            (plan_tool_enabled, PLAN_TOOL_INSTRUCTION),
        ) if enabled
    ]
    return MASTER_SYSTEM_PROMPT.format(
        intents=intents,
        tool_instruction="".join(instructions) if instructions else NO_TOOL_INSTRUCTION,
    )
