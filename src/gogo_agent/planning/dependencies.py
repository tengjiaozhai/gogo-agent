"""043 规划服务的进程内注入。

计划记录目前只保存在本进程的内存 SQLite 中：重启后丢失，多实例之间不共享，生产 MySQL 表尚未迁移。
"""

from __future__ import annotations

from .service import PlanService
from .store import create_plan_repository

_service: PlanService | None = None


def get_plan_service() -> PlanService:
    """返回进程内唯一的规划服务；首次调用时创建。"""
    global _service
    if _service is None:
        _service = PlanService(create_plan_repository())
    return _service


def reset_plan_service(service: PlanService | None = None) -> None:
    """测试和脚本专用：替换或清空进程内服务。"""
    global _service
    _service = service
