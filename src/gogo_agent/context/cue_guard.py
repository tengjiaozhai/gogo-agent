"""改口词守卫：判断用户本轮原话是否明确表示要改变已有的长期记忆。

只有用户本轮原话含改口词时，才允许把已有条目判为 REPLACE；否则降级为 ADD。
本模块只做关键词判定，不判断主题是否相同——主题匹配由模型与 id 校验负责。

纯函数、无依赖：评测脚本与生产代码共用同一份词表，避免指标与行为各自漂移。
"""

from __future__ import annotations

# 基线词表。扩展前须在未用于调参的样例上验证，否则是在同一批数据上过拟合。
CUE_TERMS: tuple[str, ...] = ("以后", "改成", "不再", "换成")


def matched_cues(utterance: str) -> tuple[str, ...]:
    """返回本轮原话命中的改口词，按 `CUE_TERMS` 顺序；未命中返回空元组。

    供日志与诊断使用：只回答"命中了什么"，不参与判定结果。
    """
    return tuple(term for term in CUE_TERMS if term in utterance)


def guard_fires(utterance: str) -> bool:
    """守卫判定：本轮原话含任一改口词即放行 REPLACE。"""
    return bool(matched_cues(utterance))
