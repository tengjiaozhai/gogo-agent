"""000-C 最小 FastAPI 服务与 HTTP 交互练习。

学习目标：
1. 掌握 FastAPI 应用与路由编写
2. 结合 Pydantic 模型自动完成请求体校验与 OpenAPI 文档生成
3. 使用 httpx 客户端在本地发起 HTTP 请求并处理状态码与 JSON
"""

import asyncio
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
import httpx
from httpx import ASGITransport

from exercises.ex000_a_types import TravelRequest

app = FastAPI(title="GoGo Travel Mini API", version="0.1.0")


@app.get("/health")
async def health_check():
    """健康检查端点。"""
    return {"status": "ok", "service": "gogo-agent-mini"}


@app.post("/api/travel/requests", status_code=status.HTTP_201_CREATED)
async def create_travel_request_endpoint(req: TravelRequest):
    """创建差旅申请，由 FastAPI + Pydantic 自动执行字段与格式校验。"""
    # 模拟业务处理
    order_id = f"TRV-{req.user_id[-4:]}-8888"
    return {
        "code": 201,
        "message": "差旅申请已接收",
        "data": {
            "order_id": order_id,
            "user_id": req.user_id,
            "destination": req.destination,
            "start_at": str(req.start_at),
            "budget": req.budget,
            "status": "PENDING_APPROVAL",
        },
    }


async def run_demo_client():
    """使用 httpx 演示直接对 ASGI app 发起 HTTP 交互（无需真实启动 socket 端口）。"""
    print("=== 000-C: FastAPI 与 HTTP 交互演示 ===\n")

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 访问健康检查端点
        resp_health = await client.get("/health")
        print(f"1. 健康检查: HTTP {resp_health.status_code} -> {resp_health.json()}")

        # 2. 提交合法的差旅申请
        valid_payload = {
            "user_id": "emp_2002",
            "destination": "广州",
            "start_at": "2026-11-15",
            "budget": 4000.0,
            "tags": ["年会"],
        }
        resp_post = await client.post("/api/travel/requests", json=valid_payload)
        print(f"\n2. 提交合法申请: HTTP {resp_post.status_code}")
        print(f"   响应内容: {resp_post.json()}")

        # 3. 提交不合法的申请（过去日期）
        invalid_payload = {
            "user_id": "emp_2002",
            "destination": "广州",
            "start_at": "2020-01-01",
        }
        resp_bad = await client.post("/api/travel/requests", json=invalid_payload)
        print(f"\n3. 提交不合法申请 (过去日期): HTTP {resp_bad.status_code}")
        print(f"   拦截原因: {resp_bad.json()['detail'][0]['msg']}")


if __name__ == "__main__":
    asyncio.run(run_demo_client())
