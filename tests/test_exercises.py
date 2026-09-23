"""000 基础小练习针对性单元测试 (pytest)。

涵盖：
- 000-A 数据模型校验与异常验证
- 000-B 异步并发性能与上下文隔离验证
- 000-C FastAPI 接口状态码与 JSON 数据验证
"""

import asyncio
from datetime import date, timedelta
import pytest
from pydantic import ValidationError
import httpx
from httpx import ASGITransport

from exercises.ex000_a_types import TravelRequest, parse_travel_request
from exercises.ex000_b_async import plan_travel_serial, plan_travel_concurrent, handle_user_request
from exercises.ex000_c_api import app


# ==================== 000-A 测试 ====================

def test_000_a_valid_request():
    """验证合法的输入数据能够正确转换为结构化对象。"""
    future_date = date.today() + timedelta(days=5)
    data = {
        "user_id": "usr_test_1",
        "destination": "深圳",
        "start_at": future_date.isoformat(),
        "budget": 2000.0,
        "tags": ["培训"],
    }
    req = parse_travel_request(data)
    assert req.user_id == "usr_test_1"
    assert req.destination == "深圳"
    assert req.start_at == future_date
    assert req.budget == 2000.0
    assert req.tags == ["培训"]
    assert req.preferences == {}  # 验证默认空字典


def test_000_a_missing_required_fields():
    """验证缺失必要字段时触发明确错误。"""
    with pytest.raises(ValueError, match="差旅申请数据格式有误"):
        parse_travel_request({"user_id": "usr_test_1"})


def test_000_a_past_date_rejected():
    """验证过去日期被正确拒绝。"""
    past_date = date.today() - timedelta(days=1)
    with pytest.raises(ValueError, match="不能早于今天"):
        parse_travel_request({
            "user_id": "usr_test_1",
            "destination": "北京",
            "start_at": past_date.isoformat(),
        })


def test_000_a_empty_destination_rejected():
    """验证空白目的地被拒绝。"""
    future_date = date.today() + timedelta(days=3)
    with pytest.raises(ValueError, match="目的地不能为空"):
        parse_travel_request({
            "user_id": "usr_test_1",
            "destination": "   ",
            "start_at": future_date.isoformat(),
        })


# ==================== 000-B 测试 ====================

@pytest.mark.asyncio
async def test_000_b_concurrency_speedup():
    """验证 asyncio.gather 并发耗时显著优于串行。"""
    _, _, t_serial = await plan_travel_serial("杭州")
    _, _, t_concurrent = await plan_travel_concurrent("杭州")

    # 并发耗时应明显小于串行耗时
    assert t_concurrent < t_serial


@pytest.mark.asyncio
async def test_000_b_contextvars_isolation():
    """验证并发请求下的 contextvars 用户上下文严格隔离，不发生串扰。"""
    tasks = [
        handle_user_request(user_id=f"user_{i}", req_id=f"REQ-{i}", destination="目的地")
        for i in range(5)
    ]
    results = await asyncio.gather(*tasks)

    for i, res in enumerate(results):
        expected_user = f"user_{i}"
        expected_req = f"REQ-{i}"
        assert res["user_id"] == expected_user
        assert res["flights"][0]["queried_by"] == expected_user
        assert res["flights"][0]["req_id"] == expected_req
        assert res["hotels"][0]["queried_by"] == expected_user
        assert res["hotels"][0]["req_id"] == expected_req


# ==================== 000-C 测试 ====================

@pytest.mark.asyncio
async def test_000_c_health_check_endpoint():
    """验证健康检查接口返回 200 与正确 JSON。"""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "service": "gogo-agent-mini"}


@pytest.mark.asyncio
async def test_000_c_create_travel_request_api():
    """验证通过 HTTP 提交合法与非法申请的状态码和响应数据。"""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 合法请求
        future_date = (date.today() + timedelta(days=10)).isoformat()
        resp_ok = await client.post(
            "/api/travel/requests",
            json={
                "user_id": "usr_9999",
                "destination": "成都",
                "start_at": future_date,
                "budget": 5000.0,
            },
        )
        assert resp_ok.status_code == 201
        data = resp_ok.json()
        assert data["code"] == 201
        assert data["data"]["destination"] == "成都"
        assert data["data"]["user_id"] == "usr_9999"

        # 非法请求 (缺失 destination)
        resp_err = await client.post(
            "/api/travel/requests",
            json={"user_id": "usr_9999", "start_at": future_date},
        )
        assert resp_err.status_code == 422
