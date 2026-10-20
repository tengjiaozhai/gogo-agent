"""阶段 F 预订与工具可靠性（061–067）调试示例：模拟供应商、内存库、固定时钟，不访问真实供应商。

最短运行路线（在仓库根目录执行）：
    PYTHONPATH=src .venv/bin/python scripts/demo_061_067_booking.py --case all

可选 --case：
    book       062–064：门禁、成功下单、重复请求只生成一单
    lost       066：响应丢失时对账确认，或保持未知且不重复下单
    cancel     063：取消复用阶段 D 实现；酒店先取消供应商侧；跨用户失败
    reliability 066：只读重试次数与退避、有界并行搜索与用户隔离
    breaker    067：熔断打开、冷却、单次探测与恢复

输出中的 booking_id 由计划、内容版本、方案与行程段派生，差旅单 ID 与审批 ID 由 uuid 生成，因此不打印。
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import date
from itertools import count

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gogo_agent.auth.models import UserAccount
from gogo_agent.booking.reliability import (
    CircuitOpenError, RetryPolicy, ToolBreakerRegistry, call_read_with_retry, search_in_parallel,
)
from gogo_agent.booking.service import BookingError, BookingService
from gogo_agent.booking.supplier import SimulatedSupplier, SupplierCancelGateway
from gogo_agent.db.base import Base
from gogo_agent.db.models import BookingRecordModel, UserProfileModel
from gogo_agent.planning.models import PlanConditions
from gogo_agent.planning.review import ApprovedTrip
from gogo_agent.planning.service import PlanService
from gogo_agent.planning.simulated import SIMULATED_POLICY, simulated_assessors, simulated_search
from gogo_agent.planning.store import create_plan_repository
from gogo_agent.travel.models import AdminDecisionRequest, SubmitTravelRequest
from gogo_agent.travel.repository import BusinessRepository
from gogo_agent.travel.service import TravelBusinessService

TODAY = date(2026, 10, 11)
CONDITIONS = PlanConditions(
    origin="北京", destination="上海", departure_date=date(2026, 10, 20), return_date=date(2026, 10, 22),
)
APPROVED = ApprovedTrip(origin="北京", destination="上海")
ADMIN = UserAccount(user_id="u_001", username="admin", password_hash="x", role="ADMIN")


class Lab:
    """一套隔离的练习环境：模拟供应商的行为脚本由调用方在创建时指定。"""

    def __init__(self, modes: list[str] | None = None) -> None:
        engine = create_engine(
            "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.engine = engine
        self.repo = BusinessRepository(sessionmaker(bind=engine, expire_on_commit=False))
        self.supplier = SimulatedSupplier(modes=list(modes or []))
        self.travel = TravelBusinessService(self.repo, booking_gateway=SupplierCancelGateway(self.supplier))
        with self.repo.transaction() as session:
            session.add(UserProfileModel(user_id="u001", level="P6"))
        ids = count(1)
        self.plans = PlanService(create_plan_repository(), today=lambda: TODAY, id_factory=lambda: f"demo-{next(ids):03d}")
        self.service = BookingService(self.travel, self.plans, self.supplier, sleep=lambda _s: None)

    def approved_order(self) -> str:
        order = self.travel.submit_order("u001", SubmitTravelRequest(
            destination="上海", departure_city="北京", departure_date=date(2026, 10, 20),
            return_date=date(2026, 10, 22), purpose="客户拜访", confirmed=True, idempotency_key="demo-order",
        ))
        self.travel.decide_approval(ADMIN, order.approval_id, AdminDecisionRequest(decision="agree"))
        return order.order_id

    def reviewed_plan(self, order_id: str | None) -> str:
        plan = self.plans.create("u001", CONDITIONS, order_id=order_id)
        self.plans.attach_candidates("u001", plan.plan_id, simulated_search(CONDITIONS))
        self.plans.compute("u001", plan.plan_id, policy=SIMULATED_POLICY)
        asyncio.run(self.plans.review("u001", plan.plan_id, proposal_tag="综合最佳",
                                      assessors=simulated_assessors(), approved=APPROVED))
        return plan.plan_id

    def rows(self) -> list[BookingRecordModel]:
        with self.repo.transaction() as session:
            return list(session.scalars(select(BookingRecordModel).where(BookingRecordModel.user_id == "u001")))

    def close(self) -> None:
        self.engine.dispose()


def case_book() -> None:
    print("== 062–064 门禁与下单 ==")
    lab = Lab()
    try:
        plan_id = lab.reviewed_plan(lab.approved_order())
        try:
            lab.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=False)
        except BookingError as exc:
            print(f"未确认 code={exc.code} 供应商调用次数={lab.supplier.create_calls}")
        first = lab.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        second = lab.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        print(f"首次 resolution={first.resolution} order_no={first.external_order_no} amount={first.amount} {first.currency}")
        print(f"重复 resolution={second.resolution} 同一预订标识={first.booking_id == second.booking_id}")
        print(f"供应商创建调用={lab.supplier.create_calls} 本地记录数={len(lab.rows())} 状态={lab.rows()[0].status}")
    finally:
        lab.close()


def case_lost() -> None:
    print("== 066 响应丢失 ==")
    lab = Lab(modes=["lost_after_commit"])
    try:
        plan_id = lab.reviewed_plan(lab.approved_order())
        outcome = lab.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        print(f"已生成订单但响应丢失 resolution={outcome.resolution} 供应商创建调用={lab.supplier.create_calls}")
    finally:
        lab.close()
    lab = Lab(modes=["lost_before_commit"])
    try:
        plan_id = lab.reviewed_plan(lab.approved_order())
        first = lab.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        again = lab.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        print(f"未生成订单且响应丢失 resolution={first.resolution}/{again.resolution} "
              f"供应商创建调用={lab.supplier.create_calls} 本地状态={lab.rows()[0].status}")
    finally:
        lab.close()


def case_cancel() -> None:
    print("== 063 取消 ==")
    lab = Lab()
    try:
        plan_id = lab.reviewed_plan(lab.approved_order())
        flight = lab.service.book("u001", plan_id, proposal_tag="综合最佳", leg="outbound", confirmed=True)
        hotel = lab.service.book("u001", plan_id, proposal_tag="综合最佳", leg="hotel", confirmed=True)
        lab.service.cancel("u001", flight.booking_id, idempotency_key="demo-cancel-f", confirmed=True, reason="行程变更")
        result = lab.service.cancel("u001", hotel.booking_id, idempotency_key="demo-cancel-h", confirmed=True)
        print(f"酒店取消 supplier_cancelled={result.supplier_cancelled} 供应商取消调用={lab.supplier.cancel_calls}")
        print("本地状态 " + ", ".join(sorted(f"{row.biz_type}:{row.status}" for row in lab.rows())))
        try:
            lab.service.cancel("u002", flight.booking_id, idempotency_key="demo-cross", confirmed=True)
        except BookingError as exc:
            print(f"跨用户取消 code={exc.code} status={exc.status}")
        print(f"本地记录总数={len(lab.rows())}")
    finally:
        lab.close()


def case_reliability() -> None:
    print("== 066 重试与并行 ==")
    sleeps: list[float] = []
    attempts = {"n": 0}

    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("临时断开")
        return "ok"

    result = call_read_with_retry(flaky, RetryPolicy(), sleep=sleeps.append)
    print(f"只读重试 结果={result} 尝试次数={attempts['n']} 退避秒数={sleeps}")

    async def slow(user_id: str) -> list[str]:
        await asyncio.sleep(0.2)
        return [f"{user_id}-候选"]

    async def broken(user_id: str) -> list[str]:
        raise RuntimeError("内部细节")

    async def scenario():
        started = time.monotonic()
        outcomes = await search_in_parallel("u001", {"途牛A": slow, "途牛B": slow, "航班C": slow})
        elapsed = time.monotonic() - started
        mixed = await search_in_parallel("u001", {"途牛A": slow, "坏供应商": broken})
        return outcomes, elapsed, mixed

    outcomes, elapsed, mixed = asyncio.run(scenario())
    print(f"并行搜索 三个供应商耗时低于 0.5 秒={elapsed < 0.5} 全部成功={all(o.ok for o in outcomes.values())}")
    print(f"单个失败不影响其他 成功={[k for k, v in mixed.items() if v.ok]} 失败类型={mixed['坏供应商'].error_type}")


def case_breaker() -> None:
    print("== 067 工具熔断 ==")
    clock = {"now": 0.0}
    registry = ToolBreakerRegistry(clock=lambda: clock["now"])
    calls = {"n": 0}

    async def down() -> str:
        calls["n"] += 1
        raise OSError("天气服务不可用")

    async def news() -> str:
        return "资讯正常"

    async def run_steps() -> None:
        for _ in range(3):
            try:
                await registry.call("query_weather", down)
            except OSError:
                pass
        try:
            await registry.call("query_weather", down)
        except CircuitOpenError as exc:
            print(f"连续 3 次失败后 code={exc.code} 外呼次数={calls['n']}")
        clock["now"] = 60.0
        try:
            await registry.call("query_weather", down)
        except OSError:
            pass
        print(f"冷却后探测失败 状态={registry.breaker('query_weather').state.value} 外呼次数={calls['n']}")
        print(f"其他工具仍可用 结果={await registry.call('query_destination_news', news)}")

    asyncio.run(run_steps())


def main() -> None:
    parser = argparse.ArgumentParser(description="阶段 F 预订与工具可靠性调试示例")
    parser.add_argument("--case", default="all", choices=["book", "lost", "cancel", "reliability", "breaker", "all"])
    args = parser.parse_args()
    cases = {
        "book": case_book, "lost": case_lost, "cancel": case_cancel,
        "reliability": case_reliability, "breaker": case_breaker,
    }
    for name, fn in cases.items():
        if args.case in (name, "all"):
            fn()


if __name__ == "__main__":
    main()
