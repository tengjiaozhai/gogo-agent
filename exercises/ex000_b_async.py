"""000-B 异步与并发练习。

学习目标：
1. 理解 async def 与 await 异步 I/O 的工作机制
2. 对比串行执行 vs asyncio.gather 并发执行的耗时差异
3. 使用 contextvars 实现协程安全的请求上下文隔离，确保高并发时用户信息不串
"""

import asyncio
import time
from contextvars import ContextVar
from typing import Any

# 定义协程本地变量（类似于 Java 中的 ThreadLocal，但在协程中按上下文自动隔离）
request_context_var: ContextVar[dict[str, Any]] = ContextVar("request_context", default={})


async def search_flights(destination: str, delay: float = 0.15) -> list[dict[str, Any]]:
    """模拟调用外部航司接口搜索航班。"""
    ctx = request_context_var.get()
    user_id = ctx.get("user_id", "anonymous")
    req_id = ctx.get("req_id", "N/A")

    # 模拟外部网络 I/O 阻塞
    await asyncio.sleep(delay)

    return [
        {
            "flight_no": "CA1234",
            "destination": destination,
            "price": 1200.0,
            "queried_by": user_id,
            "req_id": req_id,
        }
    ]


async def search_hotels(destination: str, delay: float = 0.15) -> list[dict[str, Any]]:
    """模拟调用外部酒店接口搜索房源。"""
    ctx = request_context_var.get()
    user_id = ctx.get("user_id", "anonymous")
    req_id = ctx.get("req_id", "N/A")

    # 模拟外部网络 I/O 阻塞
    await asyncio.sleep(delay)

    return [
        {
            "hotel_name": f"{destination}中心万豪酒店",
            "price_per_night": 650.0,
            "queried_by": user_id,
            "req_id": req_id,
        }
    ]


async def plan_travel_serial(destination: str) -> tuple[list[dict], list[dict], float]:
    """串行执行：先查机票，再查酒店。"""
    start_time = time.perf_counter()
    flights = await search_flights(destination)
    hotels = await search_hotels(destination)
    elapsed = time.perf_counter() - start_time
    return flights, hotels, elapsed


async def plan_travel_concurrent(destination: str) -> tuple[list[dict], list[dict], float]:
    """并发执行：同时查机票和酒店。"""
    start_time = time.perf_counter()
    flights, hotels = await asyncio.gather(
        search_flights(destination),
        search_hotels(destination),
    )
    elapsed = time.perf_counter() - start_time
    return flights, hotels, elapsed


async def handle_user_request(user_id: str, req_id: str, destination: str) -> dict[str, Any]:
    """模拟一个完整的用户并发请求处理流程。"""
    # 在当前异步任务的上下文副本中设置变量
    token = request_context_var.set({"user_id": user_id, "req_id": req_id})
    try:
        flights, hotels, elapsed = await plan_travel_concurrent(destination)
        return {
            "user_id": user_id,
            "req_id": req_id,
            "flights": flights,
            "hotels": hotels,
            "elapsed": round(elapsed, 4),
        }
    finally:
        request_context_var.reset(token)


async def main():
    print("=== 000-B: 异步与并发练习演示 ===\n")

    # 1. 耗时对比演示
    request_context_var.set({"user_id": "test_user", "req_id": "REQ-BENCH"})
    print("1. 正在测试串行执行与并发执行耗时差异...")
    _, _, t_serial = await plan_travel_serial("成都")
    _, _, t_concurrent = await plan_travel_concurrent("成都")
    print(f"  👉 串行耗时: {t_serial:.3f} 秒")
    print(f"  👉 并发耗时: {t_concurrent:.3f} 秒 (提升约 {(t_serial / t_concurrent):.1f}x)\n")

    # 2. 多用户并发上下文隔离测试
    print("2. 正在模拟多个用户同时发出并发请求 (测试 contextvars 隔离)...")
    res_a, res_b = await asyncio.gather(
        handle_user_request(user_id="user_alice", req_id="REQ-A-101", destination="深圳"),
        handle_user_request(user_id="user_bob", req_id="REQ-B-202", destination="杭州"),
    )

    print(f"  用户 Alice 结果:")
    print(f"    - 请求ID: {res_a['req_id']}")
    print(f"    - 机票查询人: {res_a['flights'][0]['queried_by']} (req: {res_a['flights'][0]['req_id']})")
    print(f"    - 酒店查询人: {res_a['hotels'][0]['queried_by']} (req: {res_a['hotels'][0]['req_id']})")

    print(f"  用户 Bob 结果:")
    print(f"    - 请求ID: {res_b['req_id']}")
    print(f"    - 机票查询人: {res_b['flights'][0]['queried_by']} (req: {res_b['flights'][0]['req_id']})")
    print(f"    - 酒店查询人: {res_b['hotels'][0]['queried_by']} (req: {res_b['hotels'][0]['req_id']})")

    # 验证没有上下文串味
    assert res_a["flights"][0]["queried_by"] == "user_alice"
    assert res_b["flights"][0]["queried_by"] == "user_bob"
    print("\n✅ 上下文完全隔离，无任何数据串扰！")


if __name__ == "__main__":
    asyncio.run(main())
