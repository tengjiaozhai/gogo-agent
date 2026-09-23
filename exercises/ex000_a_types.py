"""000-A 类型与数据练习。

学习目标：
1. 掌握 Pydantic v2 模型定义 (BaseModel, Field, field_validator)
2. 掌握 None、列表、字典的类型注解与默认值
3. 掌握结构化数据校验与异常捕获 (ValidationError)
"""

from datetime import date
from typing import Optional
from pydantic import BaseModel, Field, ValidationError, field_validator


class TravelRequest(BaseModel):
    """差旅申请输入对象。"""

    user_id: str = Field(..., description="申请人用户唯一标识")
    destination: str = Field(..., description="目的地城市或区域")
    start_at: date = Field(..., description="出发日期 (YYYY-MM-DD)")
    budget: Optional[float] = Field(default=None, ge=0, description="预算上限 (元)，为 None 时表示无明确预算")
    tags: list[str] = Field(default_factory=list, description="行程标签列表")
    preferences: dict[str, str] = Field(default_factory=dict, description="出行偏好字典，如 {'seat': 'window'}")

    @field_validator("destination")
    @classmethod
    def validate_destination(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("目的地不能为空或纯空格")
        return trimmed

    @field_validator("start_at")
    @classmethod
    def validate_start_at(cls, value: date) -> date:
        today = date.today()
        if value < today:
            raise ValueError(f"出发日期 {value} 不能早于今天 {today}")
        return value


def parse_travel_request(raw: dict) -> TravelRequest:
    """解析原始字典为结构化 TravelRequest 对象，提供统一异常转换。"""
    try:
        return TravelRequest.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"差旅申请数据格式有误: {e}") from e


if __name__ == "__main__":
    print("=== 000-A: 类型与数据练习演示 ===")

    # 1. 正常构造
    valid_data = {
        "user_id": "emp_1001",
        "destination": "北京",
        "start_at": "2026-10-01",
        "budget": 3500.0,
        "tags": ["商务", "技术交流"],
        "preferences": {"hotel": "含早餐", "seat": "靠窗"},
    }
    req = parse_travel_request(valid_data)
    print("✅ 解析成功:", req.model_dump())

    # 2. 字段缺失测试
    print("\n--- 测试缺失 destination ---")
    try:
        parse_travel_request({"user_id": "emp_1001", "start_at": "2026-10-01"})
    except ValueError as err:
        print("✅ 正确拦截缺失字段:", err)

    # 3. 错误日期测试（过去时间）
    print("\n--- 测试过去日期 ---")
    try:
        parse_travel_request({"user_id": "emp_1001", "destination": "上海", "start_at": "2020-01-01"})
    except ValueError as err:
        print("✅ 正确拦截非法日期:", err)
